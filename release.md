# Making new releases

The version number should follow the [semantic versioning guidelines](http://semver.org/).

- Bump version in src/geventhttpclient/\__init__.py
- Bump version in setup.py
- Tag v1.1.x

then run:

    python setup.py sdist upload

Please only use `sdist` !