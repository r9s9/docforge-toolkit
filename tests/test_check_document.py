"""check-document: compliance report, verdicts, markdown, fix."""

from __future__ import annotations

import json
import zipfile


def test_check_reports_obvious_and_subtle_changes(cli, invoice_package, demo_dir):
    code, out = cli("check", invoice_package, demo_dir / "invoice_edited.docx")
    assert code == 0
    kinds = {d["kind"]: d for d in out["differences"]}
    # The retitled document: an ordinary changed_fixed difference.
    assert kinds["changed_fixed"]["found"] == "PRO FORMA INVOICE"
    # "30 days" -> "90 days" is 99% similar, so upstream's score counts it as a
    # match; the plugin still lists it for Claude to judge.
    minor = kinds["changed_fixed_minor"]
    assert "90 days" in minor["found"] and minor["severity"] == "info"
    assert out["fixable"] is True
    assert out["score"] == 95.0  # the deterministic score is upstream's


def test_unchanged_document_passes(cli, invoice_package, demo_dir):
    code, out = cli("check", invoice_package, demo_dir / "invoice_1.docx")
    assert code == 0 and out["score"] == 100.0 and out["differences_total"] == 0


def test_verdicts_add_the_semantic_dimension(cli, invoice_package, demo_dir, tmp_path):
    verdicts = tmp_path / "verdicts.json"
    verdicts.write_text(json.dumps({"verdicts": [
        {"index": 0, "material": False, "severity": "info", "rationale": "Title wording only."},
        {"index": 1, "material": True, "severity": "error", "rationale": "Payment window tripled."},
    ]}))
    report_md = tmp_path / "report.md"
    report_json = tmp_path / "report.json"
    code, out = cli("check", invoice_package, demo_dir / "invoice_edited.docx", "--verdicts",
                    verdicts, "--markdown", report_md, "--report", report_json)
    assert code == 0 and out["verdicts_applied"] == 2
    semantic = next(d for d in out["dimensions"] if d["name"] == "semantic")
    assert semantic["score"] == 50.0
    assert out["differences"][1]["severity"] == "error"
    text = report_md.read_text()
    assert "Payment window tripled." in text and "| changed_minor |" in text
    assert json.loads(report_json.read_text())["score"] == 95.0


def test_fix_restores_boilerplate_and_strict_restores_small_edits(cli, invoice_package, demo_dir, tmp_path):
    edited = demo_dir / "invoice_edited.docx"
    code, out = cli("fix", invoice_package, edited, "--out", tmp_path / "fixed.docx")
    assert code == 0 and out["fixes_applied"] == 1 and out["remaining_differences"] == 1
    code, out = cli("fix", invoice_package, edited, "--out", tmp_path / "strict.docx", "--strict")
    assert code == 0 and out["fixes_applied"] == 2 and out["remaining_differences"] == 0
    code, out = cli("check", invoice_package, tmp_path / "strict.docx")
    assert out["score"] == 100.0 and out["differences_total"] == 0


def test_unsafe_package_zip_is_rejected(cli, tmp_path):
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../escape.txt", "x")
    code, out = cli("fields", evil)
    assert code == 1 and "unsafe path" in out["error"]
    assert not (tmp_path.parent / "escape.txt").exists()


def test_missing_package_is_a_clear_error(cli, tmp_path):
    code, out = cli("check", tmp_path / "nope", tmp_path / "doc.docx")
    assert code == 1 and "not found" in out["error"]


def test_check_says_when_a_template_has_no_fixed_wording(cli, demo_dir, tmp_path, invoice_package):
    """A Full template turns every text into a field, so a check can't compare wording."""
    code, out = cli("check", invoice_package, demo_dir / "invoice_edited.docx")
    assert out["fixed_text_checked"] > 0 and "template_note" not in out
    work = tmp_path / "full"
    cli("analyze", demo_dir / "invoice_1.docx", demo_dir / "invoice_2.docx", "--work", work)
    cli("build", "--work", work, "--name", "Invoice", "--out", tmp_path / "full-pkg")
    code, out = cli("check", tmp_path / "full-pkg", demo_dir / "invoice_edited.docx")
    assert code == 0 and out["fixed_text_checked"] == 0
    assert "Smart template" in out["template_note"]
