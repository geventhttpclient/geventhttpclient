import os

# Tests in this folder rely on gevent monkey patching. They are only
# collected in the default patched mode; with NON_GEVENT=1 the suite runs
# unpatched and this folder is excluded from collection (see issue #241).
if os.environ.get("NON_GEVENT") == "1":
    collect_ignore_glob = ["test_*.py"]
