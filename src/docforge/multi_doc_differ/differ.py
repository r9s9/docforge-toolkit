"""multi_doc_differ — compare 2+ same-type documents structurally (spec §8).

Strategy:
  1. Pick a representative document (the most complete one).
  2. Align each other document's top-level elements to the representative using
     difflib over *loose, value-independent signatures* — so "Project Name: A"
     and "Project Name: B" align even though their text differs, and shifted
     positions still match.
  3. For each aligned node, decide IDENTICAL / CHANGED / PARTIAL_CHANGE (with a
     static prefix/suffix split) / ROW_COUNT_CHANGED / IMAGE_CHANGED, attaching a
     confidence that grows with the number of corroborating samples.

The output is evidence for the classifier — it never decides field names itself.
"""

from __future__ import annotations

import difflib
from collections import Counter

from ..common.textutil import common_skeleton, value_kind
from ..schemas.diff import DiffRunResult, NodeDiff
from ..schemas.enums import DiffStatus, ElementType
from ..schemas.extraction import DocumentExtraction, NormalizedElement


def pick_representative(extractions: list[DocumentExtraction]) -> int:
    """Index of the document with the most top-level elements (ties -> first)."""
    best_i, best_n = 0, -1
    for i, e in enumerate(extractions):
        n = len(e.top_level_elements())
        if n > best_n:
            best_i, best_n = i, n
    return best_i


def _len_bucket(n: int) -> int:
    for i, edge in enumerate((1, 20, 80, 200)):
        if n < edge:
            return i
    return 4


def align_signature(e: NormalizedElement) -> str:
    """A signature stable across value changes (used only for alignment)."""
    if e.type == ElementType.HEADING:
        return f"h:{e.text.strip().lower()}"
    if e.type == ElementType.TABLE:
        hdrs = "|".join(e.table_structure.headers).lower() if e.table_structure else ""
        return f"t:{hdrs}"
    if e.type == ElementType.IMAGE or e.image_ref is not None:
        return "img"
    txt = e.text.strip()
    if ":" in txt and len(txt.split(":", 1)[0]) <= 40:
        return f"p:{txt.split(':', 1)[0].lower().strip()}"
    style = (e.style_name or "").lower()
    return f"p:{style}:{_len_bucket(len(txt))}"


