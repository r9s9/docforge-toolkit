# docforge-live

DocForge's Word template engine, and the command line that the
[DocForge.live plugin for Claude](https://github.com/r9s9/docforge-claude-plugin)
runs.

It turns finished `.docx` files into reusable templates, fills them in the
original layout (fonts, tables, headers and numbering stay as they were), and
checks returned documents against their template. The plugin tells Claude when
and how to use it; this package does the deterministic work, on your computer
or in Claude's code sandbox.

The package makes no network calls and has no telemetry. It reads and writes
only the files you give it.

## Install

```
pip install docforge-live
```

Python 3.10 or newer. PDF export also needs LibreOffice (`soffice`).

## Use

```
docforge-live --help
```

`python3 -m docforge.toolkit` does the same, and works even when pip's script
folder isn't on your PATH. Every command prints one JSON object; errors come
back as `{"ok": false, "error", "hint"}`.

To try it on sample documents:

```
docforge-live demo --out demo
docforge-live analyze demo/invoice_1.docx demo/invoice_2.docx --work work --mode smart
docforge-live build --work work --name Invoice --out templates/invoice
docforge-live check templates/invoice demo/invoice_edited.docx
```

The plugin's skill references describe each workflow and the files Claude
writes along the way (`decisions.json`, values files, verdicts).

## What's in this repository

- `src/docforge/`: DocForge's document engine, copied from
  [r9s9/docforge](https://github.com/r9s9/docforge). `tools/UPSTREAM.md`
  records the commit. Never edit these files by hand: change them upstream
  and sync.
- `src/docforge/toolkit/`: this package's own code, the command line and the
  workflows on top of the engine.
- `tools/sync_upstream.py`: copies the engine from a local copy of
  r9s9/docforge, and `--verify` checks it still matches the lock.
- `tools/run_upstream_tests.py`: runs DocForge's own engine tests against
  this copy.
- `tests/`: the package's tests.
- `plugin_checks/`: checks for the DocForge.live plugin, which can't hold
  code itself. The plugin's CI runs them against the plugin repository; they
  also set the toolkit up by following the skill's own instructions and run
  the workflows with the pinned version from PyPI.

## Development

`.github/workflows/ci.yml` lists the checks: lint with ruff, the upstream lock
check, the tests on Python 3.10 and 3.13, a build that is installed and run in
a clean environment, and upstream's engine tests. Install the package in a
virtual environment with pytest and ruff, then run the same commands.

After syncing the engine, set `ref:` in the upstream-parity job of `ci.yml`
to the new commit in `tools/upstream.lock.json`; a test fails until the two
match.

## Releasing

1. Raise `version` in `pyproject.toml`, push to `main`, and wait for CI to
   pass.
2. On GitHub, publish a release tagged `v` plus that version, for example
   `v1.0.1`. The publish workflow builds the package, checks that the tag
   matches the version, and waits for approval in the `pypi` environment.
3. Approve it. PyPI never accepts the same version twice, so a fix always
   gets a new version.
4. In the plugin, raise the pinned version in `skills/docforge/SKILL.md`,
   then release the plugin.

## License

MIT, © The DocForge Authors, the same as DocForge itself. The dependencies
install from PyPI under their own licenses: python-docx (MIT), docxtpl
(LGPL-2.1), lxml (BSD), Jinja2 (BSD), pydantic (MIT) and python-dateutil
(Apache-2.0 or BSD).
