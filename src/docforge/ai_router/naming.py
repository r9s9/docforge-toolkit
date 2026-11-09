"""Match the field names a model returns to the ones a template actually has.

Templates built from a document carry machine-made names — ``header_level_1_title``,
``tables_section_title``, ``body_body``. A model asked to fill them will often
answer with the name it *understood* rather than the exact string: dropping the
suffix, using the label, or tidying the wording. Requiring an exact match meant
a response could be entirely correct and entirely discarded, leaving a document
with nothing placed and no explanation.

Matching is deliberately narrow. An exact name wins, then the same name once
slugified, then a close spelling — and only when it is close enough that the two
could not plausibly be different fields. Anything below that is reported rather
than guessed at, because putting content in the wrong section of someone's
document is worse than leaving it out.
"""

from __future__ import annotations

from ..common.textutil import similarity, slugify_field
from ..schemas.template import FieldDefinition

# How alike two slugified names must be to be treated as the same field.
# High on purpose: "review_scope" and "review_summary" must never merge.
_MIN_NAME_SIMILARITY = 0.86


def build_name_resolver(fields: list[FieldDefinition]):
    """Return ``resolve(name) -> real field name or None`` for ``fields``."""
    exact = {f.field_name for f in fields}
    by_slug: dict[str, str] = {}
    for f in fields:
        # The field's own name is authoritative; its label is a fallback key, so
        # a model answering "Overview" still finds the field labelled Overview.
        by_slug.setdefault(slugify_field(f.field_name, fallback=""), f.field_name)
    for f in fields:
        if f.label:
            by_slug.setdefault(slugify_field(f.label, fallback=""), f.field_name)

    def resolve(name: str | None) -> str | None:
        if not name:
            return None
        if name in exact:
            return name
        slug = slugify_field(name, fallback="")
        if not slug:
            return None
        if slug in by_slug:
            return by_slug[slug]
        best, best_score = None, 0.0
        for candidate, real in by_slug.items():
            score = similarity(slug, candidate)
            if score > best_score:
                best, best_score = real, score
        return best if best_score >= _MIN_NAME_SIMILARITY else None

    return resolve
