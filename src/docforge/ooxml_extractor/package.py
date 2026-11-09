"""DocxPackage — safe access to the raw OOXML parts of a .docx file.

A .docx is a ZIP (OPC package). This module unpacks it defensively (zip-bomb
and path-traversal guards) and exposes the raw parts: document.xml, styles.xml,
numbering.xml, header*/footer*.xml, relationships and media.

The higher-level normalizer uses python-docx for the object model, but relies on
this class for media hashing, relationship resolution and numbering metadata —
i.e. the things python-docx does not surface conveniently.
"""

from __future__ import annotations

import hashlib
import posixpath
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from lxml import etree

from .safety import parse_xml

# OOXML namespaces
NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "ep": "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties",
}

OFFICE_DOC_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"


class DocxError(Exception):
    """Generic problem reading a DOCX package."""


class UnsafeDocxError(DocxError):
    """The package tripped a safety guard (zip bomb, traversal, too many parts)."""


@dataclass
class RelInfo:
    rel_id: str
    type: str
    target: str  # resolved (package-absolute) for internal targets
    external: bool = False


@dataclass
class DocxPackage:
    """In-memory view of the parts inside a .docx OPC package."""

    parts: dict[str, bytes]

    # ----- construction ---------------------------------------------------
    @classmethod
    def from_bytes(
        cls,
        data: bytes,
        *,
        max_entries: int = 2000,
        max_total_bytes: int = 200 * 1024 * 1024,
    ) -> DocxPackage:
        """Unzip defensively. We stream-read each entry and abort if the running
        decompressed total exceeds ``max_total_bytes`` (header sizes can lie, so
        we count actual bytes), and reject path traversal / excessive entry count.
        """
        try:
            zf = zipfile.ZipFile(BytesIO(data))
        except zipfile.BadZipFile as exc:  # pragma: no cover - trivial
            raise DocxError("File is not a valid .docx (zip) archive") from exc

        parts: dict[str, bytes] = {}
        with zf:
            infos = zf.infolist()
            if len(infos) > max_entries:
                raise UnsafeDocxError(
                    f"DOCX has {len(infos)} entries (> {max_entries} limit)"
                )
            total_actual = 0
            for info in infos:
                name = info.filename
                if name.endswith("/"):
                    continue  # directory entry
                # Path-traversal / absolute-path guard.
                norm = posixpath.normpath(name)
                if norm.startswith("..") or norm.startswith("/") or ".." in norm.split("/"):
                    raise UnsafeDocxError(f"Unsafe entry path in DOCX: {name!r}")
                # Stream-read with a hard cap on actual decompressed bytes.
                with zf.open(info, "r") as fh:
                    chunks: list[bytes] = []
                    while True:
                        chunk = fh.read(65536)
                        if not chunk:
                            break
                        total_actual += len(chunk)
                        if total_actual > max_total_bytes:
                            raise UnsafeDocxError(
                                "Decompressed DOCX exceeds size limit "
                                f"({max_total_bytes} bytes) — possible zip bomb"
                            )
                        chunks.append(chunk)
                    parts[name] = b"".join(chunks)
        if not parts:
            raise DocxError("DOCX archive is empty")
        return cls(parts=parts)

    @classmethod
    def from_path(cls, path: str | Path, **kwargs) -> DocxPackage:
        data = Path(path).read_bytes()
        return cls.from_bytes(data, **kwargs)

    # ----- raw part access ------------------------------------------------
    def has(self, name: str) -> bool:
        return name in self.parts

    def part(self, name: str) -> bytes | None:
        return self.parts.get(name)

    def list_parts(self) -> list[str]:
        return sorted(self.parts)

    def xml(self, name: str) -> etree._Element | None:
        raw = self.parts.get(name)
        if raw is None:
            return None
        return parse_xml(raw)

    # ----- relationships --------------------------------------------------
    def main_document_name(self) -> str:
        """Resolve the main document part via the package root relationships.

        Falls back to the conventional ``word/document.xml`` if not declared.
        """
        root_rels = self.xml("_rels/.rels")
        if root_rels is not None:
            for rel in root_rels:
                if rel.get("Type") == OFFICE_DOC_REL:
                    target = rel.get("Target", "")
                    return target.lstrip("/")
        return "word/document.xml"

    def rels_for(self, part_name: str) -> dict[str, RelInfo]:
        """Return {rId: RelInfo} for the relationships of ``part_name``.

        Internal targets are resolved to package-absolute paths.
        """
        directory = posixpath.dirname(part_name)
        base = posixpath.basename(part_name)
        rels_name = posixpath.join(directory, "_rels", base + ".rels") if directory else f"_rels/{base}.rels"
        root = self.xml(rels_name)
        out: dict[str, RelInfo] = {}
        if root is None:
            return out
        for rel in root:
            rid = rel.get("Id", "")
            rtype = rel.get("Type", "")
            target = rel.get("Target", "")
            external = rel.get("TargetMode") == "External"
            if not external:
                # Resolve relative to the owning part's directory.
                resolved = posixpath.normpath(posixpath.join(directory, target)) if directory else target
                target = resolved.lstrip("/")
            out[rid] = RelInfo(rel_id=rid, type=rtype, target=target, external=external)
        return out

    # ----- media ----------------------------------------------------------
    def media(self) -> dict[str, bytes]:
        return {n: b for n, b in self.parts.items() if n.startswith("word/media/")}

    def part_hash(self, name: str) -> str | None:
        raw = self.parts.get(name)
        if raw is None:
            return None
        return hashlib.sha256(raw).hexdigest()

    # ----- doc properties -------------------------------------------------
    def page_count(self) -> int | None:
        """Best-effort page count from docProps/app.xml (<Pages>)."""
        root = self.xml("docProps/app.xml")
        if root is None:
            return None
        for child in root:
            if etree.QName(child).localname == "Pages" and child.text:
                try:
                    return int(child.text)
                except ValueError:
                    return None
        return None
