/*
 * geventhttpclient HTTP/2 sans-IO wrapper around nghttp2.
 *
 * Compiled by setup.py together with the vendored nghttp2 lib/ sources
 * and statically linked (llhttp-Muster: no CMake, no system dependency).
 *
 * Sans-IO design: nghttp2 callbacks copy data into Python objects
 * (bytes, lists of tuples). The Python layer feeds inbound bytes via
 * Session.recv() and receives (events, outbound_frames) back; outbound
 * frames produced by the Session.submit_*() methods are sent by the
 * caller over its own transport. No I/O happens in this module.
 *
 * Concurrency: all entry points must be called under the GIL with only
 * one owner per session (typically one greenlet driving a connection
 * pump).
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <nghttp2/nghttp2.h>

#include <stdarg.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

/* === Event kind names, interned once at module init === */

static PyObject *KIND_HEADERS;
static PyObject *KIND_DATA;
static PyObject *KIND_STREAM_RESET;
static PyObject *KIND_STREAM_CLOSED;
static PyObject *KIND_SETTINGS;
static PyObject *KIND_PING;
static PyObject *KIND_GOAWAY;
static PyObject *KIND_WINDOW_UPDATE;

/* === Streaming body provider ===
 *
 * nghttp2 pulls request-body bytes through a data source read callback
 * while draining the send queue (which is also where flow control
 * defers frames). Request bodies are collected per stream by
 * py_stream_body; submit_data appends chunks and the callback copies
 * them out. A collector is freed when the callback reports EOF, when
 * the stream closes, or when the session dies. */

typedef struct py_stream_body {
    int32_t stream_id;
    PyObject *chunks;      /* list of bytes objects not yet consumed */
    Py_ssize_t offset;     /* read offset into chunks[0] */
    int finished;          /* caller submitted the final chunk */
    struct py_stream_body *next;
} py_stream_body;

typedef struct {
    PyObject_HEAD
    nghttp2_session *session;
    /* stream_id -> {"headers": [(name, value), ...]} accumulating the
     * header block of the HEADERS frame currently being received. */
    PyObject *stream_headers;
    /* events collected by the callbacks of the current recv() call */
    PyObject *pending_events;
    /* linked list of request-body collectors awaiting EOF */
    py_stream_body *bodies;
} PyHTTP2Session;

static PyTypeObject PyHTTP2Session_Type;

static py_stream_body *
body_find(PyHTTP2Session *self, int32_t stream_id)
{
    py_stream_body *body = self->bodies;
    while (body != NULL) {
        if (body->stream_id == stream_id) return body;
        body = body->next;
    }
    return NULL;
}

static void
body_free(PyHTTP2Session *self, py_stream_body *target)
{
    py_stream_body **link = &self->bodies;
    while (*link != NULL) {
        if (*link == target) {
            *link = target->next;
            Py_XDECREF(target->chunks);
            PyMem_Free(target);
            return;
        }
        link = &(*link)->next;
    }
}

static void
bodies_free_all(PyHTTP2Session *self)
{
    py_stream_body *body = self->bodies;
    while (body != NULL) {
        py_stream_body *next = body->next;
        Py_XDECREF(body->chunks);
        PyMem_Free(body);
        body = next;
    }
    self->bodies = NULL;
}

/* === helpers === */

static int
dict_set_long(PyObject *dict, const char *key, long value)
{
    PyObject *v = PyLong_FromLong(value);
    if (v == NULL) return -1;
    int rc = PyDict_SetItemString(dict, key, v);
    Py_DECREF(v);
    return rc;
}

static int
dict_set_ulong(PyObject *dict, const char *key, unsigned long value)
{
    PyObject *v = PyLong_FromUnsignedLong(value);
    if (v == NULL) return -1;
    int rc = PyDict_SetItemString(dict, key, v);
    Py_DECREF(v);
    return rc;
}

static int
dict_set_bool(PyObject *dict, const char *key, int truth)
{
    PyObject *v = truth ? Py_True : Py_False;
    return PyDict_SetItemString(dict, key, v);
}

static int
dict_set_bytes(PyObject *dict, const char *key, const char *buf, size_t len)
{
    PyObject *v = PyBytes_FromStringAndSize(buf, (Py_ssize_t)len);
    if (v == NULL) return -1;
    int rc = PyDict_SetItemString(dict, key, v);
    Py_DECREF(v);
    return rc;
}

/* Append the event to the pending list; steals a reference on failure
 * paths only (always decrefs the event). Returns 0 or -1 (exception
 * set; nghttp2 callback failure must be returned by the caller). */
static int
emit_event(PyHTTP2Session *self, PyObject *event)
{
    if (event == NULL) return -1;
    if (PyList_Append(self->pending_events, event) < 0) {
        Py_DECREF(event);
        return -1;
    }
    Py_DECREF(event);
    return 0;
}

static PyObject *
new_event(PyObject *kind, int32_t stream_id)
{
    PyObject *event = PyDict_New();
    if (event == NULL) return NULL;
    if (PyDict_SetItemString(event, "_kind", kind) < 0) goto fail;
    if (dict_set_long(event, "stream_id", stream_id) < 0) goto fail;
    return event;
fail:
    Py_DECREF(event);
    return NULL;
}

/* === nghttp2 callbacks === */

