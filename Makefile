build_ext:
	python setup.py build_ext --inplace

test:
	pytest src/geventhttpclient/tests	

_develop:
	python setup.py develop

develop: _develop build_ext

clean:
	rm -rf build
	find . -name '*.pyc' -delete

dist:
	python setup.py sdist upload

release:
	cat release.md

.PHONY: develop dist release test
