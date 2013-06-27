import sys
from distutils.core import setup
from setuptools.extension import Extension
from setuptools import find_packages

parser_sources = ['ext/http_parser.c']
if '__pypy__' not in sys.builtin_module_names:
    # Normal CPython module will be built
    parser_sources.append('ext/_parser.c')
    extension_name = 'geventhttpclient._parser'
else:
    # CFFI helper module will be built (doesn't use CPython API)
    extension_name = 'geventhttpclient._cffi__parser_helper'

httpparser = Extension(extension_name,
                       sources = parser_sources,
                       include_dirs = ['ext'])

setup(name='geventhttpclient',
       version = '1.1',
       description = 'http client library for gevent',
       author="Antonin Amand",
       author_email="antonin.amand@gmail.com",
       packages=find_packages('src'),
       package_dir={'': 'src'},
       ext_modules = [httpparser],
       include_package_data=True,
       install_requires=[
        'gevent >= 0.13'
       ])

