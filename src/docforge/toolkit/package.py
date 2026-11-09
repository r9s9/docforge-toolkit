"""Template packages: one folder (or zip) per template, one subfolder per version.

Each version folder uses the same layout and file names as the DocForge web
app's ``template_registry`` (``templates/<id>/<version>/``), so a package made
here is the same thing the app stores:

    <package>/
      docforge-template.json        index: id, name, versions (plugin addition)
      README.txt                    what this folder is (plugin addition)
      notes.md                      optional: preferences Claude should keep
      1/
        template.docx               the template, with {{ tags }}
        with-labels.docx            the template with «Label» values (plugin addition)
        template_intelligence.json  document type, sections, classifications
        field_definitions.json
        validation_rules.json
        manifest.json
        review_snapshot.json
        representative.json         normalized structure of the example used
        representative.docx
        source_examples/            the example documents
        extracted_sources/          their normalized structure
"""

from __future__ import annotations

import json
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..common.textutil import slugify_field
from ..schemas.extraction import DocumentExtraction
from ..schemas.template import (
    FieldDefinition,
    ReviewSnapshot,
    TemplateIntelligence,
    TemplateManifest,
    ValidationRule,
)
from .common import ToolkitError, safe_filename

# File names shared with upstream template_registry/registry.py.
TEMPLATE_DOCX = "template.docx"
INTELLIGENCE = "template_intelligence.json"
FIELDS = "field_definitions.json"
RULES = "validation_rules.json"
MANIFEST = "manifest.json"
REVIEW = "review_snapshot.json"
REPRESENTATIVE = "representative.json"
REPRESENTATIVE_DOCX = "representative.docx"
SOURCES = "source_examples"
EXTRACTED = "extracted_sources"

# Plugin additions.
INDEX = "docforge-template.json"
LABELS_DOCX = "with-labels.docx"
NOTES = "notes.md"
FORMAT = "docforge-template-package"
FORMAT_VERSION = 1

# Limits for unpacking a zipped package someone attached.
_ZIP_MAX_ENTRIES = 2000
_ZIP_MAX_BYTES = 300 * 1024 * 1024

_README = """DocForge template package: {name}

This folder is a reusable Word template made by the DocForge.live plugin for Claude.
Each numbered folder is a version; the highest number is the latest.

- template.docx      the template, with {{{{ tags }}}} where content goes
- with-labels.docx   the same template showing each field as «Label»

To use it, keep this folder (or a zip of it) and ask Claude to fill or check
documents with it. The JSON files describe the fields, rules and layout.
"""


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _read_json(path: Path):
    if not path.is_file():
        raise ToolkitError(f"template package is missing {path.name} ({path.parent})")
    return json.loads(path.read_text(encoding="utf-8"))


def _versions(root: Path) -> list[int]:
    return sorted(
        int(p.name) for p in root.iterdir()
        if p.is_dir() and p.name.isdigit() and (p / MANIFEST).is_file()
    ) if root.is_dir() else []


@dataclass
class TemplateVersion:
    """One version of a template package, read from disk."""

    root: Path  # the version folder
    package_root: Path

    @property
    def manifest(self) -> TemplateManifest:
        return TemplateManifest.model_validate(_read_json(self.root / MANIFEST))

    @property
    def template_id(self) -> str:
        return self.manifest.template_id

    @property
    def version(self) -> int:
        return self.manifest.version

    @property
    def name(self) -> str:
        return self.manifest.name

    def fields(self) -> list[FieldDefinition]:
        return [FieldDefinition.model_validate(x) for x in _read_json(self.root / FIELDS)]

    def rules(self) -> list[ValidationRule]:
        return [ValidationRule.model_validate(x) for x in _read_json(self.root / RULES)]

    def intelligence(self) -> TemplateIntelligence:
        return TemplateIntelligence.model_validate(_read_json(self.root / INTELLIGENCE))

    def review(self) -> ReviewSnapshot:
        return ReviewSnapshot.model_validate(_read_json(self.root / REVIEW))

    def representative(self) -> DocumentExtraction | None:
        path = self.root / REPRESENTATIVE
        if not path.is_file():
            return None
        return DocumentExtraction.model_validate(_read_json(path))

    def template_bytes(self) -> bytes:
        path = self.root / TEMPLATE_DOCX
        if not path.is_file():
            raise ToolkitError(f"template package is missing {TEMPLATE_DOCX} ({self.root})")
        return path.read_bytes()

    def notes(self) -> str:
        path = self.package_root / NOTES
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def summary(self) -> dict:
        m = self.manifest
        return {
            "template_id": m.template_id,
            "name": m.name,
            "version": m.version,
            "versions": _versions(self.package_root),
            "package": str(self.package_root),
            "template_docx": str(self.root / TEMPLATE_DOCX),
            "with_labels_docx": str(self.root / LABELS_DOCX) if (self.root / LABELS_DOCX).is_file() else None,
        }


def _find_root(path: Path) -> tuple[Path, Path | None]:
    """(package root, explicit version folder or None) for a folder."""
    if (path / MANIFEST).is_file() and path.name.isdigit():
        return path.parent, path
    if (path / INDEX).is_file() or _versions(path):
        return path, None
    children = [p for p in path.iterdir() if p.is_dir() and not p.name.startswith((".", "__"))]
    if len(children) == 1:  # a zip that holds the package folder
        return _find_root(children[0])
    raise ToolkitError(
        f"{path} is not a DocForge template package",
        hint="Point at the package folder (it holds docforge-template.json and numbered "
        "version folders), a version folder, or a .zip of the package.",
    )


