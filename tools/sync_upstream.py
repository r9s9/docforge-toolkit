"""Copy DocForge's deterministic document engine from an upstream checkout.

The plugin runs DocForge's engine inside Claude's code sandbox, and Claude does
the AI work itself. So only the pure modules are copied into ``src/docforge``:
reading .docx files, comparing examples, building templates, filling them and
checking compliance. The AI client, web app, database and storage layers stay
upstream.

Three package ``__init__`` files and the whole ``toolkit/`` package are
plugin-owned and never overwritten. One upstream file gets small anchored
edits (see ``PATCHES``); an edit whose anchor has moved fails the sync loudly,
so an upstream change can never slip a model call back into the plugin.

Usage:
  python tools/sync_upstream.py --from ../docforge           copy, then record tools/upstream.lock.json
  python tools/sync_upstream.py --verify                     the engine matches the lock; imports stay in bounds
  python tools/sync_upstream.py --from ../docforge --status  what changed upstream since the last sync
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "src" / "docforge"
LOCK = ROOT / "tools" / "upstream.lock.json"
UPSTREAM_MD = ROOT / "tools" / "UPSTREAM.md"

UPSTREAM_REPO = "https://github.com/r9s9/docforge"

# Upstream files copied verbatim (or patched), relative to backend/docforge.
ALLOWLIST: tuple[str, ...] = (
    "__init__.py",
    "sampledata.py",
    "ai/tools.py",
    "ai_classifier/contents.py",
    "ai_classifier/fields.py",
    "ai_classifier/heuristic.py",
    "ai_classifier/tags_only.py",
    "ai_router/context.py",
    "ai_router/document.py",
    "ai_router/naming.py",
    "ai_router/router.py",
    "assembler/__init__.py",
    "assembler/assembler.py",
    "assembler/placeholders.py",
    "assembler/postprocess.py",
    "assembler/richtext.py",
    "common/__init__.py",
    "common/textutil.py",
    "compliance/__init__.py",
    "compliance/checker.py",
    "multi_doc_differ/__init__.py",
    "multi_doc_differ/differ.py",
    "ooxml_extractor/__init__.py",
    "ooxml_extractor/numbering.py",
    "ooxml_extractor/package.py",
    "ooxml_extractor/safety.py",
    "schemas/__init__.py",
    "schemas/classification.py",
    "schemas/compliance.py",
    "schemas/diff.py",
    "schemas/enums.py",
    "schemas/extraction.py",
    "schemas/generation.py",
    "schemas/routing.py",
    "schemas/template.py",
    "schemas/validation.py",
    "structure_normalizer/__init__.py",
    "structure_normalizer/normalizer.py",
    "structure_normalizer/text.py",
    "structure_normalizer/walk.py",
    "template_builder/__init__.py",
    "template_builder/builder.py",
    "template_builder/ooxml_ops.py",
    "validator/__init__.py",
    "validator/validator.py",
)

# Plugin-owned files under the engine folder: never written by a sync.
OWNED_FILES: tuple[str, ...] = (
    "ai/__init__.py",
    "ai_classifier/__init__.py",
    "ai_router/__init__.py",
)
OWNED_DIRS: tuple[str, ...] = ("toolkit",)

# Upstream files the plugin adapts logic or rules from without copying them.
# A change to one of these means a toolkit module or a skill reference may
# need the same change; --status lists them.
WATCHED: dict[str, str] = {
    "services/compliance_fix.py": "toolkit/fixer.py",
    "services/preview_docx.py": "toolkit/samples.py",
    "services/generation.py": "toolkit/render.py",
    "services/publish.py": "toolkit/package.py",
    "template_registry/registry.py": "toolkit/package.py",
    "ai_classifier/service.py": "ai_classifier/__init__.py",
    "ai_classifier/llm.py": "toolkit/analysis.py (flagging rule)",
    "ai_router/service.py": "ai_router/__init__.py",
    "compliance/judge.py": "toolkit/compliance.py (verdicts)",
    "ai/prompts.py": "the plugin's skills/docforge/references/*.md (the rules Claude follows)",
}

# The model-calling tail of ai_router/document.py, replaced by its own
# deterministic fallback (what upstream does when no model is configured).
_ROUTE_DOCUMENT_CONTENT = '''def route_document_content(
    fields: list[FieldDefinition],
    content: dict,
    *,
    template_id: str,
    version: int,
    client=None,
    template_context: dict | None = None,
    source_doc: DocumentExtraction | None = None,
    learned_hints: str = "",
) -> RoutingResult:
    """Map extracted document content onto template fields, deterministically.

    Plugin build: Claude does the AI mapping itself in the fill-template skill,
    so this is upstream's no-model path. The signature is kept for parity.
    """
    return route_document_heuristic(fields, content, template_id, version)
'''

# path -> list of (anchor, replacement, mode). mode "line": the anchor must
# occur exactly once and is replaced; mode "tail": everything from the anchor
# to the end of the file is replaced.
PATCHES: dict[str, list[tuple[str, str, str]]] = {
    "ai_router/document.py": [
        ("from ..ai.client import LLMClient, LLMError\n", "", "line"),
        ("from .llm import route_llm\n", "", "line"),
        ("def route_document_content(", _ROUTE_DOCUMENT_CONTENT, "tail"),
    ],
}

# Names that must not survive in a patched file.
PATCH_FORBIDDEN_NAMES: dict[str, set[str]] = {
    "ai_router/document.py": {"LLMClient", "LLMError", "route_llm", "generation_ai_config"},
}

# Third-party packages the engine must never import (they belong to the web app).
FORBIDDEN_PACKAGES = {
    "anthropic", "fastapi", "httpx", "jwt", "openai", "psycopg2", "pydantic_settings",
    "sqlalchemy", "starlette", "uvicorn",
}
# Upstream modules the engine must never import.
FORBIDDEN_MODULES = {
    "docforge.ai.client", "docforge.ai.prompts", "docforge.ai.parallel", "docforge.ai.usage",
    "docforge.ai.deadline", "docforge.ai.errors", "docforge.ai.anthropic_transport",
    "docforge.ai.pricing", "docforge.ai_quota", "docforge.ai_keys", "docforge.api",
    "docforge.config", "docforge.db", "docforge.jobs", "docforge.logging_setup",
    "docforge.services", "docforge.settings_store", "docforge.storage",
    "docforge.template_registry",
}


class SyncError(RuntimeError):
    """A sync or verify step failed; the message says what to fix."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _upstream_package(path: Path) -> Path:
    """Accept a repo checkout (with backend/docforge) or the package directory."""
    for candidate in (path / "backend" / "docforge", path):
        if (candidate / "__init__.py").is_file() and (candidate / "schemas").is_dir():
            return candidate
    raise SyncError(f"no DocForge package found under {path}")