static int
on_header_callback(nghttp2_session *session, const nghttp2_frame *frame,
                   const uint8_t *name, size_t namelen,
                   const uint8_t *value, size_t valuelen, uint8_t flags,
                   void *user_data)
{
    PyHTTP2Session *self = (PyHTTP2Session *)user_data;
    int32_t sid = frame->hd.stream_id;

    PyObject *sid_key = PyLong_FromLong(sid);
    if (sid_key == NULL) return NGHTTP2_ERR_CALLBACK_FAILURE;

    /* borrowed; may be NULL on first header of this stream */
    PyObject *acc_list = NULL;
    PyObject *stream_dict = PyDict_GetItemWithError(self->stream_headers, sid_key);
    if (stream_dict == NULL) {
        if (PyErr_Occurred()) goto fail;
        stream_dict = PyDict_New();
        if (stream_dict == NULL) goto fail;
        if (PyDict_SetItem(self->stream_headers, sid_key, stream_dict) < 0) {
            Py_DECREF(stream_dict);
            goto fail;
        }
        Py_DECREF(stream_dict); /* dict holds its own reference now */
        acc_list = PyList_New(0);
        if (acc_list == NULL) goto fail;
        if (PyDict_SetItemString(stream_dict, "headers", acc_list) < 0) {
            Py_DECREF(acc_list);
            goto fail;
        }
        Py_DECREF(acc_list); /* stream_dict holds it */
    }
    /* borrowed list; must exist now */
    acc_list = PyDict_GetItemString(stream_dict, "headers");
    if (acc_list == NULL) goto fail;

    PyObject *name_str = PyUnicode_FromStringAndSize((const char *)name,
                                                     (Py_ssize_t)namelen);
    PyObject *value_str = PyUnicode_FromStringAndSize((const char *)value,
                                                      (Py_ssize_t)valuelen);
    if (name_str == NULL || value_str == NULL) {
        Py_XDECREF(name_str);
        Py_XDECREF(value_str);
        goto fail;
    }
    PyObject *pair = PyTuple_Pack(2, name_str, value_str);
    Py_DECREF(name_str);
    Py_DECREF(value_str);
    if (pair == NULL) goto fail;
    if (PyList_Append(acc_list, pair) < 0) {
        Py_DECREF(pair);
        goto fail;
    }
    Py_DECREF(pair);
    Py_DECREF(sid_key);
    return 0;
fail:
    Py_DECREF(sid_key);
    return NGHTTP2_ERR_CALLBACK_FAILURE;
}

static int
on_frame_recv_callback(nghttp2_session *session, const nghttp2_frame *frame,
                       void *user_data)
{
    PyHTTP2Session *self = (PyHTTP2Session *)user_data;
    int32_t sid = frame->hd.stream_id;
    int ack = (frame->hd.flags & NGHTTP2_FLAG_ACK) != 0;

    switch (frame->hd.type) {
    case NGHTTP2_HEADERS: {
        PyObject *sid_key = PyLong_FromLong(sid);
        if (sid_key == NULL) return NGHTTP2_ERR_CALLBACK_FAILURE;
        PyObject *stream_dict = PyDict_GetItemWithError(self->stream_headers,
                                                        sid_key);
        Py_DECREF(sid_key);
        if (stream_dict == NULL) {
            if (PyErr_Occurred()) return NGHTTP2_ERR_CALLBACK_FAILURE;
            return 0; /* unknown stream: nothing accumulated */
        }
        PyObject *headers_list = PyDict_GetItemString(stream_dict, "headers");
        if (headers_list == NULL) return NGHTTP2_ERR_CALLBACK_FAILURE;

        PyObject *event = new_event(KIND_HEADERS, sid);
        if (event == NULL) return NGHTTP2_ERR_CALLBACK_FAILURE;
        if (PyDict_SetItemString(event, "headers", headers_list) < 0) {
            Py_DECREF(event);
            return NGHTTP2_ERR_CALLBACK_FAILURE;
        }
        if (dict_set_bool(event, "end_stream",
                          (frame->hd.flags & NGHTTP2_FLAG_END_STREAM) != 0) < 0) {
            Py_DECREF(event);
            return NGHTTP2_ERR_CALLBACK_FAILURE;
        }
        if (emit_event(self, event) < 0) return NGHTTP2_ERR_CALLBACK_FAILURE;

        /* Reset the accumulator: the event now owns the collected
         * list; a following trailer block starts a fresh one. */
        PyObject *fresh = PyList_New(0);
        if (fresh == NULL) return NGHTTP2_ERR_CALLBACK_FAILURE;
        if (PyDict_SetItemString(stream_dict, "headers", fresh) < 0) {
            Py_DECREF(fresh);
            return NGHTTP2_ERR_CALLBACK_FAILURE;
        }
        Py_DECREF(fresh);
        break;
    }
    case NGHTTP2_DATA: {
        /* DATA chunks are emitted by on_data_chunk_recv_callback; here
         * we only need the (rare) end-of-stream notification for a
         * frame without chunk callback invocations (empty DATA). */
        if ((frame->hd.flags & NGHTTP2_FLAG_END_STREAM) != 0) {
            PyObject *event = new_event(KIND_STREAM_CLOSED, sid);
            if (event == NULL) return NGHTTP2_ERR_CALLBACK_FAILURE;
            if (dict_set_bool(event, "end_stream", 1) < 0 ||
                dict_set_ulong(event, "error_code", NGHTTP2_NO_ERROR) < 0) {
                Py_DECREF(event);
                return NGHTTP2_ERR_CALLBACK_FAILURE;
            }
            if (emit_event(self, event) < 0) return NGHTTP2_ERR_CALLBACK_FAILURE;
        }
        break;
    }
    case NGHTTP2_RST_STREAM: {
        PyObject *event = new_event(KIND_STREAM_RESET, sid);
        if (event == NULL) return NGHTTP2_ERR_CALLBACK_FAILURE;
        if (dict_set_ulong(event, "error_code",
                           frame->rst_stream.error_code) < 0) {
            Py_DECREF(event);
            return NGHTTP2_ERR_CALLBACK_FAILURE;
        }
        if (emit_event(self, event) < 0) return NGHTTP2_ERR_CALLBACK_FAILURE;
        break;
    }
    case NGHTTP2_SETTINGS: {
        PyObject *event = PyDict_New();
        if (event == NULL) return NGHTTP2_ERR_CALLBACK_FAILURE;
        if (PyDict_SetItemString(event, "_kind", KIND_SETTINGS) < 0 ||
            dict_set_long(event, "stream_id", 0) < 0 ||
            dict_set_bool(event, "ack", ack) < 0) {
            Py_DECREF(event);
            return NGHTTP2_ERR_CALLBACK_FAILURE;
        }
        PyObject *settings = PyDict_New();
        if (settings == NULL) {
            Py_DECREF(event);
            return NGHTTP2_ERR_CALLBACK_FAILURE;
        }
        int failed = 0;
        for (size_t i = 0; i < frame->settings.niv; ++i) {
            PyObject *key = PyLong_FromLong(frame->settings.iv[i].settings_id);
            PyObject *value = PyLong_FromUnsignedLong(
                frame->settings.iv[i].value);
            if (key == NULL || value == NULL ||
                PyDict_SetItem(settings, key, value) < 0) {
                Py_XDECREF(key);
                Py_XDECREF(value);
                failed = 1;
                break;
            }
            Py_DECREF(key);
            Py_DECREF(value);
        }
        if (failed || PyDict_SetItemString(event, "settings", settings) < 0) {
            Py_DECREF(settings);
            Py_DECREF(event);
            return NGHTTP2_ERR_CALLBACK_FAILURE;
        }
        Py_DECREF(settings);
        if (emit_event(self, event) < 0) return NGHTTP2_ERR_CALLBACK_FAILURE;
        break;
    }
    case NGHTTP2_PING: {
        PyObject *event = PyDict_New();
        if (event == NULL) return NGHTTP2_ERR_CALLBACK_FAILURE;
        if (PyDict_SetItemString(event, "_kind", KIND_PING) < 0 ||
            dict_set_long(event, "stream_id", 0) < 0 ||
            dict_set_bool(event, "ack", ack) < 0 ||
            dict_set_bytes(event, "opaque_data",
                           (const char *)frame->ping.opaque_data, 8) < 0) {
            Py_DECREF(event);
            return NGHTTP2_ERR_CALLBACK_FAILURE;
        }
        if (emit_event(self, event) < 0) return NGHTTP2_ERR_CALLBACK_FAILURE;
        break;
    }
    case NGHTTP2_GOAWAY: {
        PyObject *event = PyDict_New();
        if (event == NULL) return NGHTTP2_ERR_CALLBACK_FAILURE;
        if (PyDict_SetItemString(event, "_kind", KIND_GOAWAY) < 0 ||
            dict_set_long(event, "stream_id", 0) < 0 ||
            dict_set_long(event, "last_stream_id",
                          frame->goaway.last_stream_id) < 0 ||
            dict_set_ulong(event, "error_code", frame->goaway.error_code) < 0 ||
            dict_set_bytes(event, "debug_data",
                           (const char *)frame->goaway.opaque_data,
                           frame->goaway.opaque_data_len) < 0) {
            Py_DECREF(event);
            return NGHTTP2_ERR_CALLBACK_FAILURE;
        }
        if (emit_event(self, event) < 0) return NGHTTP2_ERR_CALLBACK_FAILURE;
        break;
    }
    case NGHTTP2_WINDOW_UPDATE: {
        PyObject *event = new_event(KIND_WINDOW_UPDATE, sid);
        if (event == NULL) return NGHTTP2_ERR_CALLBACK_FAILURE;
        if (dict_set_ulong(event, "increment",
                           frame->window_update.window_size_increment) < 0) {
            Py_DECREF(event);
            return NGHTTP2_ERR_CALLBACK_FAILURE;
        }
        if (emit_event(self, event) < 0) return NGHTTP2_ERR_CALLBACK_FAILURE;
        break;
    }
    default:
        break;
    }
    return 0;
}

