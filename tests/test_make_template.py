"""make-template: analyze, review decisions, build packages."""

from __future__ import annotations

import json
import zipfile

from docforge.toolkit.package import open_template


def test_analyze_proposes_fields_from_examples(cli, demo_dir, tmp_path):
    code, out = cli("analyze", demo_dir / "invoice_1.docx", demo_dir / "invoice_2.docx",
                    "--work", tmp_path / "w", "--mode", "smart")
    assert code == 0
    names = [f["field_name"] for f in out["proposed_fields"]]
    assert names == ["invoice_number", "invoice_date", "bill_to", "line_items", "total_due"]
    assert out["documents"][0]["representative"] is True
    # Cells inside the line-items table belong to the table, not to review.
    assert out["needs_review"]["count"] == 0
    assert (tmp_path / "w" / "decisions.json").is_file()


def test_outline_and_evidence_views(cli, demo_dir, tmp_path):
    work = tmp_path / "w"
    cli("analyze", demo_dir / "invoice_1.docx", demo_dir / "invoice_2.docx", "--work", work,
        "--mode", "smart")
    code, out = cli("outline", "--work", work, "--limit", "3")
    assert code == 0 and out["returned"] == 3 and out["next_offset"] == 3
    date = next(n for n in cli("outline", "--work", work)[1]["nodes"] if n["node_id"] == "n0003")
    assert date["proposal"]["static_prefix"] == "Invoice Date: "
    assert date["evidence"]["status"] == "partial_change"
    code, out = cli("node", "--work", work, "n0003")
    assert code == 0 and out["text"].startswith("Invoice Date:")
    code, out = cli("neighbors", "--work", work, "n0003", "--radius", "1")
    assert [n["node_id"] for n in out["neighbors"]] == ["n0002", "n0003", "n0004"]
    code, out = cli("node", "--work", work, "nope")
    assert code == 1 and "unknown node_id" in out["error"]


def test_decisions_are_validated(cli, demo_dir, tmp_path):
    work = tmp_path / "w"
    cli("analyze", demo_dir / "invoice_1.docx", demo_dir / "invoice_2.docx", "--work", work,
        "--mode", "smart")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"classifications": [
        {"node_id": "n0002", "field_name": "Bad Name"},
        {"node_id": "zzz"},
        {"node_id": "n0003", "classification": "DYNAMIC_SOMETHING"},
        {"node_id": "n0019", "field_name": "item"},
    ]}))
    code, out = cli("review", "--work", work, "--decisions", bad)
    assert code == 1
    for problem in ("must be snake_case", "unknown node_id", "unknown classification", "reserved"):
        assert problem in out["error"]


def test_review_warns_about_stale_section_names(cli, demo_dir, tmp_path):
    work = tmp_path / "w"
    cli("analyze", demo_dir / "invoice_1.docx", demo_dir / "invoice_2.docx", "--work", work,
        "--mode", "smart")
    (work / "decisions.json").write_text(json.dumps({
        "sections": [{"section_key": "header", "field_names": ["bill_to"]}],
        "classifications": [{"node_id": "n0004", "field_name": "customer"}],
    }))
    code, out = cli("review", "--work", work)
    assert code == 0
    assert any("bill_to" in w for w in out["warnings"])


def test_fixed_decision_removes_a_field(cli, demo_dir, tmp_path):
    work = tmp_path / "w"
    cli("analyze", demo_dir / "invoice_1.docx", demo_dir / "invoice_2.docx", "--work", work,
        "--mode", "smart")
    (work / "decisions.json").write_text(json.dumps({
        "classifications": [{"node_id": "n0004", "classification": "FIXED"}],
    }))
    code, out = cli("review", "--work", work)
    assert "bill_to" not in [f["field_name"] for f in out["fields"]]


