#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include "Python_compat.h"
#include <llhttp.h>
#include <stdio.h>

static PyObject * PyExc_HTTPParseError;

enum py_parser_should_keep_alive {
    KA_INCOMPLETE,
    KA_FALSE,
    KA_TRUE,
};

typedef struct {
    PyObject_HEAD
    llhttp_t* parser;
    llhttp_errno_t error;
    const char* reason;
    enum py_parser_should_keep_alive should_keep_alive;
} PyHTTPResponseParser;

static int on_message_begin(llhttp_t* parser)
{
    int fail = 0;
    PyHTTPResponseParser *self = (PyHTTPResponseParser*) parser->data;
    PyObject* callable = PyObject_GetAttrString((PyObject*)self, "_on_message_begin");
    if (callable) {
        PyObject* result = PyObject_CallObject(callable, NULL);
        PyObject* exception = PyErr_Occurred();
        if (exception != NULL) {
            fail = -1;
        } else {
            if (PyObject_IsTrue(result))
                fail = -1;
        }
        Py_XDECREF(result);
        Py_DECREF(callable);
    }
    return fail;
}

static int on_message_complete(llhttp_t* parser)
{
    int fail = 0;
    PyHTTPResponseParser *self = (PyHTTPResponseParser*) parser->data;
    self->should_keep_alive = llhttp_should_keep_alive(parser) ? KA_TRUE : KA_FALSE;
    PyObject* callable = PyObject_GetAttrString((PyObject*)self, "_on_message_complete");
    if (callable) {
        PyObject* result = PyObject_CallObject(callable, NULL);
        PyObject* exception = PyErr_Occurred();
        if (exception != NULL) {
            fail = -1;
        } else {
            if (PyObject_IsTrue(result))
                fail = -1;
        }
        Py_XDECREF(result);
        Py_DECREF(callable);
    }
    return fail;
}

static int on_headers_complete(llhttp_t* parser)
{
    /* 1 => skip body, 2 => upgrade, 0 => continue, -1 => error */
    int skip_body = 0;
    PyHTTPResponseParser *self = (PyHTTPResponseParser*) parser->data;
    PyObject* callable = PyObject_GetAttrString((PyObject*)self, "_on_headers_complete");
    if (callable) {
        PyObject* result = PyObject_CallObject(callable, NULL);
        PyObject* exception = PyErr_Occurred();
        if (exception != NULL) {
            skip_body = -1;
        } else if (PyObject_IsTrue(result)) {
            skip_body = 1;
        }
        Py_XDECREF(result);
        Py_DECREF(callable);
    }
    return skip_body;
}

static int on_http_data_cb(llhttp_t* parser, const char *at, size_t length, const char * python_cb)
{
    int fail = 0;
    PyHTTPResponseParser *self = (PyHTTPResponseParser*) parser->data;
    PyObject* callable = PyObject_GetAttrString((PyObject*)self, python_cb);
    if (callable) {
        PyObject* args = Py_BuildValue("(s#)", at, length);
        PyObject* result = PyObject_CallObject(callable, args);
        PyObject* exception = PyErr_Occurred();
        if (exception != NULL) {
            fail = -1;
        } else {
            if (PyObject_IsTrue(result))
                fail = -1;
        }
        Py_XDECREF(result);
        Py_DECREF(callable);
        Py_DECREF(args);
    }
    return fail;
}

static int on_status(llhttp_t* parser, const char *at, size_t length)
{
    return on_http_data_cb(parser, at, length, "_on_status");
}

static int on_header_field(llhttp_t* parser, const char *at, size_t length)
{
    return on_http_data_cb(parser, at, length, "_on_header_field");
}

static int on_header_value(llhttp_t* parser, const char *at, size_t length)
{
    return on_http_data_cb(parser, at, length, "_on_header_value");
}

static int on_body(llhttp_t* parser, const char *at, size_t length)
{
    int fail = 0;
    PyHTTPResponseParser *self = (PyHTTPResponseParser*) parser->data;
    PyObject* callable = PyObject_GetAttrString((PyObject*)self, "_on_body");
    if (callable) {
        PyObject* bytearray = PyByteArray_FromStringAndSize(at, length);
        PyObject* result = PyObject_CallFunctionObjArgs(
            callable, bytearray, NULL);
        PyObject* exception = PyErr_Occurred();
        if (exception != NULL) {
            fail = -1;
        } else {
            if (PyObject_IsTrue(result))
                fail = -1;
        }
        Py_XDECREF(result);
        Py_DECREF(callable);
        Py_DECREF(bytearray);
    }
    return fail;
}

