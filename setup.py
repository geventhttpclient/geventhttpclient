from distutils.core import setup

from setuptools.extension import Extension

http_parser = Extension(
    "geventhttpclient._parser",
    sources=[
        "ext/_parser.c",
        "llhttp/src/api.c",
        "llhttp/src/http.c",
        "llhttp/src/llhttp.c",
    ],
    include_dirs=[
        "ext",
        "llhttp/include",
    ],
)

setup(
    ext_modules=[http_parser],
)
