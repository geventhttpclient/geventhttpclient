build_ext:
	python setup.py build_ext --inplace

test:
	pytest geventhttpclient/tests

_develop:
	python setup.py develop

develop: _develop build_ext

clean:
	rm -rf build
	rm -rf dist
	rm -rf geventhttpclient.egg-info/
	find . -name '*.pyc' -delete
	find . -name '*.so' -delete

dist:
	python setup.py sdist upload

release:
	cat release.md

.PHONY: develop dist release test
