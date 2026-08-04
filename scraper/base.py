"""
scraper/base.py

Shared base class for all website scrapers.

Level 1: requests + BeautifulSoup (default)
Level 2: Playwright, only if Level 1 cannot get useful HTML

Each site module inherits this and fills in its own URLs / selectors.
We wait 1–2 seconds between requests and try not to crash on failures.
"""

from __future__ import annotations

import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

import requests
from bs4 import BeautifulSoup

import config


@dataclass
class ScrapedBook:
    """
    Normalized result returned by every website scraper.

    Attributes
    ----------
    source : str
        Website display name (e.g. "Goodreads").
    isbn13 : str
        Normalized ISBN-13.
    fields : dict
        Metadata keys aligned with config.METADATA_FIELDS.
    cover_urls : list[str]
        Remote cover image URLs to download.
    blurb : str
        Description / blurb text (also mirrored into fields['description']).
    reviews : list[str]
        Individual review texts (saved as separate files by media helpers).
    method_used : str
        "requests+bs4", "playwright", or "none".
    success : bool
        True if at least a book page / title was found.
    error : str
        Error summary when success is False (else "").
    """

    source: str
    isbn13: str
    fields: dict[str, Any] = field(default_factory=dict)
    cover_urls: list[str] = field(default_factory=list)
    blurb: str = ""
    reviews: list[str] = field(default_factory=list)
    method_used: str = "none"
    success: bool = False
    error: str = ""