static int
on_stream_close_callback(nghttp2_session *session, int32_t stream_id,
                         uint32_t error_code, void *user_data)
{
    PyHTTP2Session *self = (PyHTTP2Session *)user_data;

    /* Report the close unless it already surfaced as a DATA
     * END_STREAM event (normal response completion). */
    PyObject *event = new_event(KIND_STREAM_CLOSED, stream_id);
    if (event == NULL) return NGHTTP2_ERR_CALLBACK_FAILURE;
    if (dict_set_ulong(event, "error_code", error_code) < 0 ||
        dict_set_bool(event, "end_stream", 0) < 0) {
        Py_DECREF(event);
        return NGHTTP2_ERR_CALLBACK_FAILURE;
    }
    if (emit_event(self, event) < 0) return NGHTTP2_ERR_CALLBACK_FAILURE;

    /* Drop the per-stream header accumulator and any unconsumed
     * request-body collector (e.g. after RST_STREAM mid-upload). */
    PyObject *sid_key = PyLong_FromLong(stream_id);
    if (sid_key == NULL) return NGHTTP2_ERR_CALLBACK_FAILURE;
    if (PyDict_DelItem(self->stream_headers, sid_key) < 0 &&
        PyErr_ExceptionMatches(PyExc_KeyError)) {
        PyErr_Clear();
    }
    Py_DECREF(sid_key);

    py_stream_body *body = body_find(self, stream_id);
    if (body != NULL) body_free(self, body);
    return 0;
}

static int
on_data_chunk_recv_callback(nghttp2_session *session, uint8_t flags,
                            int32_t stream_id, const uint8_t *data,
                            size_t len, void *user_data)
{
    PyHTTP2Session *self = (PyHTTP2Session *)user_data;
    PyObject *event = new_event(KIND_DATA, stream_id);
    if (event == NULL) return NGHTTP2_ERR_CALLBACK_FAILURE;
    if (dict_set_bytes(event, "data", (const char *)data, len) < 0 ||
        dict_set_bool(event, "end_stream",
                      (flags & NGHTTP2_FLAG_END_STREAM) != 0) < 0) {
        Py_DECREF(event);
        return NGHTTP2_ERR_CALLBACK_FAILURE;
    }
    if (emit_event(self, event) < 0) return NGHTTP2_ERR_CALLBACK_FAILURE;
    return 0;
}

static ssize_t
send_callback(nghttp2_session *session, const uint8_t *data, size_t length,
              int flags, void *user_data)
{
    /* Never used: we drain via nghttp2_session_mem_send(). nghttp2
     * still validates that a send callback is registered. */
    (void)session; (void)data; (void)flags; (void)user_data;
    return (ssize_t)length;
}

