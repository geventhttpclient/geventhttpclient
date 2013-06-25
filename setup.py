import sys
from distutils.core import setup
from setuptools.extension import Extension
from setuptools import find_packages

parser_sources = ['ext/http_parser.c']

if '__pypy__' not in sys.builtin_module_names:
    parser_sources.append('ext/_parser.c')

httpparser = Extension('geventhttpclient._parser',
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

