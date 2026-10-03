"""Build setup for geventhttpclient C extensions.

Compiles two native extensions, both statically linking their vendored
protocol library (llhttp-Muster: sources directly, no CMake):

- ``_parser``:       HTTP/1.1 sans-IO wrapper around llhttp
- ``_http2_parser``: HTTP/2 sans-IO wrapper around nghttp2 (vendor/nghttp2/lib/ only)
"""

import os
import sys

from setuptools import setup
from setuptools.extension import Extension

# Version of the pinned nghttp2 submodule (vendor/nghttp2).
# Update together with the submodule pin. Used to generate
# lib/includes/nghttp2/nghttp2ver.h, which nghttp2 does not check in
# (only the nghttp2ver.h.in template is tracked).
NGHTTP2_VERSION = "1.70.0"


def _nghttp2_sources() -> list[str]:
    """Collect the C source files of nghttp2's lib/ directory."""
    src_dir = os.path.join("vendor", "nghttp2", "lib")
    return sorted(
        os.path.join(src_dir, name)
        for name in os.listdir(src_dir)
        if name.endswith(".c")
    )


def _generate_nghttp2ver_h() -> str:
    """Generate nghttp2ver.h from the nghttp2ver.h.in template.

    Writes into ``build/include/nghttp2/`` (not into the submodule, so
    the pinned submodule checkout stays clean). Returns the directory to
    add to include_dirs, so it shadows the missing in-tree header.
    """
    template_path = os.path.join(
        "vendor", "nghttp2", "lib", "includes", "nghttp2", "nghttp2ver.h.in"
    )
    out_dir = os.path.join("build", "include", "nghttp2")
    out_path = os.path.join(out_dir, "nghttp2ver.h")
    with open(template_path, encoding="utf-8") as f:
        template = f.read()
    major, minor, patch = (int(part) for part in NGHTTP2_VERSION.split("."))
    version_num = (major << 16) | (minor << 8) | patch
    content = template.replace("@PACKAGE_VERSION@", NGHTTP2_VERSION)
    content = content.replace("@PACKAGE_VERSION_NUM@", str(version_num))
    os.makedirs(out_dir, exist_ok=True)
    # Rewrite only on change: keeps incremental builds from recompiling
    # every nghttp2 source on each setup.py invocation.
    try:
        with open(out_path, encoding="utf-8") as f:
            if f.read() == content:
                return os.path.join("build", "include")
    except FileNotFoundError:
        pass
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content)
    return os.path.join("build", "include")


nghttp2_include = _generate_nghttp2ver_h()

http_parser = Extension(
    "geventhttpclient._parser",
    sources=[
        "ext/_parser.c",
        "vendor/llhttp/src/api.c",
        "vendor/llhttp/src/http.c",
        "vendor/llhttp/src/llhttp.c",
    ],
    include_dirs=[
        "ext",
        "vendor/llhttp/include",
    ],
)

# Feature macros for nghttp2's lib/ sources on POSIX. HAVE_CONFIG_H is
# deliberately NOT defined: the source guards include config.h only when
# it is set, and we provide the needed flags directly instead. The
# HAVE_* headers exist only on POSIX -- defining them on Windows would
# pull in missing headers (arpa/inet.h et al).
NGHTTP2_DEFINES = []

if sys.platform == "win32":
    # MSVC has no ``ssize_t``. Upstream handles this in the CMake build
    # via cmakeconfig.h (``#cmakedefine ssize_t @ssize_t@`` -> int,
    # mirroring autoconf's AC_TYPE_SSIZE_T fallback); our direct-C build
    # replicates the same macro here. MSVC's headers never typedef
    # ssize_t, so the macro cannot collide. NGHTTP2_STATICLIB keeps the
    # headers from emitting dllimport attributes (the lib is compiled
    # into this extension, not a DLL).
    NGHTTP2_DEFINES.extend(
        [
            ("ssize_t", "int"),
            ("NGHTTP2_STATICLIB", "1"),
        ],
    )
else:
    NGHTTP2_DEFINES.extend(
        [
            ("HAVE_ARPA_INET_H", "1"),
            ("HAVE_NETINET_IN_H", "1"),
            ("HAVE_CLOCK_GETTIME", "1"),
            ("HAVE_DECL_CLOCK_MONOTONIC", "1"),
        ],
    )

http2_parser = Extension(
    "geventhttpclient._http2_parser",
    sources=[
        "ext/_http2_parser.c",
        *_nghttp2_sources(),
    ],
    include_dirs=[
        "ext",
        nghttp2_include,
        "vendor/nghttp2/lib/includes",
        "vendor/nghttp2/lib",
    ],
    define_macros=NGHTTP2_DEFINES,
)

setup(
    ext_modules=[http_parser, http2_parser],
)
