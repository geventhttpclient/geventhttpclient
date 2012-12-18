from distutils.core import setup
from setuptools.extension import Extension
from setuptools import find_packages
import sys

setup_requires = []
if 'nosetests' in sys.argv:
    setup_requires.append('nose')


httpparser = Extension('geventhttpclient._parser',
                    sources=['ext/_parser.c', 'ext/http_parser.c'],
                    include_dirs=['ext'])


setup(name='geventhttpclient',
       version='1.0a',
       description='http client library for gevent',
       author="Antonin Amand",
       author_email="antonin.amand@gmail.com",
       packages=find_packages('src'),
       package_dir={'': 'src'},
       ext_modules=[httpparser],
       include_package_data=True,
       install_requires=[
        'gevent >= 0.13'
       ],
       setup_requires=setup_requires,
       tests_require=[
        'pytest',
       ])