static ssize_t
body_read_callback(nghttp2_session *session, int32_t stream_id, uint8_t *buf,
                   size_t length, uint32_t *data_flags,
                   nghttp2_data_source *source, void *user_data)
{
    py_stream_body *body = (py_stream_body *)source->ptr;
    size_t copied = 0;
    while (copied < length && PyList_GET_SIZE(body->chunks) > 0) {
        PyObject *chunk = PyList_GET_ITEM(body->chunks, 0); /* borrowed */
        /* Defensive: every chunk in the queue is a bytes object --
         * submit_data() normalises to PyBytes before appending. If a
         * caller ever bypasses submit_data() and patches the queue,
         * we'd rather raise here than silently corrupt memory via
         * PyBytes_GET_SIZE on a non-bytes object. */
        if (!PyBytes_Check(chunk)) {
            PyErr_SetString(PyExc_TypeError,
                            "body chunk is not bytes (HTTP/2 collector invariant violated)");
            return NGHTTP2_ERR_TEMPORAL_CALLBACK_FAILURE;
        }
        Py_ssize_t remaining = PyBytes_GET_SIZE(chunk) - body->offset;
        size_t to_copy = (size_t)remaining < (length - copied)
                             ? (size_t)remaining
                             : length - copied;
        memcpy(buf + copied, PyBytes_AS_STRING(chunk) + body->offset,
               to_copy);
        body->offset += (Py_ssize_t)to_copy;
        copied += to_copy;
        if (body->offset == PyBytes_GET_SIZE(chunk)) {
            /* chunk fully consumed: drop it from the queue */
            if (PyList_SetSlice(body->chunks, 0, 1, NULL) < 0)
                return NGHTTP2_ERR_CALLBACK_FAILURE;
            body->offset = 0;
        }
    }
    if (PyList_GET_SIZE(body->chunks) == 0) {
        if (body->finished) {
            /* Last chunk fully consumed (this call may have copied
             * bytes already): signal end of body. */
            *data_flags |= NGHTTP2_DATA_FLAG_EOF;
            body_free((PyHTTP2Session *)user_data, body);
        } else if (copied == 0) {
            /* Nothing read and more submit_data() calls expected:
             * defer the stream; submit_data resumes it via
             * nghttp2_session_resume_data(). Returning DEFERRED after
             * having copied bytes would drop them. */
            return NGHTTP2_ERR_DEFERRED;
        }
        /* else: frame ends here without END_STREAM; the next
         * submit_data() call resumes the item. */
    }
    return (ssize_t)copied;
}

/* === send-side helpers === */

/* Drain the nghttp2 outbound queue into one bytes object. Returns a new
 * reference (possibly b"") or NULL with an exception set. */
static PyObject *
drain_send(nghttp2_session *session)
{
    /* Frames are pulled one by one; accumulate in a bytearray for the
     * common multi-frame case (preface SETTINGS, HEADERS, DATA, ACKs). */
    PyObject *out = PyByteArray_FromStringAndSize("", 0);
    if (out == NULL) return NULL;
    while (nghttp2_session_want_write(session)) {
        const uint8_t *frame = NULL;
        ssize_t n = nghttp2_session_mem_send(session, &frame);
        if (n < 0) {
            Py_DECREF(out);
            PyErr_Format(PyExc_RuntimeError,
                         "nghttp2_session_mem_send failed: %zd", n);
            return NULL;
        }
        if (n == 0) break; /* no more data even though want_write said so */
        Py_ssize_t before = PyByteArray_GET_SIZE(out);
        if (PyByteArray_Resize(out, before + (Py_ssize_t)n) < 0) {
            Py_DECREF(out);
            return NULL;
        }
        memcpy(PyByteArray_AsString(out) + before, frame, (size_t)n);
    }
    /* Return plain bytes: the result is handed straight to a socket. */
    PyObject *result = PyBytes_FromStringAndSize(PyByteArray_AsString(out),
                                                 PyByteArray_GET_SIZE(out));
    Py_DECREF(out);
    return result;
}

/* Build an nghttp2_nv array from a Python sequence of (str, str) pairs.
 * The strings are borrowed from the sequence entries, which the caller
 * must keep alive until the corresponding submit call returned. Returns
 * 0 or -1 (exception set). */
