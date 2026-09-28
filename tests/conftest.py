import os

import gevent.monkey

# Tests run monkey patched by default. Set NON_GEVENT=1 to run the suite
# without monkey patching (see issue #241); tests that require patching
# live in tests/gevent_only and are excluded in that mode.
if os.environ.get("NON_GEVENT") != "1":
    gevent.monkey.patch_all()  # make sure all tests run monkey patched