class BaseScraper(ABC):
    """
    Abstract scraper with automatic Level-1 -> Level-2 fallback.

    Child classes must implement:
        - source_name
        - build_candidate_urls(isbn13)
        - parse_book_page(soup, page_url, isbn13)
        - is_parse_useful(result)  (optional override)
    """

    source_name: str = "Base"

    # Realistic browser-like headers reduce trivial bot blocks at Level 1.
    DEFAULT_HEADERS: dict[str, str] = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    }

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(self.DEFAULT_HEADERS)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def scrape(
        self,
        isbn13: str,
        *,
        hint_title: str = "",
        hint_authors: str = "",
    ) -> ScrapedBook:
        """
        Scrape one ISBN from this website using Level-1 then Level-2 fallback.

        hint_title / hint_authors are optional extras from a site that already
        succeeded (e.g. Amazon/Goodreads). Child classes may use them when the
        ISBN alone is not listed (common on Kobo/Audible/BookBub).
        """
        # Default engine ignores hints; subclasses that need them override scrape().
        _ = hint_title, hint_authors
        result = ScrapedBook(source=self.source_name, isbn13=isbn13)
        result.fields = self._empty_fields(isbn13)

        try:
            urls = self.build_candidate_urls(isbn13)
        except Exception as exc:  # noqa: BLE001 - must never crash the lab app
            result.error = f"URL build failed: {exc}"
            return result

        # -------- Level 1: requests + BeautifulSoup --------
        for url in urls:
            self.polite_delay()
            html = self.fetch_html_requests(url)
            if not html:
                continue
            soup = self.make_soup(html)
            parsed = self.parse_book_page(soup, page_url=url, isbn13=isbn13)
            if self.is_parse_useful(parsed):
                parsed.method_used = "requests+bs4"
                parsed.success = True
                return parsed

        # -------- Level 2: Playwright (only if Level 1 failed) --------
        for url in urls:
            self.polite_delay()
            html = self.fetch_html_playwright(url)
            if not html:
                continue
            soup = self.make_soup(html)
            parsed = self.parse_book_page(soup, page_url=url, isbn13=isbn13)
            if self.is_parse_useful(parsed):
                parsed.method_used = "playwright"
                parsed.success = True
                return parsed

        result.error = (
            f"{self.source_name}: could not extract usable book data "
            f"with requests+bs4 or Playwright for ISBN {isbn13}"
        )
        return result

    # ------------------------------------------------------------------
    # Abstract / overridable hooks
    # ------------------------------------------------------------------
    @abstractmethod
    def build_candidate_urls(self, isbn13: str) -> list[str]:
        """Return one or more page URLs to try for this ISBN."""

    @abstractmethod
    def parse_book_page(
        self,
        soup: BeautifulSoup,
        page_url: str,
        isbn13: str,
    ) -> ScrapedBook:
        """Parse a book page into a ScrapedBook object."""

    def is_parse_useful(self, parsed: ScrapedBook) -> bool:
        """
        Decide whether Level-1 output is good enough (else try Playwright).

        Default rule: title must exist and not be N/A.
        """
        title = str(parsed.fields.get("title", config.MISSING_VALUE)).strip()
        return bool(title) and title != config.MISSING_VALUE

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------
    def polite_delay(self) -> None:
        """Sleep randomly between 1 and 2 seconds (assignment requirement)."""
        low, high = config.REQUEST_DELAY_SECONDS
        time.sleep(random.uniform(low, high))

    def fetch_html_requests(self, url: str) -> Optional[str]:
        """
        Level-1 fetch with retries/timeouts.

        Returns
        -------
        str | None
            HTML text on success, else None.
        """
        last_error = ""
        for attempt in range(1, config.MAX_RETRIES + 1):
            try:
                response = self.session.get(
                    url,
                    timeout=config.HTTP_TIMEOUT_SECONDS,
                    allow_redirects=True,
                )
                if response.status_code == 200 and response.text.strip():
                    # Very small / challenge pages are treated as failure.
                    if len(response.text) < 500:
                        last_error = "Response too small"
                    else:
                        return response.text
                else:
                    last_error = f"HTTP {response.status_code}"
            except requests.RequestException as exc:
                last_error = str(exc)

            # Short backoff before retry (still soft-fail overall).
            time.sleep(min(attempt, 3))

        # Caller logs via preprocessing CSV; keep this layer quiet-ish.
        _ = last_error
        return None

    def fetch_html_playwright(self, url: str) -> Optional[str]:
        """
        Level-2 fetch using headless Chromium via Playwright.

        Returns
        -------
        str | None
            Rendered HTML on success, else None.
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return None

        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(
                    user_agent=self.DEFAULT_HEADERS["User-Agent"],
                    locale="en-US",
                )
                page = context.new_page()
                page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=config.HTTP_TIMEOUT_SECONDS * 1000,
                )
                # Extra wait helps JS-rendered book widgets appear.
                page.wait_for_timeout(2500)
                html = page.content()
                context.close()
                browser.close()
                if html and len(html) > 500:
                    return html
        except Exception:  # noqa: BLE001
            return None
        return None

    def make_soup(self, html: str) -> BeautifulSoup:
        """Create a BeautifulSoup document (lxml parser for speed)."""
        return BeautifulSoup(html, "lxml")

    def _empty_fields(self, isbn13: str) -> dict[str, str]:
        """Return a full N/A field dictionary for this source."""
        fields: dict[str, str] = {}
        for key in config.METADATA_FIELDS:
            if key == "isbn13":
                fields[key] = isbn13
            elif key == "source":
                fields[key] = self.source_name
            else:
                fields[key] = config.MISSING_VALUE
        return fields

    @staticmethod
    def text_or_na(value: Optional[str]) -> str:
        """Normalize empty/None text to config.MISSING_VALUE."""
        if value is None:
            return config.MISSING_VALUE
        text = " ".join(str(value).split())
        return text if text else config.MISSING_VALUE

    @staticmethod
    def unique_non_empty(values: list[str]) -> list[str]:
        """Deduplicate strings while preserving order; drop empties."""
        seen: set[str] = set()
        output: list[str] = []
        for value in values:
            text = " ".join(str(value).split())
            if not text or text in seen:
                continue
            seen.add(text)
            output.append(text)
        return output