static int
make_nv_array(PyObject *headers, nghttp2_nv **out_nva, size_t *out_nvlen)
{
    if (!PyList_Check(headers) && !PyTuple_Check(headers)) {
        PyErr_SetString(PyExc_TypeError,
                        "headers must be a list of (name, value) pairs");
        return -1;
    }
    Py_ssize_t n = PySequence_Size(headers);
    if (n < 0) return -1;
    nghttp2_nv *nva = PyMem_Malloc(sizeof(nghttp2_nv) * (size_t)(n > 0 ? n : 1));
    if (nva == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    for (Py_ssize_t i = 0; i < n; ++i) {
        PyObject *pair = PySequence_GetItem(headers, i); /* new reference */
        if (pair == NULL) {
            PyMem_Free(nva);
            return -1;
        }
        if (!PyTuple_Check(pair) || PyTuple_GET_SIZE(pair) != 2) {
            PyErr_SetString(PyExc_ValueError,
                            "each header must be a (name, value) tuple of str");
            Py_DECREF(pair);
            PyMem_Free(nva);
            return -1;
        }
        Py_ssize_t name_len = 0;
        Py_ssize_t value_len = 0;
        const char *name = PyUnicode_AsUTF8AndSize(PyTuple_GET_ITEM(pair, 0),
                                                   &name_len);
        const char *value = PyUnicode_AsUTF8AndSize(PyTuple_GET_ITEM(pair, 1),
                                                    &value_len);
        if (name == NULL || value == NULL) {
            Py_DECREF(pair);
            PyMem_Free(nva);
            return -1;
        }
        nva[i].name = (uint8_t *)name;
        nva[i].namelen = (size_t)name_len;
        nva[i].value = (uint8_t *)value;
        nva[i].valuelen = (size_t)value_len;
        nva[i].flags = NGHTTP2_NV_FLAG_NONE;
        Py_DECREF(pair);
    }
    *out_nva = nva;
    *out_nvlen = (size_t)n;
    return 0;
}

/* === Session lifecycle === */

static PyObject *
session_new(int is_client)
{
    PyHTTP2Session *self = PyObject_New(PyHTTP2Session, &PyHTTP2Session_Type);
    if (self == NULL) return NULL;
    self->session = NULL;
    self->stream_headers = PyDict_New();
    self->pending_events = PyList_New(0);
    self->bodies = NULL;
    if (self->stream_headers == NULL || self->pending_events == NULL) {
        Py_XDECREF(self->stream_headers);
        Py_XDECREF(self->pending_events);
        Py_DECREF(self);
        return NULL;
    }

    nghttp2_session_callbacks *callbacks = NULL;
    nghttp2_option *option = NULL;
    if (nghttp2_session_callbacks_new(&callbacks) != 0 ||
        nghttp2_option_new(&option) != 0) {
        nghttp2_session_callbacks_del(callbacks);
        nghttp2_option_del(option);
        PyErr_SetString(PyExc_RuntimeError, "nghttp2 setup failed");
        Py_DECREF(self);
        return NULL;
    }
    nghttp2_session_callbacks_set_send_callback(callbacks, send_callback);
    nghttp2_session_callbacks_set_on_header_callback(callbacks,
                                                     on_header_callback);
    nghttp2_session_callbacks_set_on_frame_recv_callback(callbacks,
                                                         on_frame_recv_callback);
    nghttp2_session_callbacks_set_on_stream_close_callback(
        callbacks, on_stream_close_callback);
    nghttp2_session_callbacks_set_on_data_chunk_recv_callback(
        callbacks, on_data_chunk_recv_callback);

    int rv;
    if (is_client) {
        /* Do not accept server-pushed streams beyond the default limit;
         * real push refusal is configured via ENABLE_PUSH=0 settings. */
        nghttp2_option_set_peer_max_concurrent_streams(option, 100);
        rv = nghttp2_session_client_new2(&self->session, callbacks, self,
                                         option);
    } else {
        rv = nghttp2_session_server_new2(&self->session, callbacks, self,
                                         option);
    }
    nghttp2_option_del(option);
    nghttp2_session_callbacks_del(callbacks);
    if (rv != 0) {
        PyErr_Format(PyExc_RuntimeError,
                     "nghttp2_session_%s_new2 failed: %d",
                     is_client ? "client" : "server", rv);
        Py_DECREF(self);
        return NULL;
    }
    /* RFC 9113 section 3.4: the first frame after the connection
     * preface MUST be SETTINGS. nghttp2 sends the preface magic on its
     * own but leaves the initial SETTINGS frame to the application, so
     * we queue defaults (all values RFC defaults; submit_settings can
     * override later ones). */
    if (nghttp2_submit_settings(self->session, NGHTTP2_FLAG_NONE, NULL, 0)
        != 0) {
        PyErr_SetString(PyExc_RuntimeError,
                        "queueing the initial SETTINGS frame failed");
        Py_DECREF(self);
        return NULL;
    }
    return (PyObject *)self;
}

static PyObject *
session_client_new(PyObject *module, PyObject *Py_UNUSED(ignored))
{
    return session_new(1);
}

static PyObject *
session_server_new(PyObject *module, PyObject *Py_UNUSED(ignored))
{
    return session_new(0);
}

static void
session_dealloc(PyObject *self_obj)
{
    PyHTTP2Session *self = (PyHTTP2Session *)self_obj;
    if (self->session != NULL) {
        nghttp2_session_del(self->session);
        self->session = NULL;
    }
    bodies_free_all(self);
    Py_XDECREF(self->stream_headers);
    Py_XDECREF(self->pending_events);
    PyObject_Del(self);
}

/* === Session methods === */

static PyObject *
session_recv(PyObject *self_obj, PyObject *data_obj)
{
    PyHTTP2Session *self = (PyHTTP2Session *)self_obj;
    Py_buffer view;
    if (PyObject_GetBuffer(data_obj, &view, PyBUF_SIMPLE) < 0) return NULL;

    ssize_t consumed = nghttp2_session_mem_recv(
        self->session, (const uint8_t *)view.buf, (size_t)view.len);
    PyBuffer_Release(&view);
    if (consumed < 0) {
        PyErr_Format(PyExc_RuntimeError,
                     "nghttp2_session_mem_recv failed: %zd", consumed);
        /* Surface the events that nghttp2 fired in this batch anyway:
         * a partial parse may already have produced SETTINGS-ACKs or
         * HEADERS that the caller would otherwise lose. Drain the
         * pending_events list into a fresh tuple of (events, b''). */
        PyObject *events = self->pending_events;
        self->pending_events = PyList_New(0);
        if (self->pending_events == NULL) {
            Py_DECREF(events);
            return NULL;
        }
        /* Pending events reference session state; on mem_recv failure
         * the session is in an undefined state. Drop them instead of
         * handing them up, otherwise they may reference freed data
         * via header accumulator. */
        Py_DECREF(events);
        return NULL;
    }
    PyObject *outbound = drain_send(self->session);
    if (outbound == NULL) return NULL;
    PyObject *events = self->pending_events; /* hand over, replace */
    self->pending_events = PyList_New(0);
    if (self->pending_events == NULL) {
        Py_DECREF(events);
        Py_DECREF(outbound);
        return NULL;
    }
    PyObject *result = PyTuple_Pack(2, events, outbound);
    Py_DECREF(events);
    Py_DECREF(outbound);
    return result;
}

static py_stream_body *
body_collector_new(PyHTTP2Session *self)
{
    py_stream_body *body = PyMem_Malloc(sizeof(py_stream_body));
    if (body == NULL) {
        PyErr_NoMemory();
        return NULL;
    }
    body->stream_id = 0; /* assigned by the caller */
    body->chunks = PyList_New(0);
    body->offset = 0;
    body->finished = 0;
    body->next = self->bodies;
    if (body->chunks == NULL) {
        PyMem_Free(body);
        return NULL;
    }
    self->bodies = body; /* linked via body->next */
    return body;
}

static PyObject *
session_submit_request(PyObject *self_obj, PyObject *args, PyObject *kwds)
{
    PyHTTP2Session *self = (PyHTTP2Session *)self_obj;
    PyObject *headers = NULL;
    int with_body = 0;
    static char *kwlist[] = {"headers", "with_body", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "O!|p", kwlist,
                                     &PyList_Type, &headers, &with_body))
        return NULL;

    nghttp2_nv *nva = NULL;
    size_t nvlen = 0;
    if (make_nv_array(headers, &nva, &nvlen) < 0) return NULL;

    /* NULL data provider => HEADERS carry END_STREAM (complete request
     * without body). With a body, a per-stream collector receives the
     * bytes submitted via submit_data; the read callback reports
     * NGHTTP2_ERR_DEFERRED until the first chunk arrives. */
    nghttp2_data_provider provider;
    nghttp2_data_provider *provider_ptr = NULL;
    py_stream_body *body = NULL;
    if (with_body) {
        body = body_collector_new(self);
        if (body == NULL) {
            PyMem_Free(nva);
            return NULL;
        }
        provider.source.ptr = body;
        provider.read_callback = body_read_callback;
        provider_ptr = &provider;
    }
    int32_t stream_id = nghttp2_submit_request(self->session, NULL, nva,
                                               nvlen, provider_ptr, NULL);
    PyMem_Free(nva);
    if (stream_id < 0) {
        if (body != NULL) body_free(self, body);
        PyErr_Format(PyExc_RuntimeError,
                     "nghttp2_submit_request failed: %d", stream_id);
        return NULL;
    }
    if (body != NULL) body->stream_id = stream_id;
    PyObject *outbound = drain_send(self->session);
    if (outbound == NULL) return NULL;
    PyObject *sid_obj = PyLong_FromLong(stream_id);
    if (sid_obj == NULL) {
        Py_DECREF(outbound);
        return NULL;
    }
    PyObject *result = PyTuple_Pack(2, sid_obj, outbound);
    Py_DECREF(sid_obj);
    Py_DECREF(outbound);
    return result;
}

