"""Normalized extraction schema (see spec §7).

This is the structural representation produced by the ooxml_extractor +
structure_normalizer. It is renderer-agnostic and fully serializable to JSON,
so it can be snapshotted, diffed, and fed to the AI classifier.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .enums import ElementType


class RunFormatting(BaseModel):
    """Character-level formatting for a run of text."""

    font: str | None = None
    size: float | None = None  # points
    bold: bool | None = None
    italic: bool | None = None
    underline: bool | None = None
    color: str | None = None  # hex RGB, e.g. "FF0000"
    highlight: str | None = None  # named highlight color


class Run(BaseModel):
    """A contiguous run of text with uniform formatting."""

    text: str = ""
    formatting: RunFormatting = Field(default_factory=RunFormatting)


class ParagraphFormatting(BaseModel):
    """Block-level + dominant character formatting for an element.

    Many fields are best-effort: when the source XML does not specify a value
    it is left ``None`` so the differ does not treat absence as a difference.
    """

    style_name: str | None = None
    alignment: str | None = None  # left/center/right/justify
    font: str | None = None
    size: float | None = None
    bold: bool | None = None
    italic: bool | None = None
    underline: bool | None = None
    color: str | None = None
    shading: str | None = None  # cell/paragraph fill color (hex)
    borders: dict[str, str] = Field(default_factory=dict)  # side -> style
    margins: dict[str, int] = Field(default_factory=dict)  # twips, best-effort


class NumberingInfo(BaseModel):
    """List numbering association for a paragraph."""

    num_id: str | None = None
    level: int | None = None
    is_ordered: bool | None = None  # True=numbered, False=bullet
    list_text: str | None = None  # resolved marker, best-effort


class ImageRef(BaseModel):
    """Reference to an embedded image."""

    relationship_id: str | None = None
    file_name: str | None = None
    hash: str | None = None  # sha256 of image bytes (for cross-doc sameness)
    width_emu: int | None = None
    height_emu: int | None = None
    content_type: str | None = None


class TableStructure(BaseModel):
    """Normalized table grid (best-effort; merged cells captured separately)."""

    n_rows: int = 0
    n_cols: int = 0
    headers: list[str] = Field(default_factory=list)  # first-row text per column
    rows: list[list[str]] = Field(default_factory=list)  # cell text grid
    merged_cells: list[dict] = Field(default_factory=list)  # {row,col,rowspan,colspan}
    col_widths: list[int | None] = Field(default_factory=list)  # twips, best-effort


class NormalizedElement(BaseModel):
    """A single normalized structural node (spec §7 "Element")."""

    node_id: str
    parent_node_id: str | None = None
    xpath: str  # stable structural reference within the package part
    type: ElementType
    subtype: str | None = None  # e.g. heading level "1", list "bullet"
    style_name: str | None = None
    text: str = ""
    runs: list[Run] = Field(default_factory=list)
    table_structure: TableStructure | None = None
    image_ref: ImageRef | None = None
    numbering_info: NumberingInfo | None = None
    position_index: int = 0  # order within its scope
    header_footer_scope: str | None = None  # None=body; else "header:rId3" etc.
    section_index: int = 0
    formatting: ParagraphFormatting = Field(default_factory=ParagraphFormatting)
    semantic_hints: list[str] = Field(default_factory=list)
    # Bookmarks this paragraph carries, and the bookmark it links to: a table
    # of contents entry links to the bookmark Word put on its heading.
    bookmarks: list[str] = Field(default_factory=list)
    link_target: str | None = None


class DocumentSection(BaseModel):
    """A Word section (sectPr): page geometry + header/footer linkage."""

    section_index: int = 0
    page_width: int | None = None  # twips
    page_height: int | None = None
    margins: dict[str, int] = Field(default_factory=dict)
    header_refs: dict[str, str] = Field(default_factory=dict)  # type -> rId
    footer_refs: dict[str, str] = Field(default_factory=dict)


class DocumentExtraction(BaseModel):
    """Top-level normalized representation of one DOCX file."""

    document_id: str
    filename: str
    page_count: int | None = None  # only when available (e.g. docProps/app.xml)
    content_hash: str = ""  # hash over normalized element texts
    sections: list[DocumentSection] = Field(default_factory=list)
    elements: list[NormalizedElement] = Field(default_factory=list)

    # --- convenience accessors -------------------------------------------
    def body_elements(self) -> list[NormalizedElement]:
        return [e for e in self.elements if e.header_footer_scope is None]

    def by_id(self, node_id: str) -> NormalizedElement | None:
        for e in self.elements:
            if e.node_id == node_id:
                return e
        return None

    def top_level_elements(self) -> list[NormalizedElement]:
        """Elements whose parent is the document body (not nested in a table)."""
        return [e for e in self.elements if e.parent_node_id is None]