def _safe_extract(zip_path: Path) -> Path:
    target = Path(tempfile.mkdtemp(prefix="docforge-package-"))
    try:
        with zipfile.ZipFile(zip_path) as zf:
            infos = zf.infolist()
            if len(infos) > _ZIP_MAX_ENTRIES:
                raise ToolkitError(f"{zip_path.name} has too many files to be a template package")
            total = 0
            for info in infos:
                name = info.filename
                parts = Path(name).parts
                if name.startswith(("/", "\\")) or ".." in parts or ":" in name:
                    raise ToolkitError(f"{zip_path.name} contains an unsafe path: {name!r}")
                total += info.file_size
                if total > _ZIP_MAX_BYTES:
                    raise ToolkitError(f"{zip_path.name} unpacks to more than {_ZIP_MAX_BYTES // 2**20} MB")
            for info in infos:
                if info.is_dir():
                    continue
                dest = target / info.filename
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
    except zipfile.BadZipFile as exc:
        raise ToolkitError(f"{zip_path.name} is not a valid zip file") from exc
    return target


def open_template(path: str | Path, version: int | None = None) -> TemplateVersion:
    """Open a package folder, a version folder, or a zipped package."""
    p = Path(path)
    if not p.exists():
        raise ToolkitError(f"template package not found: {p}")
    if p.is_file():
        if p.suffix.lower() != ".zip":
            raise ToolkitError(
                f"{p.name} is not a template package",
                hint="A package is a folder or a .zip of one. A .docx with {{ tags }} alone "
                "is not enough: the package also holds the field definitions.",
            )
        p = _safe_extract(p)
    root, explicit = _find_root(p)
    if explicit is not None and version is None:
        return TemplateVersion(root=explicit, package_root=root)
    versions = _versions(root)
    if not versions:
        raise ToolkitError(f"{root} has no template versions")
    chosen = version if version is not None else versions[-1]
    if chosen not in versions:
        raise ToolkitError(f"version {chosen} not found; this package has {versions}")
    return TemplateVersion(root=root / str(chosen), package_root=root)


class PackageWriter:
    """Write the next version of a package (version 1 for a new one)."""

    def __init__(self, root: str | Path, name: str):
        self.root = Path(root)
        if self.root.exists() and not self.root.is_dir():
            raise ToolkitError(f"{self.root} exists and is not a folder")
        if self.root.is_dir() and any(self.root.iterdir()):
            if not ((self.root / INDEX).is_file() or _versions(self.root)):
                raise ToolkitError(
                    f"{self.root} is not empty and is not a DocForge template package",
                    hint="Choose a new folder for the package.",
                )
        index = self._index()
        self.name = name or index.get("name") or "Template"
        self.template_id = index.get("template_id") or slugify_field(self.name, fallback="template")
        self.version = (max(_versions(self.root)) + 1) if _versions(self.root) else 1

    def _index(self) -> dict:
        path = self.root / INDEX
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {}
        return {}

    def write(
        self,
        *,
        template_docx: bytes,
        labels_docx: bytes | None,
        intelligence: TemplateIntelligence,
        fields: list[FieldDefinition],
        rules: list[ValidationRule],
        review: ReviewSnapshot,
        source_examples: dict[str, bytes],
        extracted_sources: dict[str, dict],
        representative_extraction: dict,
        representative_docx: bytes,
        notes: str = "",
    ) -> TemplateVersion:
        vdir = self.root / str(self.version)
        if vdir.exists():
            raise ToolkitError(f"{vdir} already exists")
        vdir.mkdir(parents=True)
        manifest = TemplateManifest(
            template_id=self.template_id,
            version=self.version,
            name=self.name,
            source_file_names=list(source_examples),
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            created_by="docforge-claude-plugin",
            renderer="docxtpl",
            model_used_for_analysis="claude",
            notes=notes,
        )
        (vdir / TEMPLATE_DOCX).write_bytes(template_docx)
        if labels_docx is not None:
            (vdir / LABELS_DOCX).write_bytes(labels_docx)
        _write_json(vdir / INTELLIGENCE, intelligence.model_dump(mode="json"))
        _write_json(vdir / FIELDS, [f.model_dump(mode="json") for f in fields])
        _write_json(vdir / RULES, [r.model_dump(mode="json") for r in rules])
        _write_json(vdir / MANIFEST, manifest.model_dump(mode="json"))
        _write_json(vdir / REVIEW, review.model_dump(mode="json"))
        _write_json(vdir / REPRESENTATIVE, representative_extraction)
        (vdir / REPRESENTATIVE_DOCX).write_bytes(representative_docx)
        for fname, data in source_examples.items():
            dest = vdir / SOURCES / safe_filename(fname)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        for fname, data in extracted_sources.items():
            _write_json(vdir / EXTRACTED / (safe_filename(fname) + ".json"), data)

        versions = _versions(self.root)
        _write_json(self.root / INDEX, {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "template_id": self.template_id,
            "name": self.name,
            "document_type": intelligence.document_type_guess,
            "latest_version": max(versions),
            "versions": versions,
        })
        readme = self.root / "README.txt"
        if not readme.exists():
            readme.write_text(_README.format(name=self.name), encoding="utf-8")
        return TemplateVersion(root=vdir, package_root=self.root)


def zip_package(package_root: str | Path, out: str | Path) -> Path:
    """Zip a package folder, with the folder as the single top-level entry."""
    root = Path(package_root)
    if not ((root / INDEX).is_file() or _versions(root)):
        raise ToolkitError(f"{root} is not a DocForge template package")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(root.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                zf.write(path, Path(root.name) / path.relative_to(root))
    return out