def _git_commit(path: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True, timeout=30,
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _upstream_version(pkg: Path) -> str:
    for line in (pkg / "__init__.py").read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip("\"'")
    return "unknown"


def _apply_patches(rel: str, text: str) -> str:
    for anchor, replacement, mode in PATCHES.get(rel, []):
        count = text.count(anchor)
        if count != 1:
            raise SyncError(
                f"{rel}: patch anchor {anchor!r} found {count} times (expected 1). "
                "Upstream changed this file; update PATCHES in tools/sync_upstream.py."
            )
        if mode == "line":
            text = text.replace(anchor, replacement)
        elif mode == "tail":
            head = text[: text.index(anchor)]
            tail = text[text.index(anchor):]
            rest = tail.split("\n", 1)[1] if "\n" in tail else ""
            # The replaced tail must be the last top-level definition.
            for line in rest.splitlines():
                if line.startswith(("def ", "class ", "async def ")):
                    raise SyncError(
                        f"{rel}: found {line.split('(')[0]!r} after {anchor!r}; "
                        "the tail patch would drop it. Update PATCHES."
                    )
            text = head + replacement
        else:  # pragma: no cover - programming error
            raise SyncError(f"unknown patch mode {mode!r}")
    banned = PATCH_FORBIDDEN_NAMES.get(rel)
    if banned:
        names = {n.id for n in ast.walk(ast.parse(text)) if isinstance(n, ast.Name)}
        names |= {
            a.name for n in ast.walk(ast.parse(text))
            if isinstance(n, ast.ImportFrom) for a in n.names
        }
        left = sorted(banned & names)
        if left:
            raise SyncError(f"{rel}: patched file still references {left}")
    return text


def _module_name(path: Path) -> str:
    rel = path.relative_to(ENGINE.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _resolve_relative(module: str, is_package: bool, level: int, target: str | None) -> str:
    base = module.split(".")
    if not is_package:
        base = base[:-1]
    if level > 1:
        base = base[: len(base) - (level - 1)]
    return ".".join(base + ([target] if target else []))


def _module_exists(name: str) -> bool:
    parts = name.split(".")
    if parts[0] != "docforge":
        return True
    path = ENGINE.parent.joinpath(*parts)
    return path.with_suffix(".py").is_file() or (path / "__init__.py").is_file()


def check_imports() -> list[str]:
    """Every engine import resolves inside the engine and stays off the web app."""
    problems: list[str] = []
    for path in sorted(ENGINE.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        module = _module_name(path)
        is_package = path.name == "__init__.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(ROOT)
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    resolved = _resolve_relative(module, is_package, node.level, node.module)
                else:
                    resolved = node.module or ""
                targets = [resolved]
                # "from . import x" may name a submodule.
                for alias in node.names:
                    candidate = f"{resolved}.{alias.name}"
                    if _module_exists(candidate) and candidate.startswith("docforge."):
                        targets.append(candidate)
            for name in targets:
                top = name.split(".")[0]
                if top in FORBIDDEN_PACKAGES:
                    problems.append(f"{rel}:{node.lineno}: imports web-app package {name!r}")
                if any(name == m or name.startswith(m + ".") for m in FORBIDDEN_MODULES):
                    problems.append(f"{rel}:{node.lineno}: imports upstream-only module {name!r}")
                elif name.startswith("docforge") and not _module_exists(name):
                    problems.append(f"{rel}:{node.lineno}: import {name!r} is not in the engine")
    return problems


def _is_owned(rel: str) -> bool:
    return rel in OWNED_FILES or any(rel.startswith(d + "/") for d in OWNED_DIRS)


def sync(source: Path, ref: str | None) -> None:
    pkg = _upstream_package(source.resolve())
    commit = ref or _git_commit(pkg)
    ENGINE.mkdir(parents=True, exist_ok=True)

    synced: dict[str, dict[str, str]] = {}
    for rel in ALLOWLIST:
        src = pkg / rel
        if not src.is_file():
            raise SyncError(f"upstream file missing: {rel}")
        raw = src.read_bytes()
        text = _apply_patches(rel, raw.decode("utf-8"))
        out = ENGINE / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        data = text.encode("utf-8")
        out.write_bytes(data)
        synced[rel] = {"upstream_sha256": _sha256(raw), "engine_sha256": _sha256(data)}

    # Remove engine files that are neither synced nor plugin-owned.
    stale = []
    for path in sorted(ENGINE.rglob("*")):
        if path.is_dir() or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(ENGINE).as_posix()
        if rel not in synced and not _is_owned(rel):
            stale.append(rel)
            path.unlink()
    missing_owned = [rel for rel in OWNED_FILES if not (ENGINE / rel).is_file()]

    watched = {}
    for rel in WATCHED:
        src = pkg / rel
        watched[rel] = _sha256(src.read_bytes()) if src.is_file() else "missing"

    lock = {
        "upstream": UPSTREAM_REPO,
        "commit": commit,
        "version": _upstream_version(pkg),
        "files": synced,
        "watched": watched,
    }
    LOCK.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _write_upstream_md(lock)

    problems = check_imports()
    print(f"Synced {len(synced)} file(s) from {pkg} @ {commit[:12]} (DocForge {lock['version']}).")
    if stale:
        print("Removed stale engine file(s): " + ", ".join(stale))
    if missing_owned:
        print("Plugin-owned file(s) still to write: " + ", ".join(missing_owned))
    if problems:
        raise SyncError("import check failed:\n  " + "\n  ".join(problems))


def _write_upstream_md(lock: dict) -> None:
    patched = "\n".join(f"- `{rel}`" for rel in PATCHES)
    owned = "\n".join(f"- `{rel}`" for rel in (*OWNED_FILES, *(d + "/" for d in OWNED_DIRS)))
    watched = "\n".join(f"- `{rel}` → {target}" for rel, target in WATCHED.items())
    files = "\n".join(f"- `{rel}`" for rel in sorted(lock["files"]))
    UPSTREAM_MD.write_text(
        f"""# Engine provenance

`src/docforge/` is DocForge's deterministic document engine, copied from
[{lock['upstream'].removeprefix('https://github.com/')}]({lock['upstream']}) by
`tools/sync_upstream.py`. Don't edit the copied files by hand: change them
upstream and re-sync, or add an anchored patch to the sync tool.

- Upstream commit: `{lock['commit']}`
- DocForge version: `{lock['version']}`

## Patched on copy

These files get small anchored edits that remove model calls:

{patched}

## Plugin-owned

Written for the plugin and never touched by a sync:

{owned}

## Watched upstream files

The plugin adapts logic or rules from these files without copying them. When
one changes, review the plugin file named next to it (paths under the engine folder):

{watched}

## Copied files

{files}
""",
        encoding="utf-8",
    )


def verify() -> None:
    if not LOCK.is_file():
        raise SyncError("tools/upstream.lock.json is missing; run a sync first")
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    problems: list[str] = []
    for rel, hashes in sorted(lock["files"].items()):
        path = ENGINE / rel
        if not path.is_file():
            problems.append(f"missing engine file {rel}")
        elif _sha256(path.read_bytes()) != hashes["engine_sha256"]:
            problems.append(f"{rel} differs from the synced copy (edited by hand?)")
    for rel in OWNED_FILES:
        if not (ENGINE / rel).is_file():
            problems.append(f"missing plugin-owned file {rel}")
    problems += check_imports()
    if problems:
        raise SyncError("verify failed:\n  " + "\n  ".join(problems))
    print(f"Engine matches upstream {lock['commit'][:12]} ({len(lock['files'])} files); imports OK.")


def status(source: Path) -> int:
    pkg = _upstream_package(source.resolve())
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    changed = [
        rel for rel, h in sorted(lock["files"].items())
        if not (pkg / rel).is_file() or _sha256((pkg / rel).read_bytes()) != h["upstream_sha256"]
    ]
    watched = [
        rel for rel, h in sorted(lock["watched"].items())
        if ((pkg / rel).is_file() and _sha256((pkg / rel).read_bytes()) != h)
        or (not (pkg / rel).is_file() and h != "missing")
    ]
    print(f"Lock: {lock['commit'][:12]}  Upstream now: {_git_commit(pkg)[:12]}")
    if changed:
        print("Copied files changed upstream (re-run the sync):")
        print("\n".join(f"  {rel}" for rel in changed))
    if watched:
        print("Watched files changed upstream (review the plugin side):")
        print("\n".join(f"  {rel} → {WATCHED[rel]}" for rel in watched))
    if not changed and not watched:
        print("Up to date.")
    return 1 if (changed or watched) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="source", type=Path, help="DocForge checkout (repo root or package dir)")
    parser.add_argument("--ref", help="label to record instead of the checkout's git commit")
    parser.add_argument("--verify", action="store_true", help="check the engine against the lock")
    parser.add_argument("--status", action="store_true", help="with --from: report upstream changes")
    args = parser.parse_args(argv)
    try:
        if args.verify:
            verify()
            return 0
        if not args.source:
            parser.error("--from is required unless --verify is given")
        if args.status:
            return status(args.source)
        sync(args.source, args.ref)
        return 0
    except SyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