static PyObject *
session_submit_data(PyObject *self_obj, PyObject *args, PyObject *kwds)
{
    PyHTTP2Session *self = (PyHTTP2Session *)self_obj;
    int32_t stream_id = 0;
    PyObject *data = NULL;
    int end_stream = 0;
    static char *kwlist[] = {"stream_id", "data", "end_stream", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "iOp", kwlist,
                                     &stream_id, &data, &end_stream))
        return NULL;

    py_stream_body *body = body_find(self, stream_id);
    if (body == NULL) {
        PyErr_Format(PyExc_ValueError,
                     "stream %d has no open request body", (int)stream_id);
        return NULL;
    }
    if (body->finished) {
        PyErr_SetString(PyExc_ValueError,
                        "request body already finished for this stream");
        return NULL;
    }
    /* Enforce bytes-only. bytearray / memoryview would otherwise be
     * silently mis-read by the collector's PyBytes_GET_SIZE macros.
     * The .pyi stub declares the type; this runtime check is a
     * belt-and-braces measure that catches direct calls on the C
     * API bypassing the Python wrapper. */
    if (!PyBytes_Check(data)) {
        PyErr_SetString(PyExc_TypeError,
                        "submit_data() data must be bytes");
        return NULL;
    }
    if (PyList_Append(body->chunks, data) < 0) return NULL;
    if (end_stream) body->finished = 1;

    /* If nghttp2 deferred the DATA frame (empty queue at last read),
     * resume it now that a chunk is available. ``resume_data`` returns
     * 0 on success and a negative code if there is nothing to
     * resume; the negative case is not an error and we clear only the
     * specific SystemError nghttp2 would surface if at all. */
    int rv = nghttp2_session_resume_data(self->session, stream_id);
    if (rv != 0 && PyErr_Occurred()) {
        PyErr_Clear();
    }
    return drain_send(self->session);
}

static PyObject *
session_submit_response(PyObject *self_obj, PyObject *args, PyObject *kwds)
{
    PyHTTP2Session *self = (PyHTTP2Session *)self_obj;
    int32_t stream_id = 0;
    PyObject *headers = NULL;
    int with_body = 0;
    static char *kwlist[] = {"stream_id", "headers", "with_body", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "iO!|p", kwlist,
                                     &stream_id, &PyList_Type, &headers,
                                     &with_body))
        return NULL;

    nghttp2_nv *nva = NULL;
    size_t nvlen = 0;
    if (make_nv_array(headers, &nva, &nvlen) < 0) return NULL;

    /* The response header block must carry the :status pseudo header.
     * NULL provider => HEADERS carry END_STREAM (empty response body).
     * With a body, the same per-stream collector mechanism as for
     * requests applies (submit_data feeds it). */
    nghttp2_data_provider provider;
    nghttp2_data_provider *provider_ptr = NULL;
    py_stream_body *body = NULL;
    if (with_body) {
        if (body_find(self, stream_id) != NULL) {
            PyMem_Free(nva);
            PyErr_SetString(PyExc_ValueError,
                            "stream already has an open body collector");
            return NULL;
        }
        body = body_collector_new(self);
        if (body == NULL) {
            PyMem_Free(nva);
            return NULL;
        }
        body->stream_id = stream_id;
        provider.source.ptr = body;
        provider.read_callback = body_read_callback;
        provider_ptr = &provider;
    }
    int rv = nghttp2_submit_response(self->session, stream_id, nva, nvlen,
                                     provider_ptr);
    PyMem_Free(nva);
    if (rv != 0) {
        if (body != NULL) body_free(self, body);
        PyErr_Format(PyExc_RuntimeError,
                     "nghttp2_submit_response failed: %d", rv);
        return NULL;
    }
    return drain_send(self->session);
}

static PyObject *
session_submit_headers(PyObject *self_obj, PyObject *args)
{
    PyHTTP2Session *self = (PyHTTP2Session *)self_obj;
    int32_t stream_id = 0;
    PyObject *headers = NULL;
    int end_stream = 0;
    if (!PyArg_ParseTuple(args, "iO!p", &stream_id, &PyList_Type, &headers,
                          &end_stream))
        return NULL;
    nghttp2_nv *nva = NULL;
    size_t nvlen = 0;
    if (make_nv_array(headers, &nva, &nvlen) < 0) return NULL;
    int rv = nghttp2_submit_headers(
        self->session, end_stream ? (uint8_t)NGHTTP2_FLAG_END_STREAM : 0,
        stream_id, NULL, nva, nvlen, NULL);
    PyMem_Free(nva);
    if (rv != 0) {
        PyErr_Format(PyExc_RuntimeError,
                     "nghttp2_submit_headers failed: %d", rv);
        return NULL;
    }
    return drain_send(self->session);
}

static PyObject *
session_submit_trailer(PyObject *self_obj, PyObject *args)
{
    PyHTTP2Session *self = (PyHTTP2Session *)self_obj;
    int32_t stream_id = 0;
    PyObject *headers = NULL;
    if (!PyArg_ParseTuple(args, "iO!", &stream_id, &PyList_Type, &headers))
        return NULL;
    nghttp2_nv *nva = NULL;
    size_t nvlen = 0;
    if (make_nv_array(headers, &nva, &nvlen) < 0) return NULL;
    int rv = nghttp2_submit_trailer(self->session, stream_id, nva, nvlen);
    PyMem_Free(nva);
    if (rv != 0) {
        PyErr_Format(PyExc_RuntimeError,
                     "nghttp2_submit_trailer failed: %d", rv);
        return NULL;
    }
    return drain_send(self->session);
}

