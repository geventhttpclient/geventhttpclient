import sys
from setuptools.extension import Extension
from setuptools import find_packages
from distutils.core import setup

DESC = """
A high performance, concurrent HTTP client library for python using gevent.

gevent.httplib support was removed in gevent 1.0, geventhttpclient now
provides that missing functionality.

geventhttpclient uses a fast http parser, written in C, originating from
nginx, extracted and modified by Joyent.

geventhttpclient has been specifically designed for high concurrency,
streaming and support HTTP 1.1 persistent connections. More generally it is
designed for efficiently pulling from REST APIs and streaming APIs
like Twitter's.

Safe SSL support is provided by default. geventhttpclient depends on
the certifi CA Bundle. This is the same CA Bundle which ships with the
Requests codebase, and is derived from Mozilla Firefox's canonical set.

As of 1.5, only Python 3.6+ is fully supported (with prebuilt wheels), 
but Python 2.7 and 3.5 *should* work too.

Use of SSL/TLS with python 2.7.9 is not recommended and may be broken.
"""

httpparser = Extension(
    'geventhttpclient._parser',
    sources=[
        'ext/_parser.c',
        'llhttp/src/api.c',
        'llhttp/src/http.c',
        'llhttp/src/llhttp.c',
    ],
    include_dirs=[
        'llhttp/include',
    ],
)

requirements = [
    'gevent >= 0.13',
    'certifi',
    'six',
    'brotli'
]

if sys.hexversion < 0x02070900:
    requirements += [
        'backports.ssl_match_hostname',
    ]

setup(name='geventhttpclient',
      version = "2.0.7", # dont forget to update version in __init__.py as well
      description = 'http client library for gevent',
      long_description = DESC,
      url="http://github.com/gwik/geventhttpclient",
      author="Antonin Amand",
      author_email="antonin.amand@gmail.com",
      packages=find_packages('src'),
      exclude_package_data={'geventhttpclient': ['tests/*']},
      license='MIT',
      package_dir={'': 'src'},
      ext_modules = [httpparser],
      include_package_data=True,
      install_requires=requirements)
