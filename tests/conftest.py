import os

import gevent.monkey
import pytest

# Tests run monkey patched by default. Set NON_GEVENT=1 to run the suite
# without monkey patching (see issue #241); tests that require patching
# live in tests/gevent_only and are excluded in that mode.
if os.environ.get("NON_GEVENT") != "1":
    gevent.monkey.patch_all()  # make sure all tests run monkey patched
else:
    # The HTTP/2 integration tests need gevent cooperative scheduling:
    # H2TestServer serves from gevent greenlets and the client's pump
    # loop yields via gevent.sleep. Unpatched, blocking SSL handshakes
    # starve the server greenlet (CI showed handshake timeouts in the
    # non-gevent job). The sans-IO parser and connection tests stay in.
    collect_ignore_glob = [
        "http2/test_alpn.py",
        "http2/test_pool.py",
        "http2/test_response.py",
        "http2/test_session.py",
        "http2/test_session_concurrent.py",
        "http2/test_session_live.py",
        "http2/test_spec.py",
        "http2/test_useragent.py",
        "http2/test_useragent_payload.py",
        "http2/test_network.py",
    ]


def pytest_collection_modifyitems(config, items):
    """Retry network marked tests, they depend on servers without an SLA."""
    for item in items:
        if item.get_closest_marker("network") and not item.get_closest_marker("flaky"):
            item.add_marker(pytest.mark.flaky(reruns=2, delay=3))
