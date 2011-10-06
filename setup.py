from distutils.core import setup
from setuptools.extension import Extension
from setuptools import find_packages

httpparser = Extension('_parser',
                    sources = ['ext/_parser.c', 'ext/http_parser.c'],
                    include_dirs = ['ext'])

setup(name='httpparser',
       version = '1.0',
       description = 'http parser from joyent and client',
       packages=find_packages('src'),
       package_dir={'': 'src/httpparser'},
       ext_modules = [httpparser])

