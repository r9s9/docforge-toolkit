"""Markdown-lite rich values: the format field content is written in.

A field value is normally a plain string that lands in one Word run. That makes
multi-paragraph content impossible: newlines inside a ``{{ field }}`` collapse,
so a three-paragraph summary renders as one wall of text.

This module defines the small text format the AI writes (and users can type)
and parses it into blocks the assembler turns into real Word paragraphs:

    blank line or newline  -> new paragraph
    "- item"               -> bullet
    "1. item"              -> numbered item
    "**bold**"             -> bold run
    "*italic*"             -> italic run

Deliberately *not* full Markdown: no headings, links, code or tables — those
belong to the template's own design, not to a value dropped into it.

Parsing is pure (no docx imports) so it can be unit-tested directly; the
rendering half lives in :mod:`docforge.assembler.postprocess`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

# The canonical description of this format, shared with the AI prompts so the
# instructions and the parser can never drift apart.
RICH_FORMAT_SPEC = """\
Formatting inside a text value (kept simple on purpose):
- Separate paragraphs with a blank line. Do NOT write one long run-on block.
- Start a line with "- " for a bullet, or "1. " for a numbered item.
- Wrap words in **double asterisks** for bold, *single asterisks* for italic.
- Nothing else (no headings, tables, links or code) — the template supplies the
  design; the value supplies only the words and their structure.\
"""

BlockKind = Literal["paragraph", "bullet", "number"]

# A line is a list item when it opens with a marker. Tabs count as indentation.
_BULLET_RE = re.compile(r"^([ \t]*)[-*•]\s+(.*)$")
_NUMBER_RE = re.compile(r"^([ \t]*)(\d+)[.)]\s+(.*)$")

# Emphasis. Both alternatives require non-space content so prose like
# "5 * 4" or a trailing footnote "Terms apply*" is left alone.
_EMPH_RE = re.compile(r"\*\*(?P<bold>\S(?:[^*]*\S)?)\*\*|\*(?P<italic>\S(?:[^*]*\S)?)\*")

# Two spaces of indent per nesting level, capped so a stray deep indent can't
# push text off the page.
_INDENT_PER_LEVEL = 2
_MAX_LEVEL = 4


@dataclass
class RichSpan:
    """A run of text with uniform emphasis."""

    text: str
    bold: bool = False
    italic: bool = False


@dataclass
class RichBlock:
    """One rendered paragraph: its kind, nesting level and formatted spans."""

    kind: BlockKind = "paragraph"
    level: int = 0
    spans: list[RichSpan] = field(default_factory=list)
    marker: str = ""  # literal fallback marker ("•" / "2.") when the package
    # has no reusable numbering definition to attach.

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.spans)


def is_rich(value: object) -> bool:
    """Whether ``value`` needs block rendering rather than a plain run.

    Kept cheap: this runs for every field of every generation, and a False
    answer must leave the existing render path completely untouched.
    """
    if not isinstance(value, str) or not value.strip():
        return False
    if "\n" in value:
        return True
    if _BULLET_RE.match(value) or _NUMBER_RE.match(value):
        return True
    return bool(_EMPH_RE.search(value))


def parse_spans(text: str) -> list[RichSpan]:
    """Split one line into emphasis spans, dropping the markers themselves."""
    spans: list[RichSpan] = []
    pos = 0
    for m in _EMPH_RE.finditer(text):
        if m.start() > pos:
            spans.append(RichSpan(text[pos : m.start()]))
        if m.group("bold") is not None:
            spans.append(RichSpan(m.group("bold"), bold=True))
        else:
            spans.append(RichSpan(m.group("italic"), italic=True))
        pos = m.end()
    if pos < len(text):
        spans.append(RichSpan(text[pos:]))
    return spans or [RichSpan(text)]


def _level(indent: str) -> int:
    return min(_MAX_LEVEL, len(indent.replace("\t", " " * 4)) // _INDENT_PER_LEVEL)


def parse_rich_blocks(value: str) -> list[RichBlock]:
    """Parse a markdown-lite value into ordered blocks.

    Every newline starts a new block, not just blank lines: a model that
    separates paragraphs with a single "\\n" means the same thing a user does,
    and joining those lines back together is precisely the bug this format
    exists to fix. Blank lines are therefore separators, never content.
    """
    blocks: list[RichBlock] = []
    for raw_line in (value or "").splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        bullet = _BULLET_RE.match(line)
        if bullet:
            indent, content = bullet.groups()
            blocks.append(
                RichBlock("bullet", _level(indent), parse_spans(content.strip()), "•")
            )
            continue
        number = _NUMBER_RE.match(line)
        if number:
            indent, digits, content = number.groups()
            blocks.append(
                RichBlock("number", _level(indent), parse_spans(content.strip()), f"{digits}.")
            )
            continue
        blocks.append(RichBlock("paragraph", 0, parse_spans(line.strip())))
    return blocks


def strip_markers(line: str) -> str:
    """Plain text for one line — list marker and emphasis characters removed.

    Used where a value is consumed as plain strings (repeatable sections), so
    markdown a user pasted there doesn't leak literal "- " / "**" into Word.
    """
    for pattern in (_BULLET_RE, _NUMBER_RE):
        m = pattern.match(line)
        if m:
            line = m.groups()[-1]
            break
    return "".join(s.text for s in parse_spans(line.strip()))
