"""Set the toolkit up by following SKILL.md's own words, then run the workflows.

The commands come from SKILL.md's text, not retyped here, so an edit that
breaks the setup fails this check. Both routes install the pinned version from
PyPI: plain pip into a fresh environment, and the private virtual environment
for Pythons that refuse pip (PEP 668).
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

PIN = re.compile(r"docforge-live==([0-9][0-9A-Za-z.]*)")


def _setup(plugin: Path) -> tuple[str, list[str], str]:
    text = (plugin / "skills" / "docforge" / "SKILL.md").read_text(encoding="utf-8")
    alias = re.search(r"`docforge` means:\s+```bash\n(.+?)\n```", text, re.S).group(1).strip()
    section = text[text.index("### Set it up first"):text.index("## Where files are")]
    blocks = [
        "\n".join(line.strip() for line in block.splitlines() if line.strip())
        for block in re.findall(r"```bash\n(.*?)\n\s*```", section, re.S)
    ]
    assert len(blocks) == 2, "a pip install, then the virtual-environment fallback"
    return alias, blocks, PIN.search(section).group(1)


def _sh(command: str, cwd: Path, home: Path | None = None) -> subprocess.CompletedProcess:
    env = None
    if home is not None:
        import os

        env = dict(os.environ, HOME=str(home))
    return subprocess.run(["bash", "-c", command], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=600)


def _with_python(line: str, python: str) -> str:
    return re.sub(r"^python3\b", shlex.quote(python), line)


def _run(docforge: str, args: str, cwd: Path) -> dict:
    proc = _sh(f"{docforge} {args}", cwd)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout)


def test_plain_pip_route_installs_the_pin_and_the_workflows_run(plugin, tmp_path):
    alias, (pip_block, _venv_block), pin = _setup(plugin)
    subprocess.run([sys.executable, "-m", "venv", str(tmp_path / "fresh")], check=True)
    python = str(tmp_path / "fresh" / "bin" / "python")
    for line in pip_block.splitlines():
        proc = _sh(_with_python(line, python), tmp_path)
        assert proc.returncode == 0, proc.stdout + proc.stderr
    docforge = _with_python(alias, python)

    doctor = _run(docforge, "doctor", tmp_path)
    assert doctor["ok"] is True and doctor["toolkit"] == pin

    # Make, fill, check and fix, as the workflows tell Claude to.
    _run(docforge, "demo --out demo", tmp_path)
    _run(docforge, "analyze demo/invoice_1.docx demo/invoice_2.docx --work w --mode smart", tmp_path)
    built = _run(docforge, 'build --work w --name "Invoice" --out "docforge-templates/invoice" '
                           "--zip outputs/invoice.docforge.zip --deliver outputs", tmp_path)
    assert len(built["hand_over"]) == 3
    assert all((tmp_path / p).is_file() for p in built["hand_over"]), built["hand_over"]
    (tmp_path / "values.json").write_text(json.dumps({"values": {
        "invoice_number": "INV-1", "invoice_date": "2026-11-02", "bill_to": "Initech & Partners",
        "line_items": [{"description": "Audit", "qty": "1", "unit_price": "USD 10", "amount": "USD 10"}],
        "total_due": "USD 10"}}))
    rendered = _run(docforge, "render docforge-templates/invoice --values values.json "
                              "--out outputs/INV-1.docx --blocks", tmp_path)
    assert rendered["validation"]["status"] == "pass"
    checked = _run(docforge, "check docforge-templates/invoice demo/invoice_edited.docx", tmp_path)
    assert {"changed_fixed", "changed_fixed_minor"} <= {d["kind"] for d in checked["differences"]}
    fixed = _run(docforge, "fix docforge-templates/invoice demo/invoice_edited.docx "
                           "--out outputs/fixed.docx --strict", tmp_path)
    assert fixed["score_after"] == 100.0


def test_virtual_environment_route_installs_the_pin(plugin, tmp_path):
    alias, (_pip_block, venv_block), pin = _setup(plugin)
    home = tmp_path / "home"
    home.mkdir()
    for line in venv_block.splitlines():
        proc = _sh(_with_python(line, sys.executable), tmp_path, home=home)
        assert proc.returncode == 0, proc.stdout + proc.stderr
    venv_python = home / ".docforge-venv" / "bin" / "python"
    assert venv_python.is_file()
    docforge = _with_python(alias, str(venv_python))
    doctor = _run(docforge, "doctor", tmp_path)
    assert doctor["ok"] is True and doctor["toolkit"] == pin
