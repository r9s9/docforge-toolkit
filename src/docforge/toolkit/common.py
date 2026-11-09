"""Shared helpers for the toolkit: errors, JSON in and out, and safe .docx input."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# The same limits the DocForge web app applies to uploads (backend config.py).
MAX_UPLOAD_MB = 25
MAX_EXAMPLES = 5


class ToolkitError(Exception):
    """A problem the person or Claude can fix. The message says how."""

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


def read_json(path: str | Path) -> Any:
    p = Path(path)
    if not p.is_file():
        raise ToolkitError(f"file not found: {p}")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ToolkitError(f"{p} is not valid JSON: {exc}") from exc


def write_json(path: str | Path, data: Any) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    return p


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(", ", ": "), default=str)


def to_json(obj: Any) -> str:
    """Valid JSON that stays readable and cheap to read back.

    One top-level key per line, and one item per line for lists of objects,
    so a long outline reads like a table instead of a wall of nested braces.
    """
    if not isinstance(obj, dict):
        return _compact(obj)
    lines = ["{"]
    items = list(obj.items())
    for i, (key, value) in enumerate(items):
        comma = "," if i < len(items) - 1 else ""
        if isinstance(value, list) and value and all(isinstance(x, (dict, list)) for x in value):
            inner = ",\n".join("  " + _compact(x) for x in value)
            lines.append(f"{_compact(key)}: [\n{inner}\n]{comma}")
        else:
            lines.append(f"{_compact(key)}: {_compact(value)}{comma}")
    lines.append("}")
    return "\n".join(lines)


def check_docx(path: str | Path) -> bytes:
    """Read a .docx and check it the way the DocForge upload path does.

    Type and size first, then a safe parse of the zip package: entry-count and
    decompressed-size limits (zip bombs), path-traversal names, and unsafe XML
    are all rejected by ``DocxPackage``. Returns the file's bytes.
    """
    from ..ooxml_extractor.package import DocxError, DocxPackage, UnsafeDocxError

    p = Path(path)
    if not p.is_file():
        raise ToolkitError(f"file not found: {p}")
    if p.suffix.lower() != ".docx":
        raise ToolkitError(
            f"{p.name}: only .docx files are supported",
            hint="Save or convert the file as .docx first, for example with "
            "`soffice --headless --convert-to docx <file>`.",
        )
    data = p.read_bytes()
    if not data:
        raise ToolkitError(f"{p.name} is empty")
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise ToolkitError(f"{p.name} is larger than {MAX_UPLOAD_MB} MB")
    try:
        pkg = DocxPackage.from_bytes(data)
    except UnsafeDocxError as exc:
        raise ToolkitError(f"{p.name}: rejected as unsafe: {exc}") from exc
    except DocxError as exc:
        raise ToolkitError(f"{p.name}: not a valid .docx: {exc}") from exc
    if not pkg.has(pkg.main_document_name()):
        raise ToolkitError(f"{p.name}: the .docx has no main document part")
    return data


_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._ ()-]+")


def safe_filename(name: str, fallback: str = "file") -> str:
    """A filename that is safe on every platform (no separators or odd characters)."""
    base = Path(name or "").name
    cleaned = _UNSAFE_NAME.sub("_", base).strip(" .")
    return cleaned or fallback
