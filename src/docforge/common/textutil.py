"""Pure text-signal helpers: detect dates/numbers/people, derive field names,
and split a set of strings into a shared static skeleton + variable tokens.

These are deterministic and unit-tested. The heuristic classifier and the diff
engine lean on them so behaviour is identical with or without an LLM.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

# --- regexes ----------------------------------------------------------------
_MONTHS = (
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
)
DATE_NUMERIC_RE = re.compile(r"\b\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}\b")
DATE_TEXT_RE = re.compile(
    rf"\b(?:{_MONTHS}\s+\d{{1,2}},?\s+\d{{2,4}}|\d{{1,2}}\s+{_MONTHS}\s+\d{{2,4}})\b",
    re.IGNORECASE,
)
NUMBER_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")
CURRENCY_RE = re.compile(
    r"(?:[$€£¥₹]\s?\d[\d,]*(?:\.\d+)?)|(?:\b\d[\d,]*(?:\.\d+)?\s?(?:USD|EUR|GBP|AED|SAR|INR)\b)",
    re.IGNORECASE,
)
PERCENT_RE = re.compile(r"\b\d+(?:\.\d+)?\s?%")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PERSON_RE = re.compile(r"^[A-Z][a-z]+(?:\s+(?:[A-Z]\.|[A-Z][a-z]+)){1,2}$")
_WORD_SPLIT_RE = re.compile(r"[^a-z0-9]+")


# --- single-value detectors -------------------------------------------------
def detect_date(text: str) -> bool:
    t = text.strip()
    return bool(DATE_NUMERIC_RE.search(t) or DATE_TEXT_RE.search(t))


def detect_currency(text: str) -> bool:
    return bool(CURRENCY_RE.search(text))


def detect_percent(text: str) -> bool:
    return bool(PERCENT_RE.search(text))


def detect_email(text: str) -> bool:
    return bool(EMAIL_RE.search(text))


def is_pure_number(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    m = NUMBER_RE.fullmatch(t)
    return m is not None


def looks_like_person(text: str) -> bool:
    t = text.strip()
    if not t or t.isupper():
        return False
    return bool(PERSON_RE.match(t))


def value_kind(text: str) -> str:
    """Classify a *whole* value into a coarse kind used for field typing.

    Returns one of: date | number | person | text.
    Currency/percent collapse to 'number' (numeric field with a format hint).
    """
    t = (text or "").strip()
    if not t:
        return "text"
    if detect_date(t):
        return "date"
    if detect_currency(t) or detect_percent(t) or is_pure_number(t):
        return "number"
    if looks_like_person(t):
        return "person"
    return "text"


def semantic_hints(text: str) -> list[str]:
    """Lightweight tags attached to an element to aid classification."""
    hints: list[str] = []
    t = (text or "").strip()
    if not t:
        return hints
    if t.endswith(":"):
        hints.append("label")
    if t.isupper() and len(t) > 2:
        hints.append("all_caps")
    if detect_date(t):
        hints.append("date")
    if detect_currency(t):
        hints.append("currency")
    if detect_percent(t):
        hints.append("percent")
    if detect_email(t):
        hints.append("email")
    if is_pure_number(t):
        hints.append("number")
    if looks_like_person(t):
        hints.append("person")
    return hints


# --- field-name derivation --------------------------------------------------
# Keep prepositions like "by"/"to" — they carry meaning in labels
# ("Prepared By" -> prepared_by, "Bill To" -> bill_to).
_STOPWORDS = {"the", "a", "an", "and", "or"}


def slugify_field(text: str, fallback: str = "field", max_words: int = 5) -> str:
    """Turn a label/value into a snake_case identifier suitable for Jinja.

    "Project Name:" -> "project_name"; "Total (USD)" -> "total_usd".
    """
    t = (text or "").strip().rstrip(":").lower()
    # Drop an obvious value after a label colon: "date: 2026-06-01" -> "date".
    if ":" in t:
        t = t.split(":", 1)[0]
    words = [w for w in _WORD_SPLIT_RE.split(t) if w]
    words = [w for w in words if w not in _STOPWORDS] or words
    words = words[:max_words]
    slug = "_".join(words).strip("_")
    if not slug or not re.match(r"[a-z_]", slug):
        slug = fallback if not slug else f"f_{slug}"
    return slug


# Words that make a name longer without telling the fields apart.
_NAME_STOPWORDS = _STOPWORDS | {
    "of", "for", "in", "on", "at", "with", "from", "into", "per", "via",
    "this", "that", "these", "those", "its", "our", "your", "their",
    "is", "are", "was", "were", "be", "been", "has", "have", "had", "will", "shall",
}
SHORT_NAME_MAX_WORDS = 3
SHORT_NAME_MAX_CHARS = 24


def short_field_name(
    text: str,
    fallback: str = "field",
    *,
    max_words: int = SHORT_NAME_MAX_WORDS,
    max_chars: int = SHORT_NAME_MAX_CHARS,
) -> str:
    """A short snake_case name for a new field: what its tag will read.

    Every tag in a template is a name a person has to read, and a long one
    wraps across lines (in a table cell, across several). So at most three
    meaningful words and 24 characters, and no section numbers: "4.2 SLA
    Breach Analysis" -> "sla_breach_analysis", not "f_4_2_sla_breach_analysis".

    Idempotent, so a name can be normalized with it again and stay the same.
    """
    t = (text or "").strip().rstrip(":").lower()
    if ":" in t:
        t = t.split(":", 1)[0]
    words = [w for w in _WORD_SPLIT_RE.split(t) if w]
    # Leading numbering ("1.2", "4)") names the position, not the content.
    lead = 0
    while lead < len(words) - 1 and words[lead].isdigit():
        lead += 1
    words = words[lead:]
    words = [w for w in words if w not in _NAME_STOPWORDS] or words
    words = words[:max_words]
    while len(words) > 1 and len("_".join(words)) > max_chars:
        words.pop()
    slug = "_".join(words)[:max_chars].strip("_")
    if not slug:
        return fallback
    if not re.match(r"[a-z_]", slug):
        slug = f"f_{slug}"
    return slug


def sample_label(label: str | None, field_name: str) -> str:
    """How a variable reads in the "with labels" view of a template: «Label»."""
    return f"«{label or field_name}»"


# --- multi-sample skeleton extraction --------------------------------------
def common_skeleton(samples: list[str]) -> tuple[str | None, str | None, list[str]]:
    """Given several text variants of the same node, return the shared static
    prefix, shared static suffix, and the list of differing middle tokens.

    Example: ["Date: 2026-06-01", "Date: 2025-12-31"]
      -> prefix="Date: ", suffix="", middles=["2026-06-01", "2025-12-31"]

    Returns (None, None, samples) if there is no meaningful shared skeleton.
    """
    cleaned = [s for s in samples if s is not None]
    if len(cleaned) < 2:
        return None, None, cleaned

    # Longest common prefix / suffix across all samples.
    prefix = cleaned[0]
    for s in cleaned[1:]:
        prefix = _common_prefix(prefix, s)
    suffix = cleaned[0]
    for s in cleaned[1:]:
        suffix = _common_suffix(suffix, s)

    # Avoid overlap when strings are short.
    min_len = min(len(s) for s in cleaned)
    if len(prefix) + len(suffix) > min_len:
        suffix = suffix[: max(0, min_len - len(prefix))]

    # Retreat to token boundaries so we never cut through an alphanumeric token.
    # e.g. "Date: 2026-06-01"/"Date: 2025-12-31" share the chars "Date: 202",
    # but the variable token is the whole date — back the prefix up to "Date: ".
    prefix = _retreat_prefix(prefix, cleaned)
    suffix = _retreat_suffix(suffix, cleaned)

    middles = [s[len(prefix): len(s) - len(suffix)] for s in cleaned]

    # Only meaningful if there *is* a shared, non-trivial skeleton and the
    # middles actually differ.
    has_skeleton = (len(prefix) + len(suffix)) >= 2 and len(set(middles)) > 1
    if not has_skeleton:
        return None, None, cleaned
    return prefix, suffix, middles


def _retreat_prefix(prefix: str, samples: list[str]) -> str:
    """Trim trailing chars off ``prefix`` while the cut falls inside a token
    (prefix ends alnum AND some sample continues with an alnum char)."""
    while (
        prefix
        and prefix[-1].isalnum()
        and any(len(s) > len(prefix) and s[len(prefix)].isalnum() for s in samples)
    ):
        prefix = prefix[:-1]
    return prefix


def _retreat_suffix(suffix: str, samples: list[str]) -> str:
    """Trim leading chars off ``suffix`` while the cut falls inside a token."""
    while (
        suffix
        and suffix[0].isalnum()
        and any(
            len(s) > len(suffix) and s[len(s) - len(suffix) - 1].isalnum()
            for s in samples
        )
    ):
        suffix = suffix[1:]
    return suffix


def _common_prefix(a: str, b: str) -> str:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return a[:i]


def _common_suffix(a: str, b: str) -> str:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[-1 - i] == b[-1 - i]:
        i += 1
    return a[len(a) - i:] if i else ""


def similarity(a: str, b: str) -> float:
    """Ratio in [0,1] of textual similarity (used for node alignment)."""
    if not a and not b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()