static llhttp_settings_t _parser_settings = {
    on_message_begin,
    NULL, // on_url
    on_status,
    on_header_field,
    on_header_value,
    on_headers_complete,
    on_body,
    on_message_complete
};

static PyObject*
PyHTTPResponseParser_new(PyTypeObject* type, PyObject* args, PyObject* kwds)
{
    PyHTTPResponseParser* self = (PyHTTPResponseParser*)type->tp_alloc(type, 0);
    if (self != NULL) {
        self->parser = PyMem_Malloc(sizeof(llhttp_t));
        if (self->parser == NULL) {
            return NULL;
        } else {
            llhttp_init(self->parser, HTTP_RESPONSE, &_parser_settings);
            self->parser->data = (void*)self;
            self->error = HPE_OK;
            self->reason = 0;
            self->should_keep_alive = KA_INCOMPLETE;
        }
    }
    return (PyObject*) self;
}

static void* set_parser_exception(PyHTTPResponseParser *parser)
{
    PyObject *args = Py_BuildValue("(s,B)", parser->reason, parser->error);
    if (args == NULL) return PyErr_NoMemory();
    PyErr_SetObject(PyExc_HTTPParseError, args);
    Py_DECREF(args);
    return NULL;
}

static size_t size_t_MAX = -1;

static PyObject*
PyHTTPResponseParser_feed(PyHTTPResponseParser *self, PyObject* args)
{
    char* buf = NULL;
    Py_ssize_t buf_len;
    int succeed = PyArg_ParseTuple(args, "s#", &buf, &buf_len);
    /* cast Py_ssize_t signed integer to unsigned */
    size_t unsigned_buf_len = buf_len + size_t_MAX + 1;
    if (succeed) {
        PyObject * exception;

        /* in case feed is called again after an error occurred */
        if (self->error != HPE_OK) {
            return set_parser_exception(self);
        }

        if (buf_len) {
            self->error = llhttp_execute(self->parser, buf, unsigned_buf_len);
            self->reason = self->parser->reason;
        } else {
            if (!llhttp_message_needs_eof(self->parser)) {
                PyErr_SetString(PyExc_HTTPParseError, "Incomplete response.");
                return NULL;
            }
            self->error = llhttp_finish(self->parser);
            self->reason = self->parser->reason;
        }

        /* Exception in callbacks */
        exception = PyErr_Occurred();
        if (exception != NULL)
            return NULL;

        if (self->error != HPE_OK) {
            return set_parser_exception(self);
        }

        Py_RETURN_NONE;
    }
    return NULL;
}

static PyObject*
PyHTTPResponseParser_parser_failed(PyHTTPResponseParser* self)
{
    return PyBool_FromLong(self->error != HPE_OK || llhttp_get_errno(self->parser) != HPE_OK);
}

#if PY_MAJOR_VERSION >= 3
static PyObject*
PyHTTPResponseParser_get_http_version(PyHTTPResponseParser *self)
{
    return PyUnicode_FromFormat("HTTP/%u.%u", self->parser->http_major,
        self->parser->http_minor);
}
#else
static PyObject*
PyHTTPResponseParser_get_http_version(PyHTTPResponseParser *self)
{
    return PyString_FromFormat("HTTP/%u.%u", self->parser->http_major,
        self->parser->http_minor);
}
#endif

static PyObject*
PyHTTPResponseParser_get_remaining_content_length(PyHTTPResponseParser *self)
{
    if (sizeof(signed long long) == 8)
        return Py_BuildValue("L", self->parser->content_length);
    if (sizeof(signed long) == 8)
        return Py_BuildValue("l", self->parser->content_length);
    // int
    return Py_BuildValue("i", self->parser->content_length);
}

static PyObject*
PyHTTPResponseParser_get_code(PyHTTPResponseParser *self)
{
    return Py_BuildValue("i", self->parser->status_code);
}

static PyObject*
PyHTTPResponseParser_should_keep_alive(PyHTTPResponseParser* self)
{
    if (self->error != HPE_OK) {
        Py_RETURN_FALSE;
    }
    int should_keep_alive = 0;
    switch (self->should_keep_alive) {
        case KA_INCOMPLETE:
            should_keep_alive = llhttp_should_keep_alive(self->parser);
            break;
        case KA_FALSE:
            should_keep_alive = 0;
            break;
        case KA_TRUE:
            should_keep_alive = 1;
            break;
    }
    return PyBool_FromLong(should_keep_alive);
}

void
PyHTTPResponseParser_dealloc(PyHTTPResponseParser* self)
{
    self->parser->data = NULL;
    PyMem_Free(self->parser);
    Py_TYPE(self)->tp_free((PyObject*)self);
}