def test_build_writes_versions_and_zip(invoice_package, cli, tmp_path):
    tv = open_template(invoice_package)
    assert tv.version == 1 and tv.template_id == "invoice"
    names = [f.field_name for f in tv.fields()]
    assert "customer" in names and "bill_to" not in names
    for name in ("template.docx", "with-labels.docx", "field_definitions.json",
                 "representative.json", "manifest.json"):
        assert (invoice_package / "1" / name).is_file()
    # Building again into the same folder adds version 2 and keeps version 1.
    work = tmp_path / "work"
    code, out = cli("build", "--work", work, "--name", "Invoice", "--out", invoice_package,
                    "--zip", tmp_path / "invoice.zip")
    assert code == 0 and out["version"] == 2 and out["versions"] == [1, 2]
    assert open_template(invoice_package, version=1).version == 1
    with zipfile.ZipFile(tmp_path / "invoice.zip") as zf:
        assert "invoice/2/template.docx" in zf.namelist()
    assert open_template(tmp_path / "invoice.zip").version == 2


def test_full_mode_keeps_page_numbers_and_drops_table_cells(cli, demo_dir, tmp_path):
    work = tmp_path / "w"
    code, out = cli("analyze", demo_dir / "project_report_1.docx",
                    demo_dir / "project_report_2.docx", "--work", work, "--mode", "full")
    assert code == 0
    names = [f["field_name"] for f in out["proposed_fields"]]
    # The task table's header cells are owned by its loop, not fields.
    assert "task_status" in names
    assert not {"task", "owner", "status", "person"} & set(names)
    # The footer's page number stays a Word field.
    assert "footer_body" not in names
    code, out = cli("build", "--work", work, "--name", "Status", "--out", tmp_path / "pkg")
    assert code == 0
    with zipfile.ZipFile(tmp_path / "pkg" / "1" / "template.docx") as zf:
        footers = [n for n in zf.namelist() if n.startswith("word/footer")]
        assert any(b"PAGE" in zf.read(n) for n in footers)


def test_single_example_is_supported(cli, demo_dir, tmp_path):
    code, out = cli("analyze", demo_dir / "project_report_1.docx", "--work", tmp_path / "w")
    assert code == 0
    assert out["proposed_fields"]
    assert out["needs_review"]["count"] > 0


def test_inputs_are_checked(cli, tmp_path):
    not_docx = tmp_path / "notes.txt"
    not_docx.write_text("hello")
    code, out = cli("analyze", not_docx, "--work", tmp_path / "w")
    assert code == 1 and "only .docx" in out["error"]
    fake = tmp_path / "fake.docx"
    fake.write_bytes(b"not a zip")
    code, out = cli("analyze", fake, "--work", tmp_path / "w")
    assert code == 1 and "not a valid .docx" in out["error"]


def _cover_block_doc(path):
    """A training concept whose cover is a bordered one-cell table, with a
    date | document number | revision strip in a table nested inside it."""
    from docx import Document

    doc = Document()
    cover = doc.add_table(rows=1, cols=1)
    cell = cover.cell(0, 0)
    cell.paragraphs[0].text = "X50003 – COPILOT ROLLOUT"
    cell.add_paragraph("AGENT TRAINING")
    cell.add_paragraph("Copilot Agents Training concept. This concept presents possible topics.")
    cell.add_paragraph("The goal of the training is to: Enable participants to build agents")
    cell.add_paragraph("ILF GROUP HOLDING")
    strip = cell.add_table(rows=1, cols=5)
    for i, text in enumerate(["16.06.2025", "Document number", "|", "Revision 0", "© ILF"]):
        strip.cell(0, i).paragraphs[0].text = text
    doc.add_heading("1 Introduction", level=1)
    doc.add_paragraph("Copilot Agents let teams automate routine work.")
    doc.save(path)
    return path


