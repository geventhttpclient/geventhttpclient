build_ext:
	uv run python setup.py build_ext --inplace

test:
	uv run pytest tests

# Run the suite without gevent monkey patching (see issue #241).
# Tests that require patching (tests/gevent_only) are excluded automatically.
test-nongevent:
	NON_GEVENT=1 uv run pytest tests

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
	cat RELEASING.md

.PHONY: develop dist release test test-nongevent
