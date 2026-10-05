# Making new releases

1. Tag the release commit and create the GitHub release for it. The tag
   must be named after the version, in the bare format without a "v"
   prefix (e.g. `2.5.2`). Existing release tags are lightweight tags.
   Either create tag and release in the web UI at
   <https://github.com/geventhttpclient/geventhttpclient/releases/new>, or
   from the command line:

   ```text
   git tag 2.5.2
   git push origin 2.5.2
   gh release create 2.5.2 --title "2.5.2" --generate-notes
   ```

   Creating the release, not pushing the tag, is what triggers the
   publish workflow:
   <https://github.com/geventhttpclient/geventhttpclient/actions/workflows/publish.yml>

   The package version is derived from the tag by setuptools-scm
   (configured in `pyproject.toml`). Nothing else needs to be bumped; the
   generated `src/geventhttpclient/_version.py` is created at build time
   and must not be committed. If the release changes installation
   requirements, mention it in the release notes (for example the
   `gevent>=25.9` floor). Note that historic `v`-prefixed tags
   (1.x era) also resolve correctly, but new tags must not use the prefix.

2. The publish workflow builds and uploads, in this order:

   - Wheels via cibuildwheel (version pinned in the workflow, build and
     arch configuration in `pyproject.toml`): CPython 3.11-3.14 on
     manylinux and musllinux for x86_64, aarch64 and ppc64le, macOS for
     x86_64, arm64 and universal2, and Windows for x86_64 and ARM64.
     Each built wheel is installed and smoke-tested (import of the
     package and its C extension) before it is uploaded.
     Building requires the `llhttp` git submodule and the full tag
     history; the workflow checks out with `fetch-depth: 0` and
     `submodules: recursive`.
   - The source distribution via `uv build`, including the complete test
     suite and the `py.typed` marker. Its version is derived the same way.

   Artifacts are uploaded to TestPyPI first, then to PyPI.

3. If the automatic trigger did not start the workflow, or it has to be
   re-run, dispatch it manually from the Actions tab with the release tag
   selected as ref. The workflow refuses to publish when the built sdist
   does not match the ref name, so dispatching from a branch is rejected.

4. Verify the result on <https://pypi.org/project/geventhttpclient/>: the
   new version should list the expected wheels. As a sanity check, install
   from the source distribution in a fresh virtualenv and import the
   package (this exercises the C extension build).

## Local builds

Without a tag on the current commit, builds produce a PEP 440 dev version
derived from the nearest tag (e.g. `2.5.2.dev3+g<hash>`). Builds without
git metadata (shallow checkouts, downloaded source trees) fall back to
`0.0.0` via `fallback_version`; importing directly from a source tree
without a build step yields `0.0.0+unknown`.