static PyObject *
session_submit_settings(PyObject *self_obj, PyObject *args)
{
    PyHTTP2Session *self = (PyHTTP2Session *)self_obj;
    PyObject *settings = NULL;
    if (!PyArg_ParseTuple(args, "O!", &PyDict_Type, &settings)) return NULL;

    PyObject *items = PyDict_Items(settings);
    if (items == NULL) return NULL;
    Py_ssize_t n = PyList_GET_SIZE(items);
    nghttp2_settings_entry *entries =
        PyMem_Malloc(sizeof(nghttp2_settings_entry) * (size_t)(n > 0 ? n : 1));
    if (entries == NULL) {
        Py_DECREF(items);
        PyErr_NoMemory();
        return NULL;
    }
    for (Py_ssize_t i = 0; i < n; ++i) {
        PyObject *pair = PyList_GET_ITEM(items, i); /* borrowed */
        long id = PyLong_AsLong(PyTuple_GET_ITEM(pair, 0));
        unsigned long value = PyLong_AsUnsignedLong(PyTuple_GET_ITEM(pair, 1));
        if ((id == -1 || value == (unsigned long)-1) && PyErr_Occurred()) {
            PyMem_Free(entries);
            Py_DECREF(items);
            return NULL;
        }
        entries[i].settings_id = (int32_t)id;
        entries[i].value = (uint32_t)value;
    }
    Py_DECREF(items);
    int rv = nghttp2_submit_settings(self->session, NGHTTP2_FLAG_NONE,
                                     entries, (size_t)n);
    PyMem_Free(entries);
    if (rv != 0) {
        PyErr_Format(PyExc_RuntimeError,
                     "nghttp2_submit_settings failed: %d", rv);
        return NULL;
    }
    return drain_send(self->session);
}

static PyObject *
session_submit_ping(PyObject *self_obj, PyObject *args)
{
    PyHTTP2Session *self = (PyHTTP2Session *)self_obj;
    Py_buffer view;
    if (!PyArg_ParseTuple(args, "y*", &view)) return NULL;
    if (view.len != 8) {
        PyBuffer_Release(&view);
        PyErr_SetString(PyExc_ValueError, "ping opaque_data must be 8 bytes");
        return NULL;
    }
    int rv = nghttp2_submit_ping(self->session, NGHTTP2_FLAG_NONE,
                                 (const uint8_t *)view.buf);
    PyBuffer_Release(&view);
    if (rv != 0) {
        PyErr_Format(PyExc_RuntimeError, "nghttp2_submit_ping failed: %d", rv);
        return NULL;
    }
    return drain_send(self->session);
}

static PyObject *
session_submit_goaway(PyObject *self_obj, PyObject *args)
{
    PyHTTP2Session *self = (PyHTTP2Session *)self_obj;
    int last_stream_id = 0;
    unsigned long error_code = 0;
    Py_buffer view = {0};
    if (!PyArg_ParseTuple(args, "ik|y*", &last_stream_id, &error_code, &view))
        return NULL;
    int rv = nghttp2_submit_goaway(
        self->session, NGHTTP2_FLAG_NONE, (int32_t)last_stream_id,
        (uint32_t)error_code, (const uint8_t *)view.buf, (size_t)view.len);
    PyBuffer_Release(&view);
    if (rv != 0) {
        PyErr_Format(PyExc_RuntimeError,
                     "nghttp2_submit_goaway failed: %d", rv);
        return NULL;
    }
    return drain_send(self->session);
}

static PyObject *
session_submit_window_update(PyObject *self_obj, PyObject *args)
{
    PyHTTP2Session *self = (PyHTTP2Session *)self_obj;
    int stream_id = 0;
    int increment = 0;
    if (!PyArg_ParseTuple(args, "ii", &stream_id, &increment)) return NULL;
    int rv = nghttp2_submit_window_update(self->session, NGHTTP2_FLAG_NONE,
                                          (int32_t)stream_id,
                                          (int32_t)increment);
    if (rv != 0) {
        PyErr_Format(PyExc_RuntimeError,
                     "nghttp2_submit_window_update failed: %d", rv);
        return NULL;
    }
    return drain_send(self->session);
}

static PyObject *
session_submit_rst_stream(PyObject *self_obj, PyObject *args)
{
    PyHTTP2Session *self = (PyHTTP2Session *)self_obj;
    int stream_id = 0;
    unsigned long error_code = 0;
    if (!PyArg_ParseTuple(args, "ik", &stream_id, &error_code)) return NULL;
    int rv = nghttp2_submit_rst_stream(self->session, NGHTTP2_FLAG_NONE,
                                       (int32_t)stream_id,
                                       (uint32_t)error_code);
    if (rv != 0) {
        PyErr_Format(PyExc_RuntimeError,
                     "nghttp2_submit_rst_stream failed: %d", rv);
        return NULL;
    }
    return drain_send(self->session);
}

static PyObject *
session_submit_priority_update(PyObject *self_obj, PyObject *args)
{
    PyHTTP2Session *self = (PyHTTP2Session *)self_obj;
    int stream_id = 0;
    Py_buffer view;
    if (!PyArg_ParseTuple(args, "iy*", &stream_id, &view)) return NULL;
    int rv = nghttp2_submit_priority_update(self->session, NGHTTP2_FLAG_NONE,
                                            (int32_t)stream_id,
                                            (const uint8_t *)view.buf,
                                            (size_t)view.len);
    PyBuffer_Release(&view);
    if (rv != 0) {
        PyErr_Format(PyExc_RuntimeError,
                     "nghttp2_submit_priority_update failed: %d", rv);
        return NULL;
    }
    return drain_send(self->session);
}

static PyObject *
session_submit_shutdown_notice(PyObject *self_obj,
                               PyObject *Py_UNUSED(ignored))
{
    PyHTTP2Session *self = (PyHTTP2Session *)self_obj;
    int rv = nghttp2_submit_shutdown_notice(self->session);
    if (rv != 0) {
        PyErr_Format(PyExc_RuntimeError,
                     "nghttp2_submit_shutdown_notice failed: %d", rv);
        return NULL;
    }
    return drain_send(self->session);
}

static PyObject *
session_next_stream_id(PyObject *self_obj, PyObject *Py_UNUSED(ignored))
{
    PyHTTP2Session *self = (PyHTTP2Session *)self_obj;
    return PyLong_FromLong(nghttp2_session_get_next_stream_id(self->session));
}

