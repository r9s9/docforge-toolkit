"""docforge-live: the command line the DocForge.live skills run.

Run it as `docforge-live` or `python3 -m docforge.toolkit`.

Every command prints one JSON object. On failure it prints
{"ok": false, "error": ..., "hint": ...} and exits with status 1.

Make a template:
  analyze <examples.docx…> --work DIR [--mode full|smart]   full (default): every text is a field
  outline --work DIR [--only all|review|fields] [--offset N] [--limit N]
  node | neighbors | evidence --work DIR <node_id>
  review --work DIR                      check decisions.json, list the resulting fields
  preview --work DIR --out FILE.docx [--tags] [--pdf]
  build --work DIR --name NAME --out PACKAGE_DIR [--zip FILE.zip] [--deliver DIR]

Fill a template:
  fields PACKAGE                         what each field holds and where it sits
  read DOCUMENT.docx                     a block-id outline of any document
  route-doc PACKAGE DOCUMENT.docx        map a same-kind document onto the fields
  render PACKAGE --values FILE --out FILE.docx [--defaults FILE] [--pdf] [--blocks]
  render PACKAGE --batch ROWS.csv|json|jsonl --out-dir DIR [--name-pattern PATTERN]
  sample PACKAGE --out FILE.docx         the template with «Label» values

Check a document:
  check PACKAGE DOCUMENT.docx [--verdicts FILE] [--report FILE.json] [--markdown FILE.md]
  fix PACKAGE DOCUMENT.docx --out FIXED.docx [--strict]

Utilities:
  doctor                                 Python and package check, with install command
  demo --out DIR                         sample documents to try everything on
  pdf FILE.docx [--out-dir DIR]          convert with LibreOffice
  zip PACKAGE_DIR --out FILE.zip         package a template folder for download
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .common import ToolkitError, read_json, to_json


def _emit(obj) -> None:
    sys.stdout.write(to_json(obj) + "\n")
    sys.stdout.flush()


# --- make a template --------------------------------------------------------


def cmd_analyze(a):
    from .analysis import analyze

    return analyze(a.files, a.work, a.mode)


def cmd_outline(a):
    from .analysis import load_workspace, outline

    return outline(
        load_workspace(a.work), offset=a.offset, limit=a.limit, only=a.only,
        text_chars=a.text_chars,
    )


def cmd_node(a):
    from .analysis import load_workspace, node

    return node(load_workspace(a.work), a.node_id)


def cmd_neighbors(a):
    from .analysis import load_workspace, neighbors

    return neighbors(load_workspace(a.work), a.node_id, a.radius)


def cmd_evidence(a):
    from .analysis import evidence, load_workspace

    return evidence(load_workspace(a.work), a.node_id)


def _draft(a):
    from .analysis import apply_decisions, load_decisions, load_workspace

    ws = load_workspace(a.work)
    return ws, apply_decisions(ws, load_decisions(ws, a.decisions))


def cmd_review(a):
    ws, draft = _draft(a)
    by_id = {e.node_id: e for e in ws.rep.elements}
    fields = []
    for f in draft.fields:
        el = by_id.get(f.node_ids[0]) if f.node_ids else None
        example = " ".join((el.text if el else "").split())
        item = {
            "field_name": f.field_name,
            "label": f.label,
            "type": f.field_type.value,
            "required": f.required,
            "description": f.description,
        }
        if f.columns:
            item["columns"] = [c.field_name for c in f.columns]
        if f.section_key:
            item["section"] = f.section_key
        if example:
            item["example"] = example[:80] + ("…" if len(example) > 80 else "")
        fields.append(item)
    return {
        "document_type": draft.result.document_type_guess,
        "sections": [s.section_key for s in draft.result.sections],
        "fields": fields,
        "warnings": draft.warnings,
    }


def cmd_preview(a):
    from .analysis import preview

    ws, draft = _draft(a)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(preview(ws, draft, tags=a.tags))
    result = {"output": str(out), "kind": "tags" if a.tags else "labels", "warnings": draft.warnings}
    if a.pdf:
        from .render import to_pdf

        result["pdf"] = str(to_pdf(out))
    return result


def cmd_build(a):
    from .analysis import build
    from .package import zip_package

    ws, draft = _draft(a)
    tv, warnings = build(ws, draft, out=a.out, name=a.name, notes=a.notes or "")
    result = {
        **tv.summary(),
        "fields": [
            {"field_name": f.field_name, "type": f.field_type.value, "required": f.required}
            for f in draft.fields
        ],
        "warnings": warnings,
    }
    if a.zip:
        result["zip"] = str(zip_package(tv.package_root, a.zip))
    result["hand_over"] = _hand_over(result, a)
    result["next"] = (
        "Give the person the files in hand_over now, in the same reply as your summary "
        "(SKILL.md, \"Hand over the files\")."
    )
    return result


def _hand_over(result: dict, a) -> list[str]:
    """The files the person gets: the template, its readable copy, and the zip.

    With --deliver, the two Word files are also copied there under the
    template's name, so they can sit next to other templates' files.
    """
    import shutil

    from .common import safe_filename

    docs = [("template", result["template_docx"]), ("with labels", result.get("with_labels_docx"))]
    files = []
    for label, src in docs:
        if not src:
            continue
        if a.deliver:
            dest = Path(a.deliver)
            dest.mkdir(parents=True, exist_ok=True)
            target = dest / safe_filename(f"{a.name} - {label}.docx", f"{label}.docx")
            shutil.copyfile(src, target)
            files.append(str(target))
        else:
            files.append(src)
    if result.get("zip"):
        files.append(result["zip"])
    return files


# --- fill a template --------------------------------------------------------


def _open(a):
    from .package import open_template

    return open_template(a.package, a.version)


def cmd_fields(a):
    from .fill import describe

    return describe(_open(a))


def cmd_read(a):
    from .fill import read_document

    doc, text = read_document(a.document)
    return {"file": a.document, "elements": len(doc.top_level_elements()), "outline": text}


def cmd_route_doc(a):
    from .common import write_json
    from .fill import route_document

    result = route_document(_open(a), a.document)
    if a.out and result.get("method") == "structural":
        write_json(a.out, {"values": result["values"], "missing": result.get("missing_required", [])})
        result["values_file"] = a.out
    return result


def cmd_render(a):
    from .fill import load_values, read_rows, render, render_batch, render_summary

    tv = _open(a)
    defaults = read_json(a.defaults) if a.defaults else None
    if defaults is not None and not isinstance(defaults, dict):
        raise ToolkitError("--defaults must be a JSON object of field values")
    if a.batch:
        if not a.out_dir:
            raise ToolkitError("--batch needs --out-dir")
        rows = read_rows(a.batch)
        result = render_batch(
            tv, rows, a.out_dir, defaults=defaults, name_pattern=a.name_pattern,
            base_dir=Path(a.batch).parent,
        )
        if a.pdf:
            from .render import to_pdf

            for item in result["documents"]:
                if "output" in item:
                    item["pdf"] = str(to_pdf(item["output"]))
        return result
    if not a.values or not a.out:
        raise ToolkitError("render needs --values FILE and --out FILE.docx (or --batch)")
    values = load_values(a.values)
    r = render(
        tv, values, defaults=defaults, skip_validation=a.skip_validation,
        base_dir=Path(a.values).parent,
    )
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(r.docx)
    result = render_summary(tv, r, output=out, blocks=a.blocks)
    if a.pdf:
        from .render import to_pdf

        result["pdf"] = str(to_pdf(out))
    return result


def cmd_sample(a):
    from .samples import labels_docx

    tv = _open(a)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(labels_docx(tv.template_bytes(), tv.fields()))
    return {"output": str(out)}


# --- check a document -------------------------------------------------------


def cmd_check(a):
    from .common import write_json
    from .compliance import apply_verdicts, check, fixed_text_count, markdown, summary

    tv = _open(a)
    report = check(tv, a.document)
    result = {}
    if a.verdicts:
        result["verdicts_applied"] = apply_verdicts(report, read_json(a.verdicts))
    result.update(summary(report, limit=a.limit))
    result["fixed_text_checked"] = fixed_text_count(tv)
    if not result["fixed_text_checked"]:
        result["template_note"] = (
            "This template has no fixed wording: every text is a field (a Full template). "
            "The check compares structure, formatting and fields, not wording. To check "
            "wording, make a Smart template from the same examples."
        )
    if a.report:
        write_json(a.report, report.model_dump(mode="json"))
        result["report"] = a.report
    if a.markdown:
        path = Path(a.markdown)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown(report, tv.name), encoding="utf-8")
        result["markdown"] = str(path)
    return result


def cmd_fix(a):
    from .common import check_docx
    from .compliance import check
    from .fixer import fix_against

    tv = _open(a)
    data = check_docx(a.document)
    before = check(tv, a.document)
    rep = tv.representative()
    if rep is None:
        raise ToolkitError("this template version has no stored structure; rebuild it")
    fixed, n = fix_against(
        rep, tv.intelligence().classifications, data, Path(a.document).name, strict=a.strict,
    )
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(fixed)
    after = check(tv, out)
    return {
        "output": str(out),
        "fixes_applied": n,
        "score_before": before.score,
        "score_after": after.score,
        "grade_after": after.grade,
        "remaining_differences": len(after.differences),
    }


# --- utilities --------------------------------------------------------------


def cmd_doctor(a):
    from .deps import doctor

    return doctor()


def cmd_demo(a):
    from ..sampledata import _INVOICE_TERMS, build_invoice, write_all

    out = Path(a.out)
    written = write_all(out)
    # An "edited" invoice whose boilerplate drifted, for check and fix: an
    # obvious change (the title) and a subtle one (the payment window).
    doc = build_invoice(1)
    for p in doc.paragraphs:
        if p.text == _INVOICE_TERMS:
            p.text = p.text.replace("30 days", "90 days")
        elif p.text == "INVOICE":
            p.runs[0].text = "PRO FORMA INVOICE"
    edited = out / "invoice_edited.docx"
    doc.save(str(edited))
    return {
        "out_dir": str(out),
        "examples": {kind: [str(p) for p in paths] for kind, paths in written.items()},
        "edited_for_compliance": str(edited),
        "try": [
            "analyze <out>/invoice_1.docx <out>/invoice_2.docx --work invoice-work",
            "check <package> <out>/invoice_edited.docx",
        ],
    }


def cmd_pdf(a):
    from .render import to_pdf

    return {"pdf": str(to_pdf(a.document, a.out_dir))}


def cmd_zip(a):
    from .package import zip_package

    return {"zip": str(zip_package(a.package_dir, a.out))}


# --- parser -----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="docforge-live", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    def add(name, func, help_text):
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.set_defaults(func=func)
        return p

    def work(p):
        p.add_argument("--work", required=True, help="analysis workspace folder")

    def package(p):
        p.add_argument("package", help="template package folder, version folder or .zip")
        p.add_argument("--version", type=int, help="template version (default: latest)")

    p = add("analyze", cmd_analyze, "read, compare and classify 1-5 example .docx files")
    p.add_argument("files", nargs="+", help="example .docx files of the same kind")
    work(p)
    p.add_argument("--mode", choices=["full", "smart"], default="full",
                   help="full (default, the app's \"Full template\"): every text becomes a field; "
                        "smart (\"Smart detect\"): boilerplate stays fixed, only what varies is a field")

    p = add("outline", cmd_outline, "the representative document with proposal and evidence")
    work(p)
    p.add_argument("--only", choices=["all", "review", "fields"], default="all")
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--limit", type=int, default=80, help="0 for no limit")
    p.add_argument("--text-chars", type=int, default=200, help="truncate text (0: full)")

    for name, func, text in (
        ("node", cmd_node, "full text, formatting, proposal and evidence of one element"),
        ("evidence", cmd_evidence, "how one element varies across the examples"),
    ):
        p = add(name, func, text)
        work(p)
        p.add_argument("node_id")
    p = add("neighbors", cmd_neighbors, "the elements around one element")
    work(p)
    p.add_argument("node_id")
    p.add_argument("--radius", type=int, default=2)

    for name, func, text in (
        ("review", cmd_review, "apply decisions.json (dry run) and list the resulting fields"),
        ("preview", cmd_preview, "write the template these decisions produce"),
        ("build", cmd_build, "build the template and write a package version"),
    ):
        p = add(name, func, text)
        work(p)
        p.add_argument("--decisions", help="decisions file (default: <work>/decisions.json)")
        if name == "preview":
            p.add_argument("--out", required=True)
            p.add_argument("--tags", action="store_true", help="show {{ tags }} instead of «Labels»")
            p.add_argument("--pdf", action="store_true")
        if name == "build":
            p.add_argument("--name", required=True, help="the template's display name")
            p.add_argument("--out", required=True, help="package folder (a new version if it exists)")
            p.add_argument("--notes", help="a note stored with this version")
            p.add_argument("--zip", help="also write a .zip of the package here")
            p.add_argument("--deliver", help="also copy template.docx and with-labels.docx into this "
                                              "folder, named after the template (the chat outputs folder)")

    p = add("fields", cmd_fields, "describe a template's fields and sections")
    package(p)

    p = add("read", cmd_read, "a block-id outline of any .docx")
    p.add_argument("document")

    p = add("route-doc", cmd_route_doc, "map a same-kind document onto a template's fields")
    package(p)
    p.add_argument("document")
    p.add_argument("--out", help="write the mapped values here (structural matches only)")

    p = add("render", cmd_render, "fill a template from a values file, or from rows")
    package(p)
    p.add_argument("--values", help="values JSON file")
    p.add_argument("--out", help="output .docx")
    p.add_argument("--batch", help="rows file: .csv, .json or .jsonl")
    p.add_argument("--out-dir", help="output folder for --batch")
    p.add_argument("--name-pattern", help="batch file names, e.g. '{invoice_number}-{bill_to}'")
    p.add_argument("--defaults", help="JSON of shared values (project metadata)")
    p.add_argument("--pdf", action="store_true", help="also convert to PDF")
    p.add_argument("--blocks", action="store_true", help="include the rendered text, for review")
    p.add_argument("--skip-validation", action="store_true")

    p = add("sample", cmd_sample, "the template with «Label» values")
    package(p)
    p.add_argument("--out", required=True)

    p = add("check", cmd_check, "score a document against its template")
    package(p)
    p.add_argument("document")
    p.add_argument("--verdicts", help="Claude's material/benign verdicts (JSON)")
    p.add_argument("--report", help="write the full JSON report here")
    p.add_argument("--markdown", help="write a readable report here")
    p.add_argument("--limit", type=int, default=60, help="differences to print")

    p = add("fix", cmd_fix, "restore a document's boilerplate to match the template")
    package(p)
    p.add_argument("document")
    p.add_argument("--out", required=True)
    p.add_argument("--strict", action="store_true",
                   help="also restore small wording changes (changed_fixed_minor)")

    add("doctor", cmd_doctor, "check Python and packages; print the install command")
    p = add("demo", cmd_demo, "write sample documents to try the plugin on")
    p.add_argument("--out", default="docforge-demo")
    p = add("pdf", cmd_pdf, "convert a .docx to PDF with LibreOffice")
    p.add_argument("document")
    p.add_argument("--out-dir")
    p = add("zip", cmd_zip, "zip a template package folder")
    p.add_argument("package_dir")
    p.add_argument("--out", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        result = args.func(args)
    except ToolkitError as exc:
        err = {"ok": False, "error": str(exc)}
        if exc.hint:
            err["hint"] = exc.hint
        _emit(err)
        return 1
    except ModuleNotFoundError as exc:
        _emit({
            "ok": False,
            "error": f"missing Python package: {exc.name}",
            "hint": "Run `doctor` and install what it lists.",
        })
        return 1
    _emit(result)
    return 1 if isinstance(result, dict) and result.get("ok") is False else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
