build_ext:
	uv run python setup.py build_ext --inplace

test:
	uv run pytest tests

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