static PyObject *
settings_as_dict(PyHTTP2Session *self,
                 uint32_t (*getter)(nghttp2_session *,
                                    nghttp2_settings_id))
{
    PyObject *result = PyDict_New();
    if (result == NULL) return NULL;
    static const nghttp2_settings_id ids[] = {
        NGHTTP2_SETTINGS_HEADER_TABLE_SIZE,
        NGHTTP2_SETTINGS_ENABLE_PUSH,
        NGHTTP2_SETTINGS_MAX_CONCURRENT_STREAMS,
        NGHTTP2_SETTINGS_INITIAL_WINDOW_SIZE,
        NGHTTP2_SETTINGS_MAX_FRAME_SIZE,
        NGHTTP2_SETTINGS_MAX_HEADER_LIST_SIZE,
    };
    for (size_t i = 0; i < sizeof(ids) / sizeof(ids[0]); ++i) {
        PyObject *key = PyLong_FromLong((long)ids[i]);
        PyObject *value = PyLong_FromUnsignedLong(getter(self->session, ids[i]));
        if (key == NULL || value == NULL ||
            PyDict_SetItem(result, key, value) < 0) {
            Py_XDECREF(key);
            Py_XDECREF(value);
            Py_DECREF(result);
            return NULL;
        }
        Py_DECREF(key);
        Py_DECREF(value);
    }
    return result;
}

static PyObject *
session_get_local_settings(PyObject *self_obj, PyObject *Py_UNUSED(ignored))
{
    PyHTTP2Session *self = (PyHTTP2Session *)self_obj;
    return settings_as_dict(self, nghttp2_session_get_local_settings);
}

static PyObject *
session_get_remote_settings(PyObject *self_obj, PyObject *Py_UNUSED(ignored))
{
    PyHTTP2Session *self = (PyHTTP2Session *)self_obj;
    return settings_as_dict(self, nghttp2_session_get_remote_settings);
}

/* === Module === */

static PyMethodDef session_methods[] = {
    {"recv", session_recv, METH_O,
     "Feed inbound bytes; return (events, outbound_frames)."},
    {"submit_request", (PyCFunction)(void (*)(void))session_submit_request,
     METH_VARARGS | METH_KEYWORDS,
     "submit_request(headers, with_body=False) -> (stream_id, frames).\n\n"
     "Submit request HEADERS. END_STREAM is set unless with_body=True."},
    {"submit_data", (PyCFunction)(void (*)(void))session_submit_data,
     METH_VARARGS | METH_KEYWORDS,
     "submit_data(stream_id, data, end_stream) -> frames."},
    {"submit_response", (PyCFunction)(void (*)(void))session_submit_response,
     METH_VARARGS | METH_KEYWORDS,
     "submit_response(stream_id, headers, with_body=False) -> frames (server only)."},
    {"submit_headers", session_submit_headers, METH_VARARGS,
     "submit_headers(stream_id, headers, end_stream=False) -> frames."},
    {"submit_trailer", session_submit_trailer, METH_VARARGS,
     "submit_trailer(stream_id, headers) -> frames."},
    {"submit_settings", session_submit_settings, METH_VARARGS,
     "submit_settings({setting_id: value}) -> frames."},
    {"submit_ping", session_submit_ping, METH_VARARGS,
     "submit_ping(opaque_data_8_bytes) -> frames."},
    {"submit_goaway", session_submit_goaway, METH_VARARGS,
     "submit_goaway(last_stream_id, error_code, debug_data=b'') -> frames."},
    {"submit_window_update", session_submit_window_update, METH_VARARGS,
     "submit_window_update(stream_id, increment) -> frames."},
    {"submit_rst_stream", session_submit_rst_stream, METH_VARARGS,
     "submit_rst_stream(stream_id, error_code) -> frames."},
    {"submit_priority_update", session_submit_priority_update, METH_VARARGS,
     "submit_priority_update(stream_id, field_value) -> frames (RFC 9218)."},
    {"submit_shutdown_notice", session_submit_shutdown_notice, METH_NOARGS,
     "submit_shutdown_notice() -> frames (first GOAWAY of a graceful shutdown)."},
    {"next_stream_id", session_next_stream_id, METH_NOARGS,
     "Next client-initiated stream ID."},
    {"get_local_settings", session_get_local_settings, METH_NOARGS,
     "Local SETTINGS as {setting_id: value}."},
    {"get_remote_settings", session_get_remote_settings, METH_NOARGS,
     "Peer SETTINGS as {setting_id: value}."},
    {NULL, NULL, 0, NULL},
};

static PyTypeObject PyHTTP2Session_Type = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "geventhttpclient._http2_parser.Session",
    .tp_basicsize = sizeof(PyHTTP2Session),
    .tp_dealloc = session_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_doc = "Sans-IO HTTP/2 client session backed by nghttp2.",
    .tp_methods = session_methods,
};

static PyMethodDef module_methods[] = {
    {"session_client_new", session_client_new, METH_NOARGS,
     "Create a new client-side HTTP/2 session."},
    {"session_server_new", session_server_new, METH_NOARGS,
     "Create a new server-side HTTP/2 session (for testing)."},
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef module_def = {
    PyModuleDef_HEAD_INIT,
    "geventhttpclient._http2_parser",
    "HTTP/2 sans-IO wrapper around the vendored nghttp2 library.",
    -1,
    module_methods,
};

PyMODINIT_FUNC
PyInit__http2_parser(void)
{
    KIND_HEADERS = PyUnicode_InternFromString("headers");
    KIND_DATA = PyUnicode_InternFromString("data");
    KIND_STREAM_RESET = PyUnicode_InternFromString("stream_reset");
    KIND_STREAM_CLOSED = PyUnicode_InternFromString("stream_closed");
    KIND_SETTINGS = PyUnicode_InternFromString("settings");
    KIND_PING = PyUnicode_InternFromString("ping");
    KIND_GOAWAY = PyUnicode_InternFromString("goaway");
    KIND_WINDOW_UPDATE = PyUnicode_InternFromString("window_update");
    if (KIND_HEADERS == NULL || KIND_DATA == NULL ||
        KIND_STREAM_RESET == NULL || KIND_STREAM_CLOSED == NULL ||
        KIND_SETTINGS == NULL || KIND_PING == NULL ||
        KIND_GOAWAY == NULL || KIND_WINDOW_UPDATE == NULL) {
        return NULL;
    }

    if (PyType_Ready(&PyHTTP2Session_Type) < 0) return NULL;
    PyObject *module = PyModule_Create(&module_def);
    if (module == NULL) return NULL;
    Py_INCREF(&PyHTTP2Session_Type);
    if (PyModule_AddObject(module, "Session",
                           (PyObject *)&PyHTTP2Session_Type) < 0) {
        Py_DECREF(&PyHTTP2Session_Type);
        Py_DECREF(module);
        return NULL;
    }
    return module;
}
