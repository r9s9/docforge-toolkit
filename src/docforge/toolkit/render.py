"""Rendering helpers: section toggles, project defaults, reading output back, PDF.

``section_toggles``, ``merge_defaults`` and ``parse_docx_blocks`` are adapted
from upstream ``services/generation.py`` (``_section_toggles``,
``_merge_with_project`` and ``_parse_docx_blocks``).
"""

from __future__ import annotations

import subprocess
import tempfile
from io import BytesIO
from pathlib import Path

from ..schemas.routing import RoutingResult
from ..schemas.template import FieldDefinition
from .common import ToolkitError
from .deps import soffice_binary


def section_toggles(fields: list[FieldDefinition], routing: RoutingResult, context: dict) -> dict:
    """Which sections render. Every section is on unless something asked otherwise.

    Deliberately not inferred from empty fields: a section vanishing because the
    content happened to be thin is the scarier failure (the document looks
    complete and silently isn't). Hiding is always an explicit decision.
    """
    from ..template_builder.builder import section_toggle_name

    skipped = {
        s.get("section_key")
        for s in (routing.skipped_sections or [])
        if isinstance(s, dict) and s.get("section_key")
    }
    skipped.update(str(k) for k in (context.get("_skipped_sections") or []))
    toggles: dict[str, bool] = {}
    for key in {f.section_key for f in fields if f.section_key}:
        name = section_toggle_name(key)
        if name:
            toggles[name] = key not in skipped
    return toggles


def merge_defaults(defaults: dict, values: dict) -> dict:
    """Project metadata as defaults; explicit (non-None) values win.

    Keys matching a template field pre-fill that field; other keys flow through
    as extra template variables.
    """
    merged = dict(defaults)
    for k, v in values.items():
        if v is not None:
            merged[k] = v
    return merged


def parse_docx_blocks(data: bytes) -> list[dict]:
    """Flatten a rendered .docx into ordered blocks: headings, paragraphs, tables."""
    from docx import Document
    from docx.table import Table

    from ..structure_normalizer import iter_block_items

    doc = Document(BytesIO(data))
    blocks: list[dict] = []
    for block in iter_block_items(doc):
        if isinstance(block, Table):
            rows = [[c.text.strip() for c in r.cells] for r in block.rows]
            blocks.append({"type": "table", "headers": rows[0] if rows else [], "rows": rows[1:]})
        else:  # Paragraph
            text = block.text
            if not text.strip():
                continue
            try:
                style = block.style.name if block.style else ""
            except Exception:  # noqa: BLE001 - a broken style reference is not fatal
                style = ""
            kind = "heading" if style.lower().startswith(("heading", "title")) else "paragraph"
            blocks.append({"type": kind, "text": text, "style": style})
    return blocks


def to_pdf(docx_path: str | Path, out_dir: str | Path | None = None, timeout: int = 180) -> Path:
    """Convert a .docx to PDF with LibreOffice. Raises ToolkitError if unavailable."""
    soffice = soffice_binary()
    if not soffice:
        raise ToolkitError(
            "PDF export needs LibreOffice (soffice), which isn't installed here",
            hint="Deliver the .docx instead; it opens in Word, Google Docs and LibreOffice.",
        )
    src = Path(docx_path).resolve()
    dest = Path(out_dir).resolve() if out_dir else src.parent
    dest.mkdir(parents=True, exist_ok=True)
    # A private profile directory: a stale or shared LibreOffice profile is the
    # usual reason a headless conversion hangs or silently produces nothing.
    with tempfile.TemporaryDirectory(prefix="docforge-lo-") as profile:
        cmd = [
            soffice, f"-env:UserInstallation={Path(profile).as_uri()}",
            "--headless", "--convert-to", "pdf", "--outdir", str(dest), str(src),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=timeout)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            raise ToolkitError(f"PDF conversion failed: {exc}") from exc
    pdf = dest / (src.stem + ".pdf")
    if not pdf.is_file():
        raise ToolkitError("LibreOffice finished but produced no PDF")
    return pdf
