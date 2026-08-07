"""
utils/media_saver.py

Saves covers, blurbs, and reviews with the project naming rules:

    <isbn13>_c_<Source>_<n>.jpg     # Cover: capitalized Source
    <isbn13>_b_<Source>_<n>.txt     # Blurb: capitalized Source
    <isbn13>_r_<Source>_<n>.txt     # One review text file per review

Folders:
    output/Cover_Page/<Source>_Cover/
    output/Blurb/<Source>_Blurb/
    output/Reviews/<Source>_Reviews/
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import requests

import config


def _asset_source_token(source: str) -> str:
    """Map display source name to Cover/Review/Blurb filename token."""
    return config.ASSET_SOURCE_TOKENS.get(source, source)


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
    Save each review as its own text file under output/Reviews/<Source>_Reviews/.

    Filename examples:
        9780143127550_r_Goodreads_1.txt
        9780143127550_r_Goodreads_2.txt

    Identical review text for the same ISBN+source is not written twice.
    """
    token = _asset_source_token(source)
    folder = config.reviews_dir_for_source(source)
    folder.mkdir(parents=True, exist_ok=True)

    clean_reviews: list[str] = []
    seen: set[str] = set()
    for review in reviews:
        text = (review or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        clean_reviews.append(text)

    if not clean_reviews:
        return []

    # Skip texts already saved for this ISBN+source.
    existing = sorted(folder.glob(f"{isbn13}_r_{token}_*.txt"))
    already: set[str] = set()
    for path in existing:
        try:
            already.add(path.read_text(encoding="utf-8").strip())
        except OSError:
            continue

    saved: list[Path] = []
    next_n = len(existing) + 1
    for text in clean_reviews:
        if text in already:
            # Return existing path if we can find it.
            for path in existing:
                try:
                    if path.read_text(encoding="utf-8").strip() == text:
                        saved.append(path)
                        break
                except OSError:
                    continue
            continue

        filename = config.REVIEW_FILENAME_TEMPLATE.format(
            isbn13=isbn13,
            source=token,
            n=next_n,
        )
        path = folder / filename
        try:
            path.write_text(text + "\n", encoding="utf-8")
            saved.append(path)
            already.add(text)
            next_n += 1
        except OSError:
            continue
    return saved


def download_covers(
    isbn13: str,
    source: str,
    cover_urls: list[str],
    session: Optional[requests.Session] = None,
) -> list[Path]:
    """Download cover images into output/Cover_Page/<Source>_Cover/."""
    saved: list[Path] = []
    token = _asset_source_token(source)
    folder = config.cover_dir_for_source(source)
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

    # Refresh-friendly: always write the primary cover as _1 (overwrite).
    for url in cover_urls:
        if not url or not str(url).startswith("http"):
            continue
        # Skip tiny placeholders / tracking pixels.
        lowered = str(url).lower()
        if any(bad in lowered for bad in ("sprite", "pixel", "1x1", "blank.gif")):
            continue
        filename = config.COVER_FILENAME_TEMPLATE.format(
            isbn13=isbn13,
            source=token,
            n=1,
        )
        path = folder / filename
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
