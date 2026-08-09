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
from utils.title_match import listing_matches_hints, note_title_match


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
            # Goodreads search uses "query" (not only "q").
            f"https://www.goodreads.com/search?utf8=%E2%9C%93&query={encoded}",
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
        ISBN lookup first; title/author search if another site already found
        the book. Enrich reviews with Playwright when short.
        """
        result = super().scrape(
            isbn13,
            hint_title=hint_title,
            hint_authors=hint_authors,
        )
        if not result.success:
            title_hit = self._scrape_by_title_author(
                isbn13,
                hint_title=hint_title,
                hint_authors=hint_authors,
            )
            if title_hit is not None:
                result = title_hit
            else:
                return result

        return self._enrich_reviews_if_needed(result)

    def _scrape_by_title_author(
        self,
        isbn13: str,
        *,
        hint_title: str,
        hint_authors: str,
    ) -> Optional[ScrapedBook]:
        """Search Goodreads by title/author when ISBN pages fail."""
        title = (hint_title or "").strip()
        if not title or title == config.MISSING_VALUE:
            return None
        authors = (hint_authors or "").strip()
        if authors == config.MISSING_VALUE:
            authors = ""
        encoded = quote(f"{title} {authors}".strip())
        urls = [
            f"https://www.goodreads.com/search?utf8=%E2%9C%93&query={encoded}",
            f"https://www.goodreads.com/search?q={encoded}",
        ]
        for url in urls:
            for method, fetcher in (
                ("requests+bs4", self.fetch_html_requests),
                ("playwright", self.fetch_html_playwright),
            ):
                self.polite_delay()
                html = fetcher(url)
                if not html:
                    continue
                soup = self.make_soup(html)
                parsed = self.parse_book_page(soup, page_url=url, isbn13=isbn13)
                # Follow first /book/show/ if still on search results.
                book_url = self._extract_canonical_or_book_url(soup, url)
                if book_url and book_url != url and (
                    not self.is_parse_useful(parsed)
                    or "/search" in str(parsed.fields.get("url", url))
                ):
                    self.polite_delay()
                    book_html = fetcher(book_url)
                    if book_html:
                        soup = self.make_soup(book_html)
                        parsed = self.parse_book_page(
                            soup, page_url=book_url, isbn13=isbn13
                        )
                if not self.is_parse_useful(parsed):
                    continue
                if not listing_matches_hints(
                    hint_title=hint_title,
                    hint_authors=hint_authors,
                    found_title=str(parsed.fields.get("title", "")),
                    found_authors=str(parsed.fields.get("authors", "")),
                ):
                    continue
                parsed.method_used = method
                parsed.success = True
                note_title_match(parsed.fields)
                return parsed
        return None

    def _enrich_reviews_if_needed(self, result: ScrapedBook) -> ScrapedBook:
        """Open the book URL in Playwright when review count is short."""
        if len(result.reviews) >= config.MIN_REVIEWS_PER_SOURCE:
            return result

        page_url = str(result.fields.get("url", "")).strip()
        if not page_url.startswith("http"):
            page_url = self.build_candidate_urls(result.isbn13)[0]

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
        Pull publisher / pages / publication date / language for PL Assignment.

        Priority observed via Inspect Element / page source:
        1) __NEXT_DATA__ Apollo Book details (publisher is here as a string)
        2) JSON-LD Book/Product
        3) Visible publication text
        """
        details: dict[str, str] = {}

        # 1) Apollo state inside __NEXT_DATA__ (most reliable for publisher).
        # Example inspected fragment:
        #   "publisher":"Penguin Press","isbn":"159420571X","isbn13":"9781594205712"
        script = soup.find("script", id="__NEXT_DATA__")
        if isinstance(script, Tag) and script.string:
            try:
                next_data = json.loads(script.string)
                apollo = (
                    next_data.get("props", {})
                    .get("pageProps", {})
                    .get("apolloState", {})
                )
                if isinstance(apollo, dict):
                    # Prefer the Book node that actually has details.publisher.
                    book_nodes = [
                        value
                        for key, value in apollo.items()
                        if isinstance(key, str)
                        and key.startswith("Book:")
                        and isinstance(value, dict)
                        and value.get("title")
                    ]
                    book_nodes.sort(
                        key=lambda node: 0
                        if isinstance(node.get("details"), dict)
                        and (node.get("details") or {}).get("publisher")
                        else 1
                    )
                    for value in book_nodes:
                        book_details = value.get("details") or {}
                        if isinstance(book_details, dict):
                            if book_details.get("publisher"):
                                details["publisher"] = str(book_details["publisher"])
                            if book_details.get("numPages") is not None:
                                details["pages"] = str(book_details["numPages"])
                            lang = book_details.get("language")
                            if isinstance(lang, dict) and lang.get("name"):
                                details["language"] = str(lang["name"])
                            elif isinstance(lang, str) and lang:
                                details["language"] = lang
                            pub_time = book_details.get("publicationTime")
                            if pub_time and "publication_date" not in details:
                                try:
                                    import time as _time

                                    details["publication_date"] = _time.strftime(
                                        "%Y-%m-%d",
                                        _time.gmtime(float(pub_time) / 1000.0),
                                    )
                                except (TypeError, ValueError, OSError):
                                    pass
                        if value.get("publisher"):
                            details.setdefault("publisher", str(value["publisher"]))
                        if details.get("publisher"):
                            break
            except (TypeError, json.JSONDecodeError):
                pass

        # Regex fallback: Apollo often embeds "publisher":"Penguin Press"
        if "publisher" not in details and script and script.string:
            pub_match = re.search(
                r'"publisher"\s*:\s*"([^"]{2,120})"',
                script.string,
            )
            if pub_match:
                candidate = pub_match.group(1).strip()
                if candidate.lower() not in {"book", "null", "none"}:
                    details["publisher"] = candidate

        # 2) JSON-LD Book object
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
                if item.get("publisher") and "publisher" not in details:
                    pub = item["publisher"]
                    if isinstance(pub, dict):
                        details["publisher"] = str(pub.get("name", ""))
                    else:
                        details["publisher"] = str(pub)
                if item.get("datePublished"):
                    details.setdefault("publication_date", str(item["datePublished"]))
                if item.get("inLanguage"):
                    details.setdefault("language", str(item["inLanguage"]))
                if item.get("numberOfPages"):
                    details.setdefault("pages", str(item["numberOfPages"]))
                if item.get("bookFormat"):
                    details.setdefault("format", str(item["bookFormat"]))
                # Some Product schemas expose country via offers / brand country.
                country = item.get("countryOfOrigin") or item.get("contentLocation")
                if isinstance(country, dict) and country.get("name"):
                    details["origin_country"] = str(country["name"])
                elif isinstance(country, str) and country:
                    details["origin_country"] = country

        # 3) Visible "pages" / "Published" style text blocks
        page_text = soup.get_text("\n", strip=True)
        pages_match = re.search(r"(\d+)\s*pages", page_text, flags=re.I)
        if pages_match and "pages" not in details:
            details["pages"] = pages_match.group(1)

        published_match = re.search(
            r"(?:First published|Published)\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4}|\d{4})",
            page_text,
            flags=re.I,
        )
        if published_match and "publication_date" not in details:
            details["publication_date"] = published_match.group(1)

        # Rare visible publisher line near edition details.
        if "publisher" not in details:
            pub_match = re.search(
                r"(?:Publisher|Published by)\s*[:\-]?\s*([A-Za-z0-9][^\n|]{2,80})",
                page_text,
                flags=re.I,
            )
            if pub_match:
                details["publisher"] = pub_match.group(1).strip()

        return details

    def _extract_cover_urls(self, soup: BeautifulSoup) -> list[str]:
        urls: list[str] = []
        selectors = [
            'img[data-testid="coverImage"]',
            ".BookCover__image img",
            "#coverImage",
            "img.ResponsiveImage",
            'img[src*="images.gr-assets.com"]',
            'img[src*="i.gr-assets.com"]',
        ]
        for selector in selectors:
            for img in soup.select(selector):
                src = img.get("src") or img.get("data-src")
                if src and str(src).startswith("http"):
                    urls.append(str(src))
            if urls:
                break
        if not urls:
            for meta in soup.select('meta[property="og:image"], meta[name="og:image"]'):
                content = str(meta.get("content") or "").strip()
                if content.startswith("http"):
                    urls.append(content)
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
        Use shared Playwright to load the book page and collect more review texts.

        Returns
        -------
        list[str]
            Extra review strings (may be empty if Playwright unavailable/blocked).
        """
        try:
            from scraper.browser_pool import shared_page
        except ImportError:
            return []

        reviews: list[str] = []
        try:
            with shared_page(
                user_agent=self.DEFAULT_HEADERS["User-Agent"],
                locale="en-US",
            ) as page:
                page.goto(
                    page_url,
                    wait_until="domcontentloaded",
                    timeout=config.HTTP_TIMEOUT_SECONDS * 1000,
                )
                page.wait_for_timeout(1200)

                # Scroll to trigger lazy-loaded reviews (enough for ~10 reviews).
                for _ in range(3):
                    page.mouse.wheel(0, 2500)
                    page.wait_for_timeout(500)

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
                soup = self.make_soup(html)
                reviews = self._extract_reviews_from_soup(soup)
        except Exception:  # noqa: BLE001
            return []
        return reviews