def _align(rep_sigs: list[str], other_sigs: list[str]) -> list[tuple[int | None, int | None]]:
    """Return aligned index pairs (rep_idx, other_idx); None marks insert/delete."""
    sm = difflib.SequenceMatcher(a=rep_sigs, b=other_sigs, autojunk=False)
    pairs: list[tuple[int | None, int | None]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                pairs.append((i1 + k, j1 + k))
        elif tag == "replace":
            span = max(i2 - i1, j2 - j1)
            for k in range(span):
                pairs.append(
                    (i1 + k if k < i2 - i1 else None, j1 + k if k < j2 - j1 else None)
                )
        elif tag == "delete":
            for k in range(i1, i2):
                pairs.append((k, None))
        elif tag == "insert":
            for k in range(j1, j2):
                pairs.append((None, k))
    return pairs


def align_to_representative(rep: DocumentExtraction, other: DocumentExtraction) -> dict[str, NormalizedElement]:
    """Map each representative top-level node_id to the aligned element in ``other``.

    Used by the compliance checker to line a candidate document up against a
    template's representative structure.
    """
    rep_top = rep.top_level_elements()
    other_top = other.top_level_elements()
    rep_sigs = [align_signature(e) for e in rep_top]
    other_sigs = [align_signature(e) for e in other_top]
    out: dict[str, NormalizedElement] = {}
    for ri, oj in _align(rep_sigs, other_sigs):
        if ri is not None and oj is not None:
            out[rep_top[ri].node_id] = other_top[oj]
    return out


def _confidence(n_docs: int) -> float:
    return min(0.55 + 0.2 * (n_docs - 1), 0.95)


def _majority_kind(values: list[str]) -> str:
    kinds = [value_kind(v) for v in values if v and v.strip()]
    if not kinds:
        return "text"
    return Counter(kinds).most_common(1)[0][0]


def _label_split(texts: list[str]) -> tuple[str, list[str]] | None:
    """If every sample shares an identical ``Label:`` prefix, return
    (label_prefix, [values...]). Treating the whole post-label value as the
    dynamic token avoids over-fitting (e.g. hard-coding the year in a date).
    """
    prefixes = []
    for t in texts:
        idx = t.find(":")
        if idx == -1:
            return None
        prefixes.append(t[: idx + 1])
    if len(set(prefixes)) != 1:
        return None
    label = prefixes[0]
    values = [t[len(label):] for t in texts]
    # Absorb a consistent single leading space into the static label.
    if all(v.startswith(" ") for v in values):
        label += " "
        values = [v[1:] for v in values]
    if len(set(values)) <= 1:
        return None  # the value itself is constant -> not dynamic
    return label, values


def _diff_text(rel, matched, n_docs, missing) -> NodeDiff:
    texts = [rel.text] + [e.text for e in matched]
    distinct = list(dict.fromkeys(texts))
    conf = _confidence(n_docs)
    note = "absent in some samples" if missing else ""

    if len(distinct) == 1:
        return NodeDiff(
            node_key=rel.node_id,
            representative_node_id=rel.node_id,
            status=DiffStatus.IDENTICAL,
            type=rel.type,
            sample_texts=distinct,
            confidence=conf,
            is_constant=(missing == 0),
            notes=note,
        )

    # Prefer a clean "Label: value" split (keeps the whole value as one token).
    label = _label_split(texts)
    if label is not None:
        label_prefix, values = label
        return NodeDiff(
            node_key=rel.node_id,
            representative_node_id=rel.node_id,
            status=DiffStatus.PARTIAL_CHANGE,
            type=rel.type,
            sample_texts=distinct,
            confidence=conf,
            is_constant=False,
            static_prefix=label_prefix,
            static_suffix="",
            variable_parts=list(dict.fromkeys(values)),
            detected_kind=_majority_kind(values),
            notes=note,
        )

    # Otherwise fall back to character-level skeleton for embedded changes.
    prefix, suffix, middles = common_skeleton(texts)
    if prefix is not None or suffix is not None:
        return NodeDiff(
            node_key=rel.node_id,
            representative_node_id=rel.node_id,
            status=DiffStatus.PARTIAL_CHANGE,
            type=rel.type,
            sample_texts=distinct,
            confidence=conf,
            is_constant=False,
            static_prefix=prefix or "",
            static_suffix=suffix or "",
            variable_parts=list(dict.fromkeys(middles)),
            detected_kind=_majority_kind(middles),
            notes=note,
        )

    return NodeDiff(
        node_key=rel.node_id,
        representative_node_id=rel.node_id,
        status=DiffStatus.CHANGED,
        type=rel.type,
        sample_texts=distinct,
        confidence=conf,
        is_constant=False,
        detected_kind=_majority_kind(distinct),
        notes=note,
    )


def _diff_table(rel, matched, n_docs, missing) -> NodeDiff:
    tss = [rel.table_structure] + [e.table_structure for e in matched if e.table_structure]
    tss = [ts for ts in tss if ts]
    headers = {tuple(ts.headers) for ts in tss}
    row_counts = [ts.n_rows for ts in tss]
    grids = {tuple(tuple(r) for r in ts.rows) for ts in tss}

    header_identical = len(headers) <= 1
    row_count_variable = len(set(row_counts)) > 1
    all_identical = len(grids) <= 1
    conf = _confidence(n_docs)

    if all_identical:
        status = DiffStatus.IDENTICAL
    elif row_count_variable:
        status = DiffStatus.ROW_COUNT_CHANGED
    else:
        status = DiffStatus.CHANGED

    return NodeDiff(
        node_key=rel.node_id,
        representative_node_id=rel.node_id,
        status=status,
        type=ElementType.TABLE,
        sample_texts=[" | ".join(rel.table_structure.headers)] if rel.table_structure else [],
        confidence=conf,
        is_constant=all_identical,
        header_identical=header_identical,
        row_count_variable=row_count_variable,
        row_counts=row_counts,
        notes="absent in some samples" if missing else "",
    )


def _diff_image(rel, matched, n_docs) -> NodeDiff:
    def h(el):
        return el.image_ref.hash if el.image_ref else None

    hashes = [h(rel)] + [h(e) for e in matched]
    hashes = [x for x in hashes if x]
    same = len(set(hashes)) <= 1
    return NodeDiff(
        node_key=rel.node_id,
        representative_node_id=rel.node_id,
        status=DiffStatus.IDENTICAL if same else DiffStatus.IMAGE_CHANGED,
        type=rel.type,
        confidence=_confidence(n_docs),
        is_constant=same,
        image_hashes=list(dict.fromkeys(hashes)),
    )


def diff_documents(
    extractions: list[DocumentExtraction],
    representative_index: int | None = None,
) -> DiffRunResult:
    """Compare 2+ extractions and return per-node diff evidence."""
    if len(extractions) < 2:
        raise ValueError("diff_documents requires at least 2 documents")

    rep_i = representative_index if representative_index is not None else pick_representative(extractions)
    rep = extractions[rep_i]
    rep_top = rep.top_level_elements()
    rep_sigs = [align_signature(e) for e in rep_top]

    # Build rep_idx -> aligned element, for every *other* document.
    aligned_per_doc: list[dict[int, NormalizedElement]] = []
    for i, od in enumerate(extractions):
        if i == rep_i:
            continue
        od_top = od.top_level_elements()
        od_sigs = [align_signature(e) for e in od_top]
        mapping: dict[int, NormalizedElement] = {}
        for ri, oj in _align(rep_sigs, od_sigs):
            if ri is not None and oj is not None:
                mapping[ri] = od_top[oj]
        aligned_per_doc.append(mapping)

    n_docs = len(extractions)
    node_diffs: list[NodeDiff] = []
    for ri, rel in enumerate(rep_top):
        matched = [m.get(ri) for m in aligned_per_doc]
        present = [x for x in matched if x is not None]
        missing = sum(1 for x in matched if x is None)
        if rel.type == ElementType.TABLE:
            nd = _diff_table(rel, present, n_docs, missing)
        elif rel.type == ElementType.IMAGE or rel.image_ref is not None:
            nd = _diff_image(rel, present, n_docs)
        else:
            nd = _diff_text(rel, present, n_docs, missing)
        nd.is_optional = missing > 0
        node_diffs.append(nd)

    summary = dict(Counter(d.status.value for d in node_diffs))
    return DiffRunResult(
        document_ids=[e.document_id for e in extractions],
        n_documents=n_docs,
        representative_document_id=rep.document_id,
        node_diffs=node_diffs,
        summary=summary,
    )