static PyMethodDef PyHTTPResponseParser_methods[] = {
    {"feed", (PyCFunction)PyHTTPResponseParser_feed, METH_VARARGS,
        "Feed the parser with data"},
    {"get_code", (PyCFunction)PyHTTPResponseParser_get_code, METH_NOARGS,
        "Get http response code"},
    {"get_http_version", (PyCFunction)PyHTTPResponseParser_get_http_version, METH_NOARGS,
        "Get http version"},
    {"get_remaining_content_length",
        (PyCFunction)PyHTTPResponseParser_get_remaining_content_length,
        METH_NOARGS,
        "Get remaining content length to read"},
    {"should_keep_alive", (PyCFunction)PyHTTPResponseParser_should_keep_alive,
        METH_NOARGS,
        "Tell whether the connection should stay connected (HTTP 1.1)"},
    {"parser_failed", (PyCFunction)PyHTTPResponseParser_parser_failed,
        METH_NOARGS,
        "Tell if parser have failed."},
    {NULL}  /* Sentinel */
};

static PyTypeObject HTTPParserType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    "HTTPResponseParser",      /*tp_name*/
    sizeof(PyHTTPResponseParser),      /*tp_basicsize*/
    0,                         /*tp_itemsize*/
    (destructor)PyHTTPResponseParser_dealloc, /*tp_dealloc*/
    0,                         /*tp_print*/
    0,                         /*tp_getattr*/
    0,                         /*tp_setattr*/
    0,                         /*tp_compare*/
    0,                         /*tp_repr*/
    0,                         /*tp_as_number*/
    0,                         /*tp_as_sequence*/
    0,                         /*tp_as_mapping*/
    0,                         /*tp_hash */
    0,                         /*tp_call*/
    0,                         /*tp_str*/
    0,                         /*tp_getattro*/
    0,                         /*tp_setattro*/
    0,                         /*tp_as_buffer*/
    Py_TPFLAGS_DEFAULT | Py_TPFLAGS_BASETYPE, /*tp_flags*/
    "HTTP Response parser (non thread-safe)",           /* tp_doc */
    0,                         /* tp_traverse */
    0,                         /* tp_clear */
    0,                         /* tp_richcompare */
    0,                         /* tp_weaklistoffset */
    0,                         /* tp_iter */
    0,                         /* tp_iternext */
    PyHTTPResponseParser_methods,      /* tp_methods */
    0,                         /* tp_members */
    0,                         /* tp_getset */
    0,                         /* tp_base */
    0,                         /* tp_dict */
    0,                         /* tp_descr_get */
    0,                         /* tp_descr_set */
    0,                         /* tp_dictoffset */
    0,                         /* tp_init */
    0,                         /* tp_alloc */
    PyHTTPResponseParser_new,                 /* tp_new */
};

static PyMethodDef module_methods[] = {
    {NULL}  /* Sentinel */
};

#if PY_MAJOR_VERSION >= 3
static struct PyModuleDef moduledef = {
        PyModuleDef_HEAD_INIT,
        "_parser",
        "HTTP Parser from nginx/Joyent.",
        0,
        module_methods,
        NULL,
        NULL,
        NULL,
        NULL
};

#define INITERROR return NULL
PyMODINIT_FUNC
PyInit__parser(void)

#else
#define INITERROR return
void
init_parser(void)
#endif
{
    PyObject *module, *httplib, *HTTPException;

    if (PyType_Ready(&HTTPParserType) < 0)
        INITERROR;

    #if PY_MAJOR_VERSION >= 3
    module = PyModule_Create(&moduledef);
    #else
    module = Py_InitModule3("_parser", module_methods,
                       "HTTP Parser from nginx/Joyent.");
    #endif

    Py_INCREF(&HTTPParserType);
    PyModule_AddObject(module, "HTTPResponseParser", (PyObject *)&HTTPParserType);

    #if PY_MAJOR_VERSION >= 3
    httplib = PyImport_ImportModule("http.client");
    #else
    httplib = PyImport_ImportModule("httplib");
    #endif
    HTTPException = PyObject_GetAttrString(httplib, "HTTPException");

    PyExc_HTTPParseError = PyErr_NewException(
            "_parser.HTTPParseError", HTTPException, NULL);
    Py_INCREF(PyExc_HTTPParseError);
    PyModule_AddObject(module, "HTTPParseError", PyExc_HTTPParseError);
    #if PY_MAJOR_VERSION >= 3
    return  module;
    #endif
}

#undef PY_SSIZE_T_CLEAN
