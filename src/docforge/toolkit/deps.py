"""Check the packages the engine needs and say exactly how to install what's missing.

Installing the docforge-live package installs everything it needs, so this
normally reports that all is well. It still matters when the package runs from
a source checkout, or when something removed a dependency. ``doctor``
installs nothing itself: it reports, and prints the command for only the
missing pieces.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import os
import shlex
import shutil
import sys
import sysconfig
from pathlib import Path

MIN_PYTHON = (3, 10)

# import name -> (distribution name, minimum version, pinned known-good version)
REQUIRED: dict[str, tuple[str, str, str]] = {
    "docx": ("python-docx", "1.1", "1.2.0"),
    "docxtpl": ("docxtpl", "0.17", "0.20.2"),
    "lxml": ("lxml", "5.1", "6.1.3"),
    "jinja2": ("Jinja2", "3.1", "3.1.6"),
    "pydantic": ("pydantic", "2.6", "2.13.5"),
    "dateutil": ("python-dateutil", "2.8", "2.9.0.post0"),
}


def _version_tuple(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in text.split("."):
        digits = ""
        for ch in piece:
            if not ch.isdigit():
                break
            digits += ch
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def _installed_version(import_name: str, dist: str) -> str | None:
    try:
        importlib.import_module(import_name)
    except Exception:  # noqa: BLE001 - any import failure means "not usable"
        return None
    try:
        return importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def soffice_binary() -> str | None:
    """LibreOffice, for PDF export. Optional."""
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _externally_managed() -> bool:
    """PEP 668: this interpreter refuses plain pip installs (system Python)."""
    if sys.prefix != sys.base_prefix:  # inside a virtual environment
        return False
    stdlib = sysconfig.get_paths().get("stdlib")
    return bool(stdlib) and (Path(stdlib) / "EXTERNALLY-MANAGED").is_file()


def missing_requirements() -> list[str]:
    """Pinned specs for required packages that are missing or too old."""
    needed: list[str] = []
    for import_name, (dist, minimum, pinned) in REQUIRED.items():
        version = _installed_version(import_name, dist)
        if version is None or (
            version != "unknown" and _version_tuple(version) < _version_tuple(minimum)
        ):
            needed.append(f"{dist}=={pinned}")
    return needed


def doctor() -> dict:
    py_ok = sys.version_info[:2] >= MIN_PYTHON
    packages = {
        dist: _installed_version(import_name, dist)
        for import_name, (dist, _minimum, _pinned) in REQUIRED.items()
    }
    needed = missing_requirements()
    from . import __version__

    report: dict = {
        "ok": py_ok and not needed,
        "toolkit": __version__,
        "python": sys.version.split()[0],
        "python_executable": sys.executable,
        "python_ok": py_ok,
        "packages": packages,
        "pdf_export": soffice_binary() is not None,
    }
    if not py_ok:
        report["problem"] = (
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is needed; "
            f"this is {report['python']}. Run the CLI with a newer python3."
        )
    if needed:
        report["missing"] = needed
        packages_spec = " ".join(needed)
        if _externally_managed():
            # The system refuses pip (PEP 668). A virtual environment that still
            # sees the system's packages needs only the missing ones, and
            # leaves the system's own packages untouched.
            venv = Path.home() / ".docforge-venv"
            vpy = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            # Quoted: on a Mac the home folder or Python path can contain spaces.
            report["install"] = (
                f"{shlex.quote(sys.executable)} -m venv --system-site-packages "
                f"{shlex.quote(str(venv))} && "
                f"{shlex.quote(str(vpy))} -m pip install --quiet {packages_spec}"
            )
            report["then_run_with"] = str(vpy)
            report["install_note"] = (
                "This Python is managed by the system, so the packages go into a "
                "virtual environment that still sees the system's packages. After "
                "installing, run the CLI with then_run_with instead of python3."
            )
        else:
            report["install"] = (
                f"{shlex.quote(sys.executable)} -m pip install --quiet {packages_spec}"
            )
    return report
