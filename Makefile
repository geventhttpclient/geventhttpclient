build_ext:
	python setup.py build_ext --inplace

test:
	pytest src/geventhttpclient/tests

_develop:
	python setup.py develop

develop: _develop build_ext

clean:
	rm -rf build
	rm -rf dist
	rm -rf src/geventhttpclient.egg_info
	find . -name '*.pyc' -delete
	find src -name '*.so' -delete

dist:
	python setup.py sdist upload

release:
	cat release.md

.PHONY: develop dist release test
