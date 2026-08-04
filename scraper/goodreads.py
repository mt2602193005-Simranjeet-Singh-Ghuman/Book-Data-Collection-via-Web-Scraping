"""
scraper/goodreads.py

Pulls book details from Goodreads by ISBN.
Tries a few CSS selectors per field because their HTML changes a lot.
"""

from __future__ import annotations

import json
import re
from typing import Optional
from urllib.parse import quote

from bs4 import BeautifulSoup, Tag

import config
from scraper.base import BaseScraper, ScrapedBook


class GoodreadsScraper(BaseScraper):
    """Goodreads-specific scraper (Module 3 first website)."""

    source_name = "Goodreads"

    def build_candidate_urls(self, isbn13: str) -> list[str]:
        """
        Build Goodreads URLs to try for one ISBN.

        Parameters
        ----------
        isbn13 : str
            Normalized ISBN-13.
        """
        encoded = quote(isbn13)
        return [
            f"https://www.goodreads.com/book/isbn/{encoded}",
            f"https://www.goodreads.com/search?q={encoded}",
        ]

    def scrape(
        self,
        isbn13: str,
        *,
        hint_title: str = "",
        hint_authors: str = "",
    ) -> ScrapedBook:
        """
        Extend base scrape with Playwright review enrichment when needed.

        If Level-1/2 got a title but fewer than MIN_REVIEWS_PER_SOURCE reviews,
        open the book URL again in Playwright and try to collect more reviews.
        """
        result = super().scrape(
            isbn13,
            hint_title=hint_title,
            hint_authors=hint_authors,
        )
        if not result.success:
            return result

        if len(result.reviews) >= config.MIN_REVIEWS_PER_SOURCE:
            return result

        page_url = str(result.fields.get("url", "")).strip()
        if not page_url.startswith("http"):
            page_url = self.build_candidate_urls(isbn13)[0]

        extra_reviews = self._collect_reviews_with_playwright(page_url)
        if extra_reviews:
            merged = self.unique_non_empty(result.reviews + extra_reviews)
            result.reviews = merged[: max(config.MIN_REVIEWS_PER_SOURCE, len(merged))]
        return result

    def parse_book_page(
        self,
        soup: BeautifulSoup,
        page_url: str,
        isbn13: str,
    ) -> ScrapedBook:
        """
        Parse Goodreads HTML into ScrapedBook.

        Handles both direct book pages and search-result pages (follows first
        book link textually by reading the first /book/show/ anchor).
        """
        result = ScrapedBook(source=self.source_name, isbn13=isbn13)
        result.fields = self._empty_fields(isbn13)

        # If this is a search page, jump parsing focus to the first book link.
        canonical_url = self._extract_canonical_or_book_url(soup, page_url)
        result.fields["url"] = self.text_or_na(canonical_url)

        title = self._extract_title(soup)
        # Search pages often have no book title widget; mark useless so fallback runs.
        if title == config.MISSING_VALUE and "/search" in page_url:
            return result

        result.fields["title"] = title
        result.fields["authors"] = self._extract_authors(soup)
        result.fields["description"] = self._extract_description(soup)
        result.blurb = (
            result.fields["description"]
            if result.fields["description"] != config.MISSING_VALUE
            else ""
        )

        details = self._extract_details_from_page(soup)
        for key, value in details.items():
            if key in result.fields and value:
                result.fields[key] = self.text_or_na(value)

        genres = self._extract_genres(soup)
        result.fields["genres"] = ", ".join(genres) if genres else config.MISSING_VALUE

        rating, ratings_count = self._extract_rating(soup)
        result.fields["rating"] = rating
        result.fields["ratings_count"] = ratings_count

        result.cover_urls = self._extract_cover_urls(soup)
        result.reviews = self._extract_reviews_from_soup(soup)
        result.fields["isbn13"] = isbn13
        result.fields["source"] = self.source_name
        return result

    # ------------------------------------------------------------------
    # Field extractors
    # ------------------------------------------------------------------
    def _extract_canonical_or_book_url(self, soup: BeautifulSoup, page_url: str) -> str:
        link = soup.find("link", rel="canonical")
        if isinstance(link, Tag) and link.get("href"):
            return str(link.get("href"))

        # Search result: first book/show link
        anchor = soup.select_one('a[href*="/book/show/"]')
        if isinstance(anchor, Tag) and anchor.get("href"):
            href = str(anchor.get("href"))
            if href.startswith("/"):
                return "https://www.goodreads.com" + href
            return href
        return page_url

    def _extract_title(self, soup: BeautifulSoup) -> str:
        selectors = [
            'h1[data-testid="bookTitle"]',
            "h1.Text__title1",
            "#bookTitle",
            "h1",
        ]
        for selector in selectors:
            node = soup.select_one(selector)
            if node:
                text = node.get_text(" ", strip=True)
                if text:
                    return self.text_or_na(text)

        # JSON-LD fallback
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                payload = json.loads(script.string or "")
            except (TypeError, json.JSONDecodeError):
                continue
            candidates = payload if isinstance(payload, list) else [payload]
            for item in candidates:
                if isinstance(item, dict) and item.get("@type") in {"Book", "Product"}:
                    name = item.get("name")
                    if name:
                        return self.text_or_na(str(name))
        return config.MISSING_VALUE

    def _extract_authors(self, soup: BeautifulSoup) -> str:
        authors: list[str] = []
        selectors = [
            'span[data-testid="name"]',
            "a.ContributorLink",
            "#bookAuthors a.authorName",
            ".ContributorLinksList a",
        ]
        for selector in selectors:
            for node in soup.select(selector):
                text = node.get_text(" ", strip=True)
                if text:
                    authors.append(text)
            if authors:
                break
        authors = self.unique_non_empty(authors)
        return ", ".join(authors) if authors else config.MISSING_VALUE

    def _extract_description(self, soup: BeautifulSoup) -> str:
        selectors = [
            'div[data-testid="description"]',
            "#description",
            ".BookPageMetadataSection__description",
            ".Formatted",
        ]
        for selector in selectors:
            node = soup.select_one(selector)
            if not node:
                continue
            text = node.get_text("\n", strip=True)
            # Skip tiny fragments / "more" buttons only
            if text and len(text) > 40:
                return self.text_or_na(text)
        return config.MISSING_VALUE

    def _extract_genres(self, soup: BeautifulSoup) -> list[str]:
        genres: list[str] = []
        selectors = [
            'span[data-testid="genresList"] a',
            ".BookPageMetadataSection__genreButton",
            "a.actionLinkLite.bookPageGenreLink",
            '.BookPageMetadataSection__genres a',
        ]
        for selector in selectors:
            for node in soup.select(selector):
                text = node.get_text(" ", strip=True)
                if text and text.lower() not in {"genres", "...more", "more"}:
                    genres.append(text)
            if genres:
                break
        return self.unique_non_empty(genres)

    def _extract_rating(self, soup: BeautifulSoup) -> tuple[str, str]:
        rating = config.MISSING_VALUE
        count = config.MISSING_VALUE

        rating_node = soup.select_one(
            'div[data-testid="RatingStatistics"] '
            ".RatingStatistics__rating, "
            ".RatingStatistics__rating, "
            "span[itemprop='ratingValue']"
        )
        if rating_node:
            rating = self.text_or_na(rating_node.get_text(" ", strip=True))

        count_node = soup.select_one(
            'div[data-testid="RatingStatistics"] '
            ".RatingStatistics__meta, "
            "meta[itemprop='ratingCount'], "
            "span[data-testid='ratingsCount']"
        )
        if count_node:
            if count_node.name == "meta":
                count = self.text_or_na(str(count_node.get("content", "")))
            else:
                raw = count_node.get_text(" ", strip=True)
                match = re.search(r"([\d,]+)", raw)
                count = self.text_or_na(match.group(1) if match else raw)

        return rating, count

    def _extract_details_from_page(self, soup: BeautifulSoup) -> dict[str, str]:
        """
        Pull publisher / pages / publication date / language from detail rows
        and JSON-LD when available.
        """
        details: dict[str, str] = {}

        # JSON-LD Book object often has reliable structured fields.
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                payload = json.loads(script.string or "")
            except (TypeError, json.JSONDecodeError):
                continue
            candidates = payload if isinstance(payload, list) else [payload]
            for item in candidates:
                if not isinstance(item, dict):
                    continue
                if item.get("@type") not in {"Book", "Product"}:
                    continue
                if item.get("publisher"):
                    pub = item["publisher"]
                    if isinstance(pub, dict):
                        details["publisher"] = str(pub.get("name", ""))
                    else:
                        details["publisher"] = str(pub)
                if item.get("datePublished"):
                    details["publication_date"] = str(item["datePublished"])
                if item.get("inLanguage"):
                    details["language"] = str(item["inLanguage"])
                if item.get("numberOfPages"):
                    details["pages"] = str(item["numberOfPages"])
                if item.get("bookFormat"):
                    details["format"] = str(item["bookFormat"])

        # Visible "pages" / "Published" style text blocks
        page_text = soup.get_text("\n", strip=True)
        pages_match = re.search(r"(\d+)\s*pages", page_text, flags=re.I)
        if pages_match and "pages" not in details:
            details["pages"] = pages_match.group(1)

        published_match = re.search(
            r"Published\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4}|\d{4})",
            page_text,
            flags=re.I,
        )
        if published_match and "publication_date" not in details:
            details["publication_date"] = published_match.group(1)

        return details

    def _extract_cover_urls(self, soup: BeautifulSoup) -> list[str]:
        urls: list[str] = []
        selectors = [
            'img[data-testid="coverImage"]',
            ".BookCover__image img",
            "#coverImage",
            "img.ResponsiveImage",
        ]
        for selector in selectors:
            for img in soup.select(selector):
                src = img.get("src") or img.get("data-src")
                if src and str(src).startswith("http"):
                    urls.append(str(src))
            if urls:
                break
        return self.unique_non_empty(urls)

    def _extract_reviews_from_soup(self, soup: BeautifulSoup) -> list[str]:
        reviews: list[str] = []
        selectors = [
            'section[data-testid="reviewsSection"] '
            'div[data-testid="reviewTextContent"]',
            'div[data-testid="reviewTextContent"]',
            ".ReviewText",
            ".reviewText",
            "div.Formatted",
        ]
        for selector in selectors:
            for node in soup.select(selector):
                text = node.get_text("\n", strip=True)
                # Avoid grabbing the main description block as a "review"
                if text and len(text) > 40:
                    reviews.append(text)
            if reviews:
                break
        return self.unique_non_empty(reviews)

    def _collect_reviews_with_playwright(self, page_url: str) -> list[str]:
        """
        Use Playwright to load the book page and collect more review texts.

        Returns
        -------
        list[str]
            Extra review strings (may be empty if Playwright unavailable/blocked).
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return []

        reviews: list[str] = []
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(
                    user_agent=self.DEFAULT_HEADERS["User-Agent"],
                    locale="en-US",
                )
                page = context.new_page()
                page.goto(
                    page_url,
                    wait_until="domcontentloaded",
                    timeout=config.HTTP_TIMEOUT_SECONDS * 1000,
                )
                page.wait_for_timeout(2000)

                # Scroll a few times to trigger lazy-loaded reviews.
                for _ in range(6):
                    page.mouse.wheel(0, 2500)
                    page.wait_for_timeout(800)

                # Click common "Show more reviews" style buttons if present.
                for label in ("Show more reviews", "More filters", "Choose shelves"):
                    try:
                        button = page.get_by_role("button", name=re.compile(label, re.I))
                        if button.count() > 0:
                            button.first.click(timeout=1500)
                            page.wait_for_timeout(1000)
                    except Exception:  # noqa: BLE001
                        pass

                html = page.content()
                context.close()
                browser.close()
                soup = self.make_soup(html)
                reviews = self._extract_reviews_from_soup(soup)
        except Exception:  # noqa: BLE001
            return []
        return reviews
