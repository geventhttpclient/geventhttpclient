import sys
from distutils.core import setup
from setuptools.extension import Extension
from setuptools import find_packages

DESC = """
A high performance, concurrent HTTP client library for python using gevent.

geventhttpclient use a fast http parser, written in C, originating from nginx,
extracted and modified by Joyent.

geventhttpclient has been specifically designed for high concurrency,
streaming and support HTTP 1.1 persistent connections. More generally it is
designed for efficiently pulling from REST APIs and streaming API's
like Twitter's.

Safe SSL support is provided by default.

Python 2.6 and 2.7 are supported as well as gevent 0.13 and gevent 1.0.

Use of SSL/TLS with python 2.7.9 is not recommanded and may be broken.
"""


httpparser = Extension('geventhttpclient._parser',
                    sources = ['ext/_parser.c', 'ext/http_parser.c'],
                    include_dirs = ['ext'])

requirements = [
    'gevent >= 0.13',
    'certifi',
]

if sys.hexversion < 0x02070900:
    requirements += [
        'backports.ssl_match_hostname',
    ]


setup(name='geventhttpclient',
      version = '1.2.0',
      description = 'http client library for gevent',
      long_description = DESC,
      url="http://github.com/gwik/geventhttpclient",
      author="Antonin Amand",
      author_email="antonin.amand@gmail.com",
      packages=find_packages('src'),
      license='LICENSE-MIT',
      package_dir={'': 'src'},
      ext_modules = [httpparser],
      include_package_data=True,
      install_requires=requirements)
