"""
scraper/bookbub.py

Tries to find a book on BookBub by ISBN, then by title/author.
Search is often unavailable outside the US, so we soft-fail with N/A
when discovery does not work. Book pages themselves parse fine when found.
"""

from __future__ import annotations

import json
import re
from html import unescape
from typing import Any, Optional
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup, Tag

import config
from scraper.base import BaseScraper, ScrapedBook
from utils.isbn import isbn13_to_isbn10
from utils.title_match import (
    listing_matches_hints,
    note_title_match,
    significant_title_tokens,
)


class BookBubScraper(BaseScraper):
    """BookBub-specific scraper (Module 7)."""

    source_name = "BookBub"

    def build_candidate_urls(self, isbn13: str) -> list[str]:
        """Build BookBub search URLs for one ISBN (+ ISBN-10 if possible)."""
        queries = [isbn13]
        isbn10 = isbn13_to_isbn10(isbn13)
        if isbn10:
            queries.append(isbn10)
        urls: list[str] = []
        for query in queries:
            encoded = quote(query)
            urls.extend(
                [
                    f"https://www.bookbub.com/search?search={encoded}",
                    f"https://www.bookbub.com/search?q={encoded}",
                    f"https://www.bookbub.com/search?utf8=%E2%9C%93&search={encoded}",
                ]
            )
        return urls

    def scrape(
        self,
        isbn13: str,
        *,
        hint_title: str = "",
        hint_authors: str = "",
    ) -> ScrapedBook:
        """
        Search BookBub by ISBN, then by title/author when ISBN search fails.

        Soft-fails when:
          - search is geo-blocked / 404
          - Cloudflare blocks Level-1/2
          - no book result is found
        """
        result = ScrapedBook(source=self.source_name, isbn13=isbn13)
        result.fields = self._empty_fields(isbn13)

        product_urls, discovery_note = self._resolve_product_urls_from_searches(
            self.build_candidate_urls(isbn13)
        )
        used_title_fallback = False
        if not product_urls:
            # Prefer guessed /books/<slug> URLs first: BookBub /search is often
            # geo-blocked and can return truncated junk links like /books/everything.
            direct_books = [
                f"https://www.bookbub.com/books/{slug}"
                for slug in self._title_author_slugs(hint_title, hint_authors)
            ]
            search_urls = self._build_title_search_urls(hint_title, hint_authors)
            search_urls = [u for u in search_urls if "/books/" not in u]
            if direct_books:
                product_urls = direct_books
                discovery_note = "Tried BookBub /books/<slug> from title+author."
                used_title_fallback = True
            if search_urls:
                searched, title_note = self._resolve_product_urls_from_searches(
                    search_urls
                )
                searched = self._filter_book_urls_for_title(searched, hint_title)
                if searched:
                    # Keep slug guesses first, then filtered search hits.
                    product_urls = self.unique_non_empty(product_urls + searched)
                    used_title_fallback = True
                    discovery_note = title_note
                elif not product_urls:
                    discovery_note = f"{discovery_note} Title search: {title_note}"

        if not product_urls:
            # Keep discovery_note in the log only (via result.error detail for CSV).
            result.error = (
                f"BookBub: could not fetch book data for ISBN {isbn13}. "
                f"{discovery_note}"
            )
            return result

        for product_url in product_urls:
            for method, fetcher in (
                ("requests+bs4", self.fetch_html_requests),
                ("playwright", self.fetch_html_playwright),
            ):
                self.polite_delay()
                html = fetcher(product_url)
                if not html or self._looks_like_block(html) or self._is_not_found(html):
                    continue
                parsed = self.parse_book_page(
                    self.make_soup(html),
                    page_url=product_url,
                    isbn13=isbn13,
                )
                if not self.is_parse_useful(parsed):
                    continue
                if used_title_fallback and not listing_matches_hints(
                    hint_title=hint_title,
                    hint_authors=hint_authors,
                    found_title=str(parsed.fields.get("title", "")),
                    found_authors=str(parsed.fields.get("authors", "")),
                ):
                    continue
                parsed.method_used = method
                parsed.success = True
                if used_title_fallback:
                    note_title_match(parsed.fields)
                return parsed

        result.error = f"BookBub: could not fetch book data for ISBN {isbn13}."
        return result

    def _build_title_search_urls(self, title: str, authors: str) -> list[str]:
        title = (title or "").strip()
        if not title or title == config.MISSING_VALUE:
            return []
        authors = (authors or "").strip()
        if authors == config.MISSING_VALUE:
            authors = ""
        query = quote(f"{title} {authors}".strip())
        urls = [
            f"https://www.bookbub.com/search?search={query}",
            f"https://www.bookbub.com/search?q={query}",
        ]
        # When /search is geo-blocked, BookBub book pages often still work via slug:
        #   /books/everything-i-never-told-you-by-celeste-ng
        for slug in self._title_author_slugs(title, authors):
            urls.append(f"https://www.bookbub.com/books/{slug}")
        return urls

    @staticmethod
    def _title_author_slugs(title: str, authors: str) -> list[str]:
        """Build likely BookBub /books/<slug> paths from title + author."""
        from utils.title_match import clean_hint_title

        def slugify(text: str) -> str:
            text = text.lower().strip()
            text = re.sub(r"['’]", "", text)
            text = re.sub(r"[^a-z0-9]+", "-", text)
            return text.strip("-")

        title = (title or "").strip()
        if not title or title == config.MISSING_VALUE:
            return []
        # Try cleaned title first (strip series parentheses), then raw.
        title_variants = []
        cleaned = clean_hint_title(title)
        if cleaned:
            title_variants.append(cleaned)
        if title not in title_variants:
            title_variants.append(title)

        first_author = authors.split(",")[0].strip() if authors else ""
        if first_author == config.MISSING_VALUE:
            first_author = ""
        author_slug = slugify(first_author) if first_author else ""

        slugs: list[str] = []
        for variant in title_variants:
            title_slug = slugify(variant)
            if not title_slug:
                continue
            if author_slug:
                slugs.append(f"{title_slug}-by-{author_slug}")
            slugs.append(title_slug)
        return list(dict.fromkeys(slugs))

    def _filter_book_urls_for_title(self, urls: list[str], title: str) -> list[str]:
        """Drop truncated /books/<one-word> links that cannot match the title."""
        tokens = significant_title_tokens(title)
        if not tokens:
            return urls
        kept: list[str] = []
        for url in urls:
            match = re.search(r"/books/([^/?#]+)", url)
            if not match:
                continue
            slug = match.group(1).lower()
            slug_tokens = set(re.findall(r"[a-z0-9]+", slug)) - {"by"}
            # Reject single-token junk like /books/everything
            if len(slug_tokens) < 2:
                continue
            if len(tokens & slug_tokens) >= max(2, min(3, len(tokens) // 2)):
                kept.append(url)
        return kept

    def is_parse_useful(self, parsed: ScrapedBook) -> bool:
        """Reject BookBub chrome / not-found shells."""
        title = str(parsed.fields.get("title", "")).strip().lower()
        if not title or title == config.MISSING_VALUE.lower():
            return False
        banned = [
            "page not found",
            "just a moment",
            "amazing deals on bestselling ebooks",
            "bookbub",
        ]
        # Exact site-name title only is useless; real book titles are longer.
        if title in {"bookbub", "page not found - bookbub"}:
            return False
        return not any(b == title for b in banned)

    def fetch_html_requests(self, url: str) -> Optional[str]:
        """Level-1 fetch; drop Cloudflare challenge shells."""
        html = super().fetch_html_requests(url)
        if html and self._looks_like_block(html):
            return None
        return html

    def fetch_html_playwright(self, url: str) -> Optional[str]:
        """Level-2 fetch tuned for BookBub / Cloudflare."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return None

        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                context = browser.new_context(
                    user_agent=self.DEFAULT_HEADERS["User-Agent"],
                    locale="en-US",
                    viewport={"width": 1366, "height": 768},
                )
                # Prefer US storefront cookies (search is US-oriented).
                context.add_cookies(
                    [
                        {
                            "name": "country",
                            "value": "US",
                            "domain": ".bookbub.com",
                            "path": "/",
                        },
                        {
                            "name": "bookbub_country",
                            "value": "US",
                            "domain": ".bookbub.com",
                            "path": "/",
                        },
                    ]
                )
                page = context.new_page()
                page.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                )
                page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=config.HTTP_TIMEOUT_SECONDS * 1000,
                )
                page.wait_for_timeout(2500)
                html = page.content()
                final_url = page.url
                context.close()
                browser.close()
                if html and len(html) > 2000 and not self._looks_like_block(html):
                    return f"<!-- BOOKBUB_FINAL_URL:{final_url} -->\n" + html
        except Exception:  # noqa: BLE001
            return None
        return None

    def parse_book_page(
        self,
        soup: BeautifulSoup,
        page_url: str,
        isbn13: str,
    ) -> ScrapedBook:
        """Parse a BookBub book page into ScrapedBook."""
        result = ScrapedBook(source=self.source_name, isbn13=isbn13)
        result.fields = self._empty_fields(isbn13)

        final_url = (
            self._extract_final_url_marker(str(soup)[:800])
            or self._canonical_url(soup)
            or page_url
        )
        result.fields["url"] = self.text_or_na(final_url)

        ld_fields, covers, reviews, blurb = self._extract_from_json_ld(soup)
        for key, value in ld_fields.items():
            if key in result.fields and value:
                result.fields[key] = self.text_or_na(value)
        result.cover_urls = covers
        result.reviews = reviews
        result.blurb = blurb
        if blurb and result.fields.get("description") in {
            config.MISSING_VALUE,
            "",
            None,
        }:
            result.fields["description"] = self.text_or_na(blurb)

        if result.fields.get("title") in {config.MISSING_VALUE, None, ""}:
            result.fields["title"] = self._extract_title_html(soup)
        if result.fields.get("authors") in {config.MISSING_VALUE, None, ""}:
            result.fields["authors"] = self._extract_authors_html(soup)
        if result.fields.get("genres") in {config.MISSING_VALUE, None, ""}:
            genres = self._extract_genres_html(soup)
            if genres:
                result.fields["genres"] = ", ".join(genres)

        # Extra PL Assignment fields from visible label/value text when present.
        visible = self._extract_visible_metadata(soup)
        for key, value in visible.items():
            if key in result.fields and value:
                if result.fields.get(key) in {config.MISSING_VALUE, "", None}:
                    result.fields[key] = self.text_or_na(value)

        if not result.cover_urls:
            result.cover_urls = self._extract_cover_urls_html(soup)
        if not result.reviews:
            result.reviews = self._extract_reviews_html(soup)

        result.fields["isbn13"] = isbn13
        result.fields["source"] = self.source_name
        result.fields.setdefault("format", "EBook")
        if result.fields.get("format") == config.MISSING_VALUE:
            result.fields["format"] = "EBook"
        return result

    def _extract_visible_metadata(self, soup: BeautifulSoup) -> dict[str, str]:
        """
        Best-effort label parsing for publisher / language / date / country
        from BookBub book pages (Inspect Element text patterns).
        """
        fields: dict[str, str] = {}
        page_text = soup.get_text("\n", strip=True)
        patterns = {
            "publisher": r"(?:Publisher|Published by)\s*[:\-]\s*([^\n]{2,80})",
            "language": r"(?:Language)\s*[:\-]\s*([A-Za-z][A-Za-z\-\s]{1,40})",
            "publication_date": (
                r"(?:Publication Date|Release Date)\s*[:\-]\s*"
                r"([A-Za-z]+\s+\d{1,2},?\s+\d{4}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4})"
            ),
            "origin_country": r"(?:Country of Origin|Origin Country)\s*[:\-]\s*([A-Za-z][A-Za-z\s]{1,40})",
        }
        banned_values = {
            "description",
            "from bookbub",
            "share with your network",
            "buy this book",
            "more",
        }
        for key, pattern in patterns.items():
            match = re.search(pattern, page_text, flags=re.I)
            if not match:
                continue
            value = match.group(1).strip()
            if value.lower() in banned_values:
                continue
            fields[key] = value
        return fields

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------
    def _resolve_product_urls_from_searches(
        self, search_urls: list[str]
    ) -> tuple[list[str], str]:
        """
        Try BookBub search pages and collect /books/... links.

        Returns
        -------
        (urls, note)
            urls may be empty; note explains soft-fail reason for the log file.
        """
        saw_not_found = False
        saw_challenge = False
        found: list[str] = []

        for search_url in search_urls:
            self.polite_delay()
            html = self.fetch_html_requests(search_url)
            final_url = search_url
            if not html or self._looks_like_block(html):
                if html and self._looks_like_block(html):
                    saw_challenge = True
                html = self.fetch_html_playwright(search_url)
                if html:
                    final_url = self._extract_final_url_marker(html[:800]) or search_url

            if not html:
                continue
            if self._looks_like_block(html):
                saw_challenge = True
                continue
            if self._is_not_found(html) or "Page Not Found" in html:
                saw_not_found = True
                continue

            soup = self.make_soup(html)
            for href in self._extract_book_links(soup, base_url=final_url):
                found.append(href)
            if found:
                break

        if found:
            return self.unique_non_empty(found), "Search returned book links."

        reasons: list[str] = []
        if saw_not_found:
            reasons.append(
                "BookBub /search appears unavailable from this region "
                "(US search feature / Page Not Found)."
            )
        if saw_challenge:
            reasons.append("Cloudflare challenge encountered.")
        if not reasons:
            reasons.append("No matching BookBub book result for this query.")
        return [], " ".join(reasons)

    def _extract_book_links(self, soup: BeautifulSoup, base_url: str) -> list[str]:
        """Extract /books/<slug> links from a search-results style page."""
        links: list[str] = []
        for anchor in soup.select('a[href*="/books/"]'):
            href = str(anchor.get("href") or "")
            if "/books/" not in href:
                continue
            # Skip non-book paths
            if any(bad in href for bad in ("/books/search", "/books?", "#")):
                continue
            absolute = urljoin(base_url, href.split("?")[0])
            # Expect /books/some-slug
            if re.search(r"/books/[^/]+/?$", absolute):
                links.append(absolute)
        return links

    # ------------------------------------------------------------------
    # Extractors
    # ------------------------------------------------------------------
    def _extract_from_json_ld(
        self, soup: BeautifulSoup
    ) -> tuple[dict[str, str], list[str], list[str], str]:
        fields: dict[str, str] = {}
        covers: list[str] = []
        reviews: list[str] = []
        blurb = ""

        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                payload = json.loads(script.string or "")
            except (TypeError, json.JSONDecodeError):
                continue
            for item in self._as_list(payload):
                if not isinstance(item, dict):
                    continue
                types = {str(t) for t in self._as_list(item.get("@type"))}
                if "Book" not in types and "Product" not in types:
                    continue

                if item.get("name"):
                    fields["title"] = str(item["name"])
                if item.get("description"):
                    blurb = self._strip_html(str(item["description"]))
                    fields["description"] = blurb
                if item.get("image"):
                    covers.extend([str(u) for u in self._as_list(item["image"])])
                authors = []
                for author in self._as_list(item.get("author")):
                    if isinstance(author, dict) and author.get("name"):
                        authors.append(str(author["name"]))
                    elif isinstance(author, str):
                        authors.append(author)
                if authors:
                    fields["authors"] = ", ".join(self.unique_non_empty(authors))
                if item.get("isbn"):
                    fields["_page_isbn"] = re.sub(r"[^0-9Xx]", "", str(item["isbn"]))
                # Extra PL fields when BookBub embeds them in JSON-LD.
                publisher = item.get("publisher")
                if isinstance(publisher, dict) and publisher.get("name"):
                    fields["publisher"] = str(publisher["name"])
                elif isinstance(publisher, str) and publisher.strip():
                    fields["publisher"] = publisher.strip()
                for date_key in ("datePublished", "dateCreated", "releaseDate"):
                    if item.get(date_key):
                        fields["publication_date"] = str(item[date_key])[:32]
                        break
                language = item.get("inLanguage") or item.get("language")
                if isinstance(language, dict) and language.get("name"):
                    fields["language"] = str(language["name"])
                elif isinstance(language, str) and language.strip():
                    fields["language"] = language.strip()
                rating = item.get("aggregateRating")
                if isinstance(rating, dict):
                    if rating.get("ratingValue") is not None:
                        try:
                            fields["rating"] = f"{float(rating['ratingValue']):.2f}"
                        except (TypeError, ValueError):
                            fields["rating"] = str(rating["ratingValue"])
                    count = rating.get("ratingCount") or rating.get("reviewCount")
                    if count is not None:
                        fields["ratings_count"] = str(count)
                if item.get("genre"):
                    genres = [str(g) for g in self._as_list(item["genre"])]
                    fields["genres"] = ", ".join(self.unique_non_empty(genres))
                for review in self._as_list(item.get("review")):
                    if isinstance(review, dict):
                        body = review.get("reviewBody") or review.get("description")
                        title = review.get("name")
                        text = "\n".join(
                            part
                            for part in [str(title or "").strip(), str(body or "").strip()]
                            if part
                        )
                        if text:
                            reviews.append(text)

        return fields, self.unique_non_empty(covers), self.unique_non_empty(reviews), blurb

    def _extract_title_html(self, soup: BeautifulSoup) -> str:
        node = soup.select_one("h1")
        if node:
            return self.text_or_na(node.get_text(" ", strip=True))
        return config.MISSING_VALUE

    def _extract_authors_html(self, soup: BeautifulSoup) -> str:
        """
        Prefer author links near the title. Avoid "More from the Author"
        carousels that list unrelated writers further down the page.
        """
        authors: list[str] = []

        # 1) Links immediately after H1 are usually the primary author(s).
        h1 = soup.select_one("h1")
        if h1 is not None:
            for sibling in list(h1.next_siblings)[:12]:
                if not isinstance(sibling, Tag):
                    continue
                for anchor in sibling.select('a[href*="/authors/"]'):
                    text = anchor.get_text(" ", strip=True)
                    if text and "followers" not in text.lower() and len(text) <= 80:
                        authors.append(text)
                if authors:
                    break

        # 2) Fallback: first clean author link on the page only.
        if not authors:
            for anchor in soup.select('a[href*="/authors/"]'):
                text = anchor.get_text(" ", strip=True)
                if text and "followers" not in text.lower() and len(text) <= 80:
                    authors.append(text)
                    break

        authors = self.unique_non_empty(authors)
        return ", ".join(authors) if authors else config.MISSING_VALUE

    def _extract_genres_html(self, soup: BeautifulSoup) -> list[str]:
        genres: list[str] = []
        for anchor in soup.select(
            'a[href*="/categories/"], a[href*="/tags/"], a[href*="/genre/"]'
        ):
            text = anchor.get_text(" ", strip=True)
            if text and text.lower() not in {"books", "ebooks", "categories"}:
                genres.append(text)
        return self.unique_non_empty(genres)

    def _extract_cover_urls_html(self, soup: BeautifulSoup) -> list[str]:
        urls: list[str] = []
        for img in soup.select("img"):
            src = str(img.get("src") or img.get("data-src") or "")
            alt = str(img.get("alt") or "").lower()
            if not src.startswith("http"):
                continue
            if "book cover" in alt or "bookbub/image" in src or "pro_pbid_" in src:
                urls.append(src)
        if not urls:
            for meta in soup.select('meta[property="og:image"], meta[name="og:image"]'):
                content = str(meta.get("content") or "").strip()
                if content.startswith("http"):
                    urls.append(content)
        return self.unique_non_empty(urls)[:3]

    def _extract_reviews_html(self, soup: BeautifulSoup) -> list[str]:
        reviews: list[str] = []
        for node in soup.select("[itemprop='review'], [class*='review'], blockquote"):
            text = node.get_text("\n", strip=True)
            if text and len(text) > 40 and "cookie" not in text.lower():
                reviews.append(text)
        return self.unique_non_empty(reviews)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _looks_like_block(html: str) -> bool:
        lowered = html.lower()
        return (
            "just a moment..." in lowered
            or "cf-browser-verification" in lowered
            or "challenges.cloudflare.com" in lowered
            or (len(html) < 2000 and "cloudflare" in lowered)
        )

    @staticmethod
    def _is_not_found(html: str) -> bool:
        lowered = html.lower()
        return "page not found" in lowered or "<h1>404</h1>" in lowered

    @staticmethod
    def _extract_final_url_marker(text: str) -> str:
        # Allow hyphens in paths (BookBub slugs are hyphenated).
        match = re.search(r"BOOKBUB_FINAL_URL:(https://[^\s\"'<>]+)", text)
        return match.group(1).rstrip(".,;)") if match else ""

    @staticmethod
    def _canonical_url(soup: BeautifulSoup) -> str:
        link = soup.find("link", rel="canonical")
        if isinstance(link, Tag) and link.get("href"):
            return str(link.get("href"))
        return ""

    @staticmethod
    def _strip_html(value: str) -> str:
        text = re.sub(r"<[^>]+>", " ", value)
        text = unescape(text)
        return " ".join(text.split())

    @staticmethod
    def _as_list(value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        return [value]
