"""
utils/media_saver.py

Saves covers, blurbs, and reviews with the project naming rules:

    <isbn13>_cp_<source>_<n>.jpg
    <isbn13>_b_<source>_<n>.txt
    <isbn13>_r_<source>_<n>.txt

Reviews:
- each review is still saved as its own numbered file (assignment rule)
- also writes <isbn13>_r_<source>_all.txt with one blank line between reviews
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import requests

import config


def _source_token(source: str) -> str:
    """Map display source name to filename token (e.g. Goodreads -> goodreads)."""
    return config.SOURCE_FILE_TOKENS.get(source, source.lower())


def save_blurb(isbn13: str, source: str, blurb: str, number: int = 1) -> Optional[Path]:
    """Save blurb text to output/Blurb/."""
    text = (blurb or "").strip()
    if not text or text == config.MISSING_VALUE:
        return None

    filename = config.BLURB_FILENAME_TEMPLATE.format(
        isbn13=isbn13,
        source=_source_token(source),
        n=number,
    )
    path = config.BLURB_DIR / filename
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
        return path
    except OSError:
        return None


def save_reviews(isbn13: str, source: str, reviews: list[str]) -> list[Path]:
    """
    Save reviews as individual files, plus one combined file.

    Combined file format (easy to read):
        review 1 text

        review 2 text

        review 3 text
    """
    saved: list[Path] = []
    token = _source_token(source)
    clean_reviews: list[str] = []

    for index, review in enumerate(reviews, start=1):
        text = (review or "").strip()
        if not text:
            continue
        clean_reviews.append(text)
        filename = config.REVIEW_FILENAME_TEMPLATE.format(
            isbn13=isbn13,
            source=token,
            n=index,
        )
        path = config.REVIEWS_DIR / filename
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text + "\n", encoding="utf-8")
            saved.append(path)
        except OSError:
            continue

    # Combined readable file with a blank line between each reviewer's text.
    if clean_reviews:
        combined_name = f"{isbn13}_r_{token}_all.txt"
        combined_path = config.REVIEWS_DIR / combined_name
        try:
            combined_path.parent.mkdir(parents=True, exist_ok=True)
            combined_path.write_text("\n\n".join(clean_reviews) + "\n", encoding="utf-8")
        except OSError:
            pass

    return saved


def download_covers(
    isbn13: str,
    source: str,
    cover_urls: list[str],
    session: Optional[requests.Session] = None,
) -> list[Path]:
    """Download cover images into output/Cover_Page/."""
    saved: list[Path] = []
    token = _source_token(source)
    http = session or requests.Session()
    http.headers.setdefault(
        "User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    )

    for index, url in enumerate(cover_urls, start=1):
        if not url or not str(url).startswith("http"):
            continue
        filename = config.COVER_FILENAME_TEMPLATE.format(
            isbn13=isbn13,
            source=token,
            n=index,
        )
        path = config.COVER_PAGE_DIR / filename
        try:
            response = http.get(url, timeout=config.HTTP_TIMEOUT_SECONDS)
            if response.status_code != 200 or not response.content:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(response.content)
            saved.append(path)
        except (OSError, requests.RequestException):
            continue
    return saved
