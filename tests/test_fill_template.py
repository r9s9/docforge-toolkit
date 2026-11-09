"""fill-template: describe fields, map documents, render values and batches."""

from __future__ import annotations

import json
from io import BytesIO

from docx import Document

VALUES = {
    "invoice_number": "INV-2026-101",
    "invoice_date": "2026-11-02",
    "customer": "Initech & Partners",
    "line_items": [
        {"description": "Discovery workshop", "qty": "2", "unit_price": "$900.00", "amount": "$1,800.00"},
        {"description": "Prototype build", "qty": "1", "unit_price": "$4,500.00", "amount": "$4,500.00"},
    ],
    "total_due": "$6,300.00",
}


def _text(path) -> str:
    doc = Document(BytesIO(path.read_bytes()))
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        parts += [c.text for row in table.rows for c in row.cells]
    return "\n".join(parts)


def test_fields_describe_shapes_and_places(cli, invoice_package):
    code, out = cli("fields", invoice_package)
    assert code == 0
    by_name = {f["field_name"]: f for f in out["fields"]}
    assert by_name["customer"]["label_before"] == "Bill To: "
    assert by_name["line_items"]["value_shape"].startswith("list of rows")
    assert [c["label"] for c in by_name["line_items"]["columns"]][1] == "Quantity"
    assert out["document_type"] == "Commercial invoice"


def test_render_fills_the_layout(cli, invoice_package, tmp_path):
    values = tmp_path / "values.json"
    values.write_text(json.dumps({"values": {**VALUES, "typo": "x"}, "drafted": ["total_due"]}))
    out_docx = tmp_path / "out.docx"
    code, out = cli("render", invoice_package, "--values", values, "--out", out_docx, "--blocks")
    assert code == 0
    assert out["validation"]["status"] == "pass"
    assert out["unknown_keys"] == ["typo"] and out["drafted"] == ["total_due"]
    text = _text(out_docx)
    assert "Bill To: Initech & Partners" in text  # label kept, & escaped correctly
    assert "Prototype build" in text  # one table row per item
    assert "Payment is due within 30 days" in text  # boilerplate untouched
    assert any(b.get("type") == "table" and len(b["rows"]) == 2 for b in out["blocks"])


def test_render_accepts_flat_and_writer_formats(cli, invoice_package, tmp_path):
    flat = tmp_path / "flat.json"
    flat.write_text(json.dumps(VALUES))
    code, out = cli("render", invoice_package, "--values", flat, "--out", tmp_path / "a.docx")
    assert code == 0 and out["missing_required"] == []
    writer = tmp_path / "writer.json"
    writer.write_text(json.dumps({"placements": [
        {"field_name": k, "value": v, "ai_drafted": k == "customer"} for k, v in VALUES.items()
    ]}))
    code, out = cli("render", invoice_package, "--values", writer, "--out", tmp_path / "b.docx")
    assert code == 0 and out["drafted"] == ["customer"]


def test_missing_required_values_are_reported(cli, invoice_package, tmp_path):
    values = tmp_path / "v.json"
    values.write_text(json.dumps({"values": {"invoice_number": "X-1"}}))
    code, out = cli("render", invoice_package, "--values", values, "--out", tmp_path / "x.docx")
    assert code == 0
    assert "customer" in out["missing_required"]
    assert out["validation"]["status"] == "fail"


def test_rich_text_becomes_paragraphs_bullets_and_bold(cli, demo_dir, tmp_path):
    work = tmp_path / "w"
    cli("analyze", demo_dir / "project_report_1.docx", demo_dir / "project_report_2.docx",
        "--work", work)
    package = tmp_path / "pkg"
    code, _ = cli("build", "--work", work, "--name", "Status", "--out", package)
    assert code == 0
    fields = cli("fields", package)[1]["fields"]
    prose = next(f["field_name"] for f in fields if f["type"] in ("text", "multiline_text")
                 and f.get("under_heading") == "Summary")
    values = tmp_path / "v.json"
    values.write_text(json.dumps({"values": {
        prose: "Highlights:\n\n- Pilot finished **early**\n- 140 users trained",
    }}))
    out_docx = tmp_path / "s.docx"
    code, out = cli("render", package, "--values", values, "--out", out_docx)
    assert code == 0
    doc = Document(BytesIO(out_docx.read_bytes()))
    bullets = [p for p in doc.paragraphs if "users trained" in p.text or "Pilot finished" in p.text]
    assert len(bullets) == 2
    assert any(r.bold and r.text == "early" for p in bullets for r in p.runs)
    assert not any("**" in p.text or p.text.startswith("- ") for p in doc.paragraphs)


def test_route_doc_reads_a_same_kind_document(cli, invoice_package, demo_dir, tmp_path):
    out_values = tmp_path / "routed.json"
    code, out = cli("route-doc", invoice_package, demo_dir / "invoice_2.docx", "--out", out_values)
    assert code == 0 and out["method"] == "structural" and out["coverage"] == 1.0
    assert out["values"]["customer"] == "Globex LLC"
    assert len(out["values"]["line_items"]) == 3
    assert json.loads(out_values.read_text())["values"]["invoice_number"] == "INV-2026-0067"


def test_route_doc_rejects_a_different_kind_of_document(cli, invoice_package, demo_dir):
    code, out = cli("route-doc", invoice_package, demo_dir / "project_report_1.docx")
    assert code == 0
    assert out["method"] == "outline"
    assert {r["field_name"] for r in out["rejected"]} >= {"invoice_number", "customer"}
    assert "Apollo Data Migration" in out["outline"]


def test_batch_renders_one_document_per_row(cli, invoice_package, tmp_path):
    rows = tmp_path / "rows.csv"
    rows.write_text(
        "invoice_number,invoice_date,customer,total_due,line_items\n"
        'INV-1,2026-11-01,Acme & Co,$100.00,"[{""description"": ""Audit"", ""qty"": ""1"", '
        '""unit_price"": ""$100.00"", ""amount"": ""$100.00""}]"\n'
        "INV-2,2026-11-02,Globex,$50.00,[]\n"
    )
    code, out = cli("render", invoice_package, "--batch", rows, "--out-dir", tmp_path / "batch",
                    "--name-pattern", "{invoice_number} {customer}")
    assert code == 0 and out["rendered"] == 2
    first, second = out["documents"]
    assert first["output"].endswith("inv-1-acme-co.docx") and "validation" not in first
    assert second["validation"] == "fail" and "'Line Items' is required" in second["issues"]
    assert "Audit" in _text(tmp_path / "batch" / "inv-1-acme-co.docx")


def test_defaults_fill_shared_values(cli, invoice_package, tmp_path):
    defaults = tmp_path / "shared.json"
    defaults.write_text(json.dumps({"customer": "Shared Client Ltd"}))
    values = tmp_path / "v.json"
    values.write_text(json.dumps({"values": {k: v for k, v in VALUES.items() if k != "customer"}}))
    out_docx = tmp_path / "d.docx"
    code, out = cli("render", invoice_package, "--values", values, "--out", out_docx,
                    "--defaults", defaults)
    assert code == 0 and "Bill To: Shared Client Ltd" in _text(out_docx)


def test_sample_shows_labels(cli, invoice_package, tmp_path):
    code, _ = cli("sample", invoice_package, "--out", tmp_path / "labels.docx")
    assert code == 0
    assert "«Bill To»" in _text(tmp_path / "labels.docx")
