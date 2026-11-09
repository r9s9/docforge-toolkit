"""What the plugin folder may contain, for Anthropic's directory and Claude's uploaders."""

from __future__ import annotations

import json
import re

from conftest import plugin_files

# For an organization the directory hasn't verified: Markdown, text, JSON,
# YAML and PNG/JPEG/GIF/WebP/ICO images. A license, a readme, .gitignore and
# the plugin's icon may also be other UTF-8 text.
TEXT = {".md", ".txt", ".json", ".yml", ".yaml"}
IMAGES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico"}
NAMED = {"LICENSE", ".gitignore"}
JUNK = {".DS_Store", "Thumbs.db", "desktop.ini"}
MAX_FILE_BYTES = 256 * 1024
MAX_FILES = 512
MAX_ZIP_ENTRIES = 200  # claude.ai's plugin upload


def _icon(plugin) -> str:
    manifest = json.loads((plugin / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    return (plugin / manifest["icon"]).resolve().relative_to(plugin).as_posix()


def test_only_file_types_the_directory_accepts(plugin):
    icon = _icon(plugin)
    refused = []
    for path in plugin_files(plugin):
        rel = path.relative_to(plugin).as_posix()
        if path.name in NAMED or rel == icon:
            continue
        if path.suffix.lower() not in TEXT | IMAGES:
            refused.append(rel)
    assert not refused, f"not accepted from an organization that isn't Verified: {refused}"


def test_text_is_utf8_with_no_shebang_or_nul(plugin):
    for path in plugin_files(plugin):
        if path.suffix.lower() in IMAGES:
            continue
        data = path.read_bytes()
        assert b"\0" not in data, path
        assert not data.startswith(b"#!"), f"{path}: a first line naming a program to run"
        data.decode("utf-8")


def test_sizes_and_counts(plugin):
    files = plugin_files(plugin)
    assert len(files) <= MAX_FILES
    for path in files:
        if path.suffix.lower() not in IMAGES:
            assert path.stat().st_size <= MAX_FILE_BYTES, path
    folders = {"."}
    for path in files:
        folders.update(p.as_posix() for p in path.relative_to(plugin).parents)
    assert len(files) + len(folders) <= MAX_ZIP_ENTRIES


def test_no_junk_links_or_top_level_bin(plugin):
    assert not (plugin / "bin").exists(), "a top-level bin/ stops claude.ai and Cowork installing it"
    for path in plugin_files(plugin):
        assert path.name not in JUNK and "__MACOSX" not in path.parts, path
        assert not path.is_symlink(), path


def test_icon_is_a_usable_svg(plugin):
    """The directory's rule for an SVG icon: no script, event handlers,
    foreignObject, animation, style, or references outside the file."""
    svg = (plugin / _icon(plugin)).read_text(encoding="utf-8").lower()
    banned = ("<script", " on", "foreignobject", "<animate", "<set", "<style", "style=", "href", "url(")
    assert "<svg" in svg and not [b for b in banned if b in svg]


def test_manifest_has_directory_fields(plugin):
    manifest = json.loads((plugin / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "docforge-live"
    assert manifest["displayName"] == "DocForge.live"
    for key in ("version", "description", "author", "license", "homepage"):
        assert manifest.get(key), key
    for key in ("documentationUrl", "supportUrl", "privacyPolicyUrl", "termsOfServiceUrl"):
        assert manifest[key].startswith("https://"), key
    market = json.loads((plugin / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    assert market["plugins"][0]["name"] == manifest["name"]


# Text the directory's security scan reads as shell commands. Each one was a
# finding on an earlier version of the plugin.
SCAN_PATTERNS = {
    # "Uses a credential from the user's machine": an environment dump.
    "environment dump": re.compile(r"\b(?:printenv|env|set|export\s+-p)\s*[|>)]"),
    "command substitution": re.compile(r"\$\("),
    # "Contains a download-and-run command": eval of fetched code.
    "eval of fetched code": re.compile(r"\b(?:eval|exec)\s*[`\"'($]"),
}


def test_no_text_the_scan_reads_as_a_shell_command(plugin):
    found = []
    for path in plugin_files(plugin):
        if path.suffix.lower() in IMAGES:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for what, pattern in SCAN_PATTERNS.items():
                if pattern.search(line):
                    found.append(f"{path.relative_to(plugin)}:{number}: {what}: {line.strip()[:80]}")
    assert not found, "\n".join(found)


def test_eval_cases_write_amounts_without_a_dollar_sign(plugin):
    """The scan reads "$" before a digit ("$1,800") as a shell variable."""
    for path in sorted((plugin / "evals").rglob("*.md")):
        assert not re.search(r"\$[0-9]", path.read_text(encoding="utf-8")), path
