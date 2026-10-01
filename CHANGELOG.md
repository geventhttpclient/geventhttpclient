# Changelog

## Unreleased

### Removed

- `Headers.iteroriginal()` and `Headers.iget()`. Both were deprecated with an
  announced removal in v2.3.0 and emitted a `DeprecationWarning` since then.
  Use `Headers.items()` and `Headers.getlist()` instead.
