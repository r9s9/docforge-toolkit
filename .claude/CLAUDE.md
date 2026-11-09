# Working in this repository

This is `docforge-live` on PyPI: DocForge's engine plus the command line the
DocForge.live plugin for Claude (r9s9/docforge-claude-plugin) installs and
runs. The plugin pins an exact version of this package.

- The engine in `src/docforge/` is copied from r9s9/docforge. Never hand-edit
  those files: change them upstream and re-sync with `tools/sync_upstream.py`.
  Code owned here lives in `src/docforge/toolkit/` (and the package
  `__init__.py` files the sync tool lists as owned).
- Keep the command line backward compatible within a major version: released
  plugin versions call it with the commands and options they were written
  for.
- A version on PyPI can never be replaced. Release only from a GitHub release
  tagged `vX.Y.Z` that matches `version` in `pyproject.toml`; the publish
  workflow waits for the owner's approval.
- Raise the plugin's pin only after the new version is on PyPI.
- Before pushing, run the checks in `.github/workflows/ci.yml`.
