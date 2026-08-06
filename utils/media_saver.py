"""
utils/media_saver.py

Saves covers, blurbs, and reviews with the project naming rules:

    <isbn13>_cp_<source>_<n>.jpg
    <isbn13>_b_<Source>_<n>.txt     # Blurb: capitalized Source (professor rule)
    <isbn13>_r_<source>_1.txt       # All reviews for ISBN+source in one file

Blurbs go into per-source folders:
    output/Blurb/Amazon_Blurb/
    output/Blurb/Kobo_Blurb/
    ...
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import requests

import config


def _source_token(source: str) -> str:
    """Map display source name to cover/review filename token (lowercase)."""
    return config.SOURCE_FILE_TOKENS.get(source, source.lower())


def _blurb_source_token(source: str) -> str:
    """Map display source name to Blurb filename token (Amazon, Kobo, ...)."""
    return config.BLURB_SOURCE_TOKENS.get(source, source)


def save_blurb(
    isbn13: str,
    source: str,
    blurb: str,
    number: Optional[int] = None,
) -> Optional[Path]:
    """
    Save blurb text into output/Blurb/<Source>_Blurb/.

    Filename example (exact professor convention):
        9780131103627_b_Amazon_1.txt
    """
    text = (blurb or "").strip()
    if not text or text == config.MISSING_VALUE:
        return None

    folder = config.blurb_dir_for_source(source)
    folder.mkdir(parents=True, exist_ok=True)
    token = _blurb_source_token(source)

    # Avoid duplicate TXT files with identical blurb text for same ISBN+source.
    existing = sorted(folder.glob(f"{isbn13}_b_{token}_*.txt"))
    for path in existing:
        try:
            if path.read_text(encoding="utf-8").strip() == text:
                return path
        except OSError:
            continue

    if number is None:
        number = len(existing) + 1

    filename = config.BLURB_FILENAME_TEMPLATE.format(
        isbn13=isbn13,
        source=token,
        n=number,
    )
    path = folder / filename
    try:
        path.write_text(text + "\n", encoding="utf-8")
        return path
    except OSError:
        return None


def save_reviews(isbn13: str, source: str, reviews: list[str]) -> list[Path]:
    """
    Save all reviews for one ISBN + source into a single text file.

    Filename:
        <isbn13>_r_<source>_1.txt

    Format (blank line between reviews):
        review 1 text

        review 2 text

        review 3 text
    """
    token = _source_token(source)
    clean_reviews: list[str] = []
    for review in reviews:
        text = (review or "").strip()
        if text:
            clean_reviews.append(text)

    if not clean_reviews:
        return []

    filename = config.REVIEW_FILENAME_TEMPLATE.format(
        isbn13=isbn13,
        source=token,
        n=1,
    )
    path = config.REVIEWS_DIR / filename
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n\n".join(clean_reviews) + "\n", encoding="utf-8")
        return [path]
    except OSError:
        return []


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
    http.headers.setdefault(
        "Accept",
        "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    )

    # CDNs often require a matching Referer / reject hotlinks without one.
    referer_by_source = {
        "Amazon": "https://www.amazon.com/",
        "Goodreads": "https://www.goodreads.com/",
        "Kobo": "https://www.kobo.com/",
        "Audible": "https://www.audible.com/",
        "BookBub": "https://www.bookbub.com/",
    }

    for index, url in enumerate(cover_urls, start=1):
        if not url or not str(url).startswith("http"):
            continue
        # Skip tiny placeholders / tracking pixels.
        lowered = str(url).lower()
        if any(bad in lowered for bad in ("sprite", "pixel", "1x1", "blank.gif")):
            continue
        filename = config.COVER_FILENAME_TEMPLATE.format(
            isbn13=isbn13,
            source=token,
            n=index,
        )
        path = config.COVER_PAGE_DIR / filename
        headers = {
            "Referer": referer_by_source.get(source, "https://www.google.com/"),
        }
        try:
            response = http.get(
                url,
                timeout=config.HTTP_TIMEOUT_SECONDS,
                headers=headers,
                allow_redirects=True,
            )
            if response.status_code != 200 or not response.content:
                continue
            content_type = (response.headers.get("Content-Type") or "").lower()
            # Reject HTML error pages saved as "images".
            if "text/html" in content_type:
                continue
            if len(response.content) < 800:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(response.content)
            saved.append(path)
            # One good cover per source is enough for the lab.
            break
        except (OSError, requests.RequestException):
            continue
    return saved
