"""
utils/title_match.py

Shared title/author matching for cross-site discovery.
When ISBN search fails on a site, another site's title/author can be used
to find the same book — but only if the found listing roughly matches.
"""

from __future__ import annotations

import re

import config

_STOPWORDS = {
    "a",
    "an",
    "the",
    "and",
    "or",
    "of",
    "to",
    "in",
    "on",
    "for",
    "with",
    "by",
    "i",
}


def clean_hint_title(title: str) -> str:
    """
    Normalize a title for cross-site search.

    Amazon titles often include series junk in parentheses, e.g.
    'THE GOLDEN TORC (Saga of Pliocene Exile, V. 2)' -> 'THE GOLDEN TORC'
    Also drops trailing genre tags like ': A novel'.
    """
    text = (title or "").strip()
    if not text or text == config.MISSING_VALUE:
        return ""
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\[[^\]]*\]", " ", text)
    # Drop common trailing edition/genre labels after a colon.
    text = re.sub(
        r"\s*:\s*(a\s+)?(novel|novella|memoir|biography|ebook|paperback)\s*$",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(r"\s+", " ", text).strip(" -\u2013\u2014")
    return text


def significant_title_tokens(title: str) -> set[str]:
    """Content words from a title (drop short stopwords)."""
    tokens = re.findall(r"[a-z0-9]+", clean_hint_title(title).lower())
    return {t for t in tokens if len(t) > 1 and t not in _STOPWORDS}


def titles_roughly_match(hint: str, found: str) -> bool:
    """True when found title shares enough words with the known title."""
    hint = (hint or "").strip()
    found = (found or "").strip()
    if not hint or hint == config.MISSING_VALUE:
        return True
    if not found or found == config.MISSING_VALUE:
        return False
    hint_tokens = significant_title_tokens(hint)
    found_tokens = significant_title_tokens(found)
    if not hint_tokens or not found_tokens:
        return False
    overlap = hint_tokens & found_tokens
    missing = hint_tokens - found_tokens
    needed = max(2, (len(hint_tokens) + 1) // 2)
    if len(overlap) < min(needed, len(hint_tokens)):
        return False
    # Reject near-miss titles like "Around the ward..." vs "Around the World..."
    # (they share many words but differ on a distinctive content word).
    if missing and len(hint_tokens) <= 5:
        return False
    return len(missing) <= max(1, len(hint_tokens) // 4)


def authors_roughly_match(hint: str, found: str) -> bool:
    """
    Soft author check: True if hint is empty, or any significant token
    from the primary hint author appears in the found authors string.
    """
    hint = (hint or "").strip()
    found = (found or "").strip()
    if not hint or hint == config.MISSING_VALUE:
        return True
    if not found or found == config.MISSING_VALUE:
        return False
    primary = hint.split(",")[0].strip().lower()
    tokens = [t for t in re.findall(r"[a-z0-9]+", primary) if len(t) > 2]
    if not tokens:
        return True
    found_l = found.lower()
    # Prefer last-name style token (usually the longest).
    tokens_sorted = sorted(tokens, key=len, reverse=True)
    return any(tok in found_l for tok in tokens_sorted[:2])


def listing_matches_hints(
    *,
    hint_title: str,
    hint_authors: str,
    found_title: str,
    found_authors: str,
) -> bool:
    """
    Gate for accepting a cross-site listing.

    Title match is required. Author match is preferred but not required when
    the title overlap is strong (stores often omit/alter author strings).
    """
    if not titles_roughly_match(hint_title, found_title):
        return False
    if authors_roughly_match(hint_authors, found_authors):
        return True
    # Strong title overlap can stand alone when author text is missing/odd.
    hint_tokens = significant_title_tokens(hint_title)
    found_tokens = significant_title_tokens(found_title)
    overlap = hint_tokens & found_tokens
    return len(overlap) >= max(3, (len(hint_tokens) * 2) // 3)


def note_title_match(fields: dict, note: str = "alternate store listing") -> None:
    """Append a short neutral note into the edition field (non-destructive)."""
    edition = str(fields.get("edition", config.MISSING_VALUE))
    if edition in {config.MISSING_VALUE, "", None}:
        fields["edition"] = note
    elif note not in edition:
        fields["edition"] = f"{edition} | {note}"
