#include <Python.h>
#include <http_parser.h>
#include <stdio.h> //printf

static int on_message_begin(http_parser* parser)
{
    PyObject* self = (PyObject*)parser->data;
    if (PyObject_HasAttrString(self, "on_message_begin")) {
        // XXX: New reference.
        PyObject* callable = PyObject_GetAttrString(self, "on_message_begin");
        PyObject_CallObject(callable, NULL);
    }
    return 0;
}

static int on_message_complete(http_parser* parser)
{
    PyObject* self = (PyObject*)parser->data;
    if (PyObject_HasAttrString(self, "on_message_complete")) {
        // XXX: New reference.
        // 
        PyObject* callable = PyObject_GetAttrString(self, "on_message_complete");
        PyObject_CallObject(callable, NULL);
    }
    return 0;
}

static int on_headers_complete(http_parser* parser)
{
    PyObject* self = (PyObject*)parser->data;
    if (PyObject_HasAttrString(self, "on_headers_complete")) {
        // XXX: New reference.
        PyObject* callable = PyObject_GetAttrString(self, "on_headers_complete");
        PyObject_CallObject(callable, NULL);
    }
    return 0;
}

static int on_url(http_parser* parser, const char *at, size_t length)
{
    return 0;
}

static int on_header_field(http_parser* parser, const char *at, size_t length)
{
    PyObject* self = (PyObject*)parser->data;
    if (PyObject_HasAttrString(self, "on_header_field")) {
        // XXX: New reference.
        PyObject* callable = PyObject_GetAttrString(self, "on_header_field");
        PyObject* args = Py_BuildValue("(s#)", at, length);
        PyObject_CallObject(callable, args);
    }
    return 0;
}

static int on_header_value(http_parser* parser, const char *at, size_t length)
{
    PyObject* self = (PyObject*)parser->data;
    if (PyObject_HasAttrString(self, "on_header_value")) {
        // XXX: New reference.
        PyObject* callable = PyObject_GetAttrString(self, "on_header_value");
        PyObject* args = Py_BuildValue("(s#)", at, length);
        PyObject_CallObject(callable, args);
    }
    return 0;
}

static int on_body(http_parser* parser, const char *at, size_t length)
{
    PyObject* self = (PyObject*)parser->data;
    if (PyObject_HasAttrString(self, "on_body")) {
        // XXX: New reference.
        PyObject* callable = PyObject_GetAttrString(self, "on_body");
        PyObject* args = Py_BuildValue("(s#)", at, length);
        PyObject_CallObject(callable, args);
    }
    return 0;
}

static http_parser_settings _parser_settings = {
    on_message_begin,
    on_url,
    on_header_field,
    on_header_value,
    on_headers_complete,
    on_body,
    on_message_complete
};

typedef struct {
    PyObject_HEAD
    http_parser* parser;
    http_parser_settings* settings;
} PyHTTPResponseParser;

static PyObject*
PyHTTPResponseParser_new(PyTypeObject* type, PyObject* args, PyObject* kwds)
{
    PyHTTPResponseParser* self = (PyHTTPResponseParser*)type->tp_alloc(type, 0);
    if (self != NULL) {
        self->parser = PyMem_Malloc(sizeof(http_parser));
        if (self->parser == NULL) {
            return NULL;
        } else {
            self->parser->data = (void*)self;
            http_parser_init(self->parser, HTTP_RESPONSE);
        }
        self->settings = PyMem_Malloc(sizeof(http_parser_settings));
        if (self->settings == NULL)
            return NULL;
    }
    return (PyObject*) self;
}

static PyObject*
PyHTTPResponseParser_feed(PyHTTPResponseParser *self, PyObject* args)
{
    char* buf;
    Py_ssize_t buf_len;
    int succeed = PyArg_ParseTuple(args, "s#", &buf, &buf_len);
    if (succeed) {
        http_parser_execute(self->parser,
                &_parser_settings, buf, (size_t)buf_len);
    }
    Py_RETURN_NONE;
}

void
PyHTTPResponseParser_dealloc(PyHTTPResponseParser* self)
{
    PyMem_Free(self->parser);
    PyMem_Free(self->settings);
    self->parser = NULL;
    self->settings = NULL;
}

static PyMethodDef PyHTTPResponseParser_methods[] = {
    {"feed", PyHTTPResponseParser_feed, METH_VARARGS,
        "Feed the parser with data"},
    {NULL}  /* Sentinel */
};

static PyTypeObject HTTPParserType = {
    PyObject_HEAD_INIT(NULL)
    0,                         /*ob_size*/
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
    "HTTP Response Parser instance (non thread-safe)",           /* tp_doc */
    0,		               /* tp_traverse */
    0,		               /* tp_clear */
    0,		               /* tp_richcompare */
    0,		               /* tp_weaklistoffset */
    0,		               /* tp_iter */
    0,		               /* tp_iternext */
    PyHTTPResponseParser_methods,      /* tp_methods */
    0,             /* tp_members */
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

#ifndef PyMODINIT_FUNC	/* declarations for DLL import/export */
#define PyMODINIT_FUNC void
#endif
PyMODINIT_FUNC
init_parser(void) 
{
    PyObject* module;

    if (PyType_Ready(&HTTPParserType) < 0)
        return;

    module = Py_InitModule3("_parser", module_methods,
                       "HTTP Parser from Joyent.");

    Py_INCREF(&HTTPParserType);
    PyModule_AddObject(module, "HTTPResponseParser", (PyObject *)&HTTPParserType);
}