def test_default_mode_is_full_and_tags_every_cell(cli, tmp_path):
    """Like the app's default "Full template": the cover block's text, the
    strip nested inside it included, all becomes fields."""
    from docx import Document

    src = _cover_block_doc(tmp_path / "training.docx")
    code, out = cli("analyze", src, "--work", tmp_path / "w")
    assert code == 0 and out["mode"] == "full"
    examples = " ".join(str(f.get("example")) for f in out["proposed_fields"])
    for text in ("X50003", "AGENT TRAINING", "16.06.2025", "Document number", "Revision 0"):
        assert text in examples, text
    code, out = cli("build", "--work", tmp_path / "w", "--name", "Training", "--out", tmp_path / "pkg")
    assert code == 0, out
    cover = Document(out["template_docx"]).tables[0].cell(0, 0)
    assert all("{{" in p.text for p in cover.paragraphs if p.text.strip())
    assert all("{{" in c.text for c in cover.tables[0].rows[0].cells)


def test_smart_outline_lists_table_cells_so_they_can_be_fields(cli, tmp_path):
    from docx import Document

    src = _cover_block_doc(tmp_path / "training.docx")
    work = tmp_path / "w"
    code, out = cli("analyze", src, "--work", work, "--mode", "smart")
    assert code == 0 and out["mode"] == "smart"
    nodes = cli("outline", "--work", work)[1]["nodes"]
    by_text = {n["text"]: n for n in nodes}
    title, date, rev = by_text["AGENT TRAINING"], by_text["16.06.2025"], by_text["Revision 0"]
    assert title["in_table"] == nodes[0]["node_id"] and title["cell"] == "r0c0"
    assert date["in_table"] != title["in_table"] and date["cell"] == "r0c0"  # the nested strip
    (work / "decisions.json").write_text(json.dumps({"classifications": [
        {"node_id": title["node_id"], "classification": "DYNAMIC_TEXT", "field_name": "course_title",
         "description": "The course title, in capitals."},
        {"node_id": date["node_id"], "classification": "DYNAMIC_DATE", "field_name": "issue_date",
         "description": "Issue date, DD.MM.YYYY."},
        {"node_id": rev["node_id"], "classification": "DYNAMIC_TEXT", "field_name": "revision",
         "static_prefix": "Revision ", "description": "Revision number."},
    ]}))
    code, out = cli("build", "--work", work, "--name", "Training", "--out", tmp_path / "pkg")
    assert code == 0, out
    values = tmp_path / "v.json"
    values.write_text(json.dumps({"values": {
        "course_title": "SITE OWNER TRAINING", "issue_date": "02.11.2026", "revision": "2"}}))
    code, out = cli("render", tmp_path / "pkg", "--values", values, "--out", tmp_path / "x.docx")
    assert code == 0, out
    cover = Document(tmp_path / "x.docx").tables[0].cell(0, 0)
    assert "SITE OWNER TRAINING" in [p.text for p in cover.paragraphs]
    assert [c.text for c in cover.tables[0].rows[0].cells] == [
        "02.11.2026", "Document number", "|", "Revision 2", "© ILF"]


def test_build_hands_over_the_files(cli, demo_dir, tmp_path):
    work = tmp_path / "w"
    cli("analyze", demo_dir / "invoice_1.docx", demo_dir / "invoice_2.docx", "--work", work)
    outputs = tmp_path / "outputs"
    code, out = cli("build", "--work", work, "--name", "Invoice", "--out", tmp_path / "pkg",
                    "--zip", outputs / "invoice.docforge.zip", "--deliver", outputs)
    assert code == 0, out
    assert sorted(p.name for p in outputs.iterdir()) == [
        "Invoice - template.docx", "Invoice - with labels.docx", "invoice.docforge.zip"]
    assert sorted(out["hand_over"]) == sorted(str(p) for p in outputs.iterdir())
    assert "hand_over" in out["next"]
    # Without --deliver, hand_over points into the package.
    code, out = cli("build", "--work", work, "--name", "Invoice", "--out", tmp_path / "pkg")
    assert out["hand_over"] == [out["template_docx"], out["with_labels_docx"]]
