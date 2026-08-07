"""
utils/title_match.py

Shared title/author matching for cross-site discovery.

Goodreads (or Amazon) supplies a canonical title. Kobo / Audible / BookBub
search by TITLE ONLY; author is used only as a secondary confirmation so we
do not accept the wrong book.

When several listings look related but none is confident enough, callers log
AMBIGUOUS_TITLE_MATCH instead of saving a wrong book.
"""

from __future__ import annotations

import re
from typing import Literal

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

MatchDecision = Literal["accept", "ambiguous", "reject"]


def build_title_query_variants(*titles: str) -> list[str]:
    """
    Build distinct title search queries from Goodreads / Amazon titles.

    Order: cleaned full title, text before ':', first ~6 content words.
    Used when both Goodreads and Amazon confirm the same book so Kobo /
    Audible / BookBub get more chances to find a listing.
    """
    variants: list[str] = []
    for raw in titles:
        text = (raw or "").strip()
        if not text or text == config.MISSING_VALUE:
            continue
        cleaned = clean_hint_title(text) or text
        candidates = [cleaned]
        if ":" in cleaned:
            before = cleaned.split(":", 1)[0].strip()
            if before:
                candidates.append(before)
        tokens = [t for t in re.findall(r"[A-Za-z0-9]+", cleaned) if t.lower() not in _STOPWORDS]
        if len(tokens) > 4:
            candidates.append(" ".join(tokens[:6]))
        for candidate in candidates:
            candidate = re.sub(r"\s+", " ", candidate).strip()
            if candidate and candidate.lower() not in {v.lower() for v in variants}:
                variants.append(candidate)
    return variants


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


def normalize_title(title: str) -> str:
    """Lowercase, strip punctuation / series noise, collapse whitespace."""
    cleaned = clean_hint_title(title).lower()
    cleaned = re.sub(r"[^a-z0-9\s]", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


# Common title number words ↔ digits (helps "Eighty Days" vs "80 Days").
_NUMBER_WORD_TO_DIGIT: dict[str, str] = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
    "eleven": "11",
    "twelve": "12",
    "thirteen": "13",
    "fourteen": "14",
    "fifteen": "15",
    "sixteen": "16",
    "seventeen": "17",
    "eighteen": "18",
    "nineteen": "19",
    "twenty": "20",
    "thirty": "30",
    "forty": "40",
    "fifty": "50",
    "sixty": "60",
    "seventy": "70",
    "eighty": "80",
    "ninety": "90",
    "hundred": "100",
}
_DIGIT_TO_NUMBER_WORD: dict[str, str] = {
    v: k for k, v in _NUMBER_WORD_TO_DIGIT.items()
}


def significant_title_tokens(title: str) -> set[str]:
    """Content words from a title (drop short stopwords; expand number variants)."""
    tokens = re.findall(r"[a-z0-9]+", clean_hint_title(title).lower())
    base = {t for t in tokens if len(t) > 1 and t not in _STOPWORDS}
    expanded = set(base)
    for token in base:
        if token in _NUMBER_WORD_TO_DIGIT:
            expanded.add(_NUMBER_WORD_TO_DIGIT[token])
        if token in _DIGIT_TO_NUMBER_WORD:
            expanded.add(_DIGIT_TO_NUMBER_WORD[token])
    return expanded


def title_match_score(hint: str, found: str) -> float:
    """
    Score title overlap in [0.0, 1.0].

    Used for viva-friendly confidence checks before accepting a listing.
    """
    hint_tokens = significant_title_tokens(hint)
    found_tokens = significant_title_tokens(found)
    if not hint_tokens or not found_tokens:
        return 0.0
    overlap = hint_tokens & found_tokens
    # Jaccard-ish but biased toward covering the hint tokens.
    cover = len(overlap) / len(hint_tokens)
    jaccard = len(overlap) / len(hint_tokens | found_tokens)
    return round((cover * 0.7) + (jaccard * 0.3), 3)


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
    extra = found_tokens - hint_tokens
    needed = max(2, (len(hint_tokens) + 1) // 2)
    if len(overlap) < min(needed, len(hint_tokens)):
        return False
    # Reject near-miss titles like "Around the ward..." vs "Around the World..."
    # (they share many words but differ on a distinctive content word).
    if missing and len(hint_tokens) <= 5:
        return False
    # Reject short-title supersets: "Golden Torc" vs "Man With the Golden Torc".
    if len(hint_tokens) <= 3 and len(extra) >= 1:
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
    return classify_title_match(
        hint_title=hint_title,
        hint_authors=hint_authors,
        found_title=found_title,
        found_authors=found_authors,
    ) == "accept"


def classify_title_match(
    *,
    hint_title: str,
    hint_authors: str,
    found_title: str,
    found_authors: str,
) -> MatchDecision:
    """
    Decide whether a found listing is safe to accept.

    Returns
    -------
    'accept'     — confident same book
    'ambiguous'  — partial overlap; do not save (log AMBIGUOUS_TITLE_MATCH)
    'reject'     — clearly different / empty
    """
    hint_title = (hint_title or "").strip()
    found_title = (found_title or "").strip()
    if not hint_title or hint_title == config.MISSING_VALUE:
        # No canonical title yet — allow ISBN-based discovery only.
        return "accept" if found_title and found_title != config.MISSING_VALUE else "reject"
    if not found_title or found_title == config.MISSING_VALUE:
        return "reject"

    score = title_match_score(hint_title, found_title)
    author_ok = authors_roughly_match(hint_authors, found_authors)
    hint_tokens = significant_title_tokens(hint_title)
    found_tokens = significant_title_tokens(found_title)
    overlap = hint_tokens & found_tokens
    extra = found_tokens - hint_tokens

    if titles_roughly_match(hint_title, found_title):
        if author_ok:
            # Still reject obvious supersets on short titles when author matched
            # by accident (shared last-name token). Require limited extras.
            if len(hint_tokens) <= 3 and len(extra) >= 2:
                return "ambiguous"
            return "accept"
        # Author missing/mismatch: only accept long, distinctive title overlap.
        # Short titles like "The Golden Torc" must not accept
        # "The Man With the Golden Torc" (different book).
        if len(hint_tokens) <= 3 and extra:
            return "ambiguous"
        if len(overlap) >= max(3, (len(hint_tokens) * 2) // 3) and len(extra) <= 1:
            return "accept"
        if score >= 0.85 and len(extra) <= 1:
            return "accept"
        return "ambiguous"

    # Partial token overlap → ambiguous (safer than wrong book).
    if score >= 0.35:
        return "ambiguous"
    return "reject"


def note_title_match(fields: dict, note: str = "alternate store listing") -> None:
    """Append a short neutral note into the edition field (non-destructive)."""
    edition = str(fields.get("edition", config.MISSING_VALUE))
    if edition in {config.MISSING_VALUE, "", None}:
        fields["edition"] = note
    elif note not in edition:
        fields["edition"] = f"{edition} | {note}"


def ambiguous_match_detail(
    *,
    hint_title: str,
    found_title: str,
    score: float | None = None,
) -> str:
    """Build a preprocessing-log detail string for AMBIGUOUS_TITLE_MATCH."""
    if score is None:
        score = title_match_score(hint_title, found_title)
    return (
        f"AMBIGUOUS_TITLE_MATCH score={score:.3f} "
        f"hint={hint_title!r} found={found_title!r}"
    )
