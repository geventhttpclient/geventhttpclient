import os

import gevent.monkey
import pytest

# Tests run monkey patched by default. Set NON_GEVENT=1 to run the suite
# without monkey patching (see issue #241); tests that require patching
# live in tests/gevent_only and are excluded in that mode.
if os.environ.get("NON_GEVENT") != "1":
    gevent.monkey.patch_all()  # make sure all tests run monkey patched


def pytest_collection_modifyitems(config, items):
    """Retry network marked tests, they depend on servers without an SLA."""
    for item in items:
        if item.get_closest_marker("network") and not item.get_closest_marker("flaky"):
            item.add_marker(pytest.mark.flaky(reruns=2, delay=3))
