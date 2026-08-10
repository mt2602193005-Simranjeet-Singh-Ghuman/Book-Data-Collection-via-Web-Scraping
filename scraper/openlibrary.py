"""
scraper/openlibrary.py

Fast Open Library metadata via their public Books API (no Playwright).
Used as a 6th source with the same output/master schema as the other sites.
"""

from __future__ import annotations

import random
import time
from typing import Any, Optional
from urllib.parse import quote

import requests

import config
from scraper.base import BaseScraper, ScrapedBook


class OpenLibraryScraper(BaseScraper):
    """Open Library scraper — ISBN API first (optimized for large batches)."""

    source_name = "OpenLibrary"
    API_URL = "https://openlibrary.org/api/books"

    def polite_delay(self) -> None:
        """Shorter delay for OL API (much lighter than HTML storefronts)."""
        low, high = config.OPENLIBRARY_REQUEST_DELAY_SECONDS
        time.sleep(random.uniform(low, high))

    def build_candidate_urls(self, isbn13: str) -> list[str]:
        """Open Library edition page URL (fallback reference only)."""
        return [f"https://openlibrary.org/isbn/{quote(isbn13)}"]

    def parse_book_page(self, soup, page_url: str, isbn13: str) -> ScrapedBook:
        """Unused for API path; kept for BaseScraper abstract contract."""
        result = ScrapedBook(source=self.source_name, isbn13=isbn13)
        result.fields = self._empty_fields(isbn13)
        result.fields["url"] = page_url
        return result

    def scrape(
        self,
        isbn13: str,
        *,
        hint_title: str = "",
        hint_authors: str = "",
    ) -> ScrapedBook:
        """Fetch one ISBN from the Open Library Books API."""
        _ = hint_title, hint_authors
        batch = self.scrape_many([isbn13])
        return batch.get(isbn13) or self._failed(isbn13, "No API response")

    def scrape_many(self, isbn13_list: list[str]) -> dict[str, ScrapedBook]:
        """
        Fetch many ISBNs in one API call (fast path for --openlibrary-only).

        Open Library supports comma-separated bibkeys.
        """
        cleaned = [str(x).strip() for x in isbn13_list if str(x).strip()]
        out: dict[str, ScrapedBook] = {}
        if not cleaned:
            return out

        bibkeys = ",".join(f"ISBN:{isbn}" for isbn in cleaned)
        params = {
            "bibkeys": bibkeys,
            "format": "json",
            "jscmd": "data",
        }
        self.polite_delay()
        try:
            response = self.session.get(
                self.API_URL,
                params=params,
                timeout=config.HTTP_TIMEOUT_SECONDS,
            )
            if response.status_code != 200:
                for isbn in cleaned:
                    out[isbn] = self._failed(
                        isbn, f"HTTP {response.status_code} from Open Library API"
                    )
                return out
            payload = response.json()
            if not isinstance(payload, dict):
                payload = {}
        except Exception as exc:  # noqa: BLE001
            for isbn in cleaned:
                out[isbn] = self._failed(isbn, f"API error: {exc}")
            return out

        for isbn in cleaned:
            key = f"ISBN:{isbn}"
            data = payload.get(key)
            if not isinstance(data, dict) or not data:
                out[isbn] = self._failed(isbn, "ISBN not found on Open Library")
                continue
            out[isbn] = self._parse_api_book(isbn, data)
        return out

    def _failed(self, isbn13: str, error: str) -> ScrapedBook:
        result = ScrapedBook(source=self.source_name, isbn13=isbn13)
        result.fields = self._empty_fields(isbn13)
        result.error = f"OpenLibrary: {error}"
        return result

    def _parse_api_book(self, isbn13: str, data: dict[str, Any]) -> ScrapedBook:
        result = ScrapedBook(source=self.source_name, isbn13=isbn13)
        result.fields = self._empty_fields(isbn13)
        result.method_used = "openlibrary-api"

        title = str(data.get("title") or "").strip()
        if not title:
            return self._failed(isbn13, "API record has no title")

        result.fields["title"] = title
        result.fields["subtitle"] = str(data.get("subtitle") or "").strip() or config.MISSING_VALUE

        authors = []
        for author in data.get("authors") or []:
            if isinstance(author, dict):
                name = str(author.get("name") or "").strip()
                if name:
                    authors.append(name)
        result.fields["authors"] = ", ".join(authors) if authors else config.MISSING_VALUE

        publishers = []
        for pub in data.get("publishers") or []:
            if isinstance(pub, dict):
                name = str(pub.get("name") or "").strip()
                if name:
                    publishers.append(name)
            elif isinstance(pub, str) and pub.strip():
                publishers.append(pub.strip())
        result.fields["publisher"] = (
            ", ".join(publishers) if publishers else config.MISSING_VALUE
        )

        result.fields["publication_date"] = (
            str(data.get("publish_date") or "").strip() or config.MISSING_VALUE
        )

        pages = data.get("number_of_pages")
        if pages is None:
            result.fields["pages"] = config.MISSING_VALUE
        else:
            result.fields["pages"] = str(pages)

        # Subjects → genres
        genres: list[str] = []
        for subject in data.get("subjects") or []:
            if isinstance(subject, dict):
                name = str(subject.get("name") or "").strip()
            else:
                name = str(subject or "").strip()
            if name:
                genres.append(name)
        result.fields["genres"] = ", ".join(genres[:12]) if genres else config.MISSING_VALUE

        url = str(data.get("url") or "").strip()
        if not url:
            url = f"https://openlibrary.org/isbn/{isbn13}"
        result.fields["url"] = url

        # Description / blurb
        blurb = ""
        desc = data.get("description")
        if isinstance(desc, dict):
            blurb = str(desc.get("value") or "").strip()
        elif isinstance(desc, str):
            blurb = desc.strip()
        if not blurb:
            notes = data.get("notes")
            if isinstance(notes, dict):
                blurb = str(notes.get("value") or "").strip()
            elif isinstance(notes, str):
                blurb = notes.strip()
        if blurb:
            result.blurb = blurb
            result.fields["description"] = blurb
        else:
            result.fields["description"] = config.MISSING_VALUE

        # Cover
        cover = data.get("cover") or {}
        cover_url = ""
        if isinstance(cover, dict):
            cover_url = (
                str(cover.get("large") or cover.get("medium") or cover.get("small") or "")
                .strip()
            )
        if not cover_url:
            cover_url = f"https://covers.openlibrary.org/b/isbn/{isbn13}-L.jpg"
        result.cover_urls = [cover_url]

        # Extras often missing on OL
        result.fields["format"] = config.MISSING_VALUE
        result.fields["price"] = config.MISSING_VALUE
        result.fields["rating"] = config.MISSING_VALUE
        result.fields["ratings_count"] = config.MISSING_VALUE
        result.fields["series"] = config.MISSING_VALUE
        result.fields["edition"] = (
            str(data.get("edition_name") or "").strip() or config.MISSING_VALUE
        )
        # Language if present as list of dicts
        langs = []
        for lang in data.get("languages") or []:
            if isinstance(lang, dict):
                key = str(lang.get("key") or "").rstrip("/").split("/")[-1]
                if key:
                    langs.append(key)
        result.fields["language"] = ", ".join(langs) if langs else config.MISSING_VALUE
        result.fields["origin_country"] = config.MISSING_VALUE

        # OL has essentially no consumer reviews like Goodreads/Amazon.
        result.reviews = []
        result.success = True
        return result
