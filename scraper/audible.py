"""
scraper/audible.py

Searches Audible (.com / .in) by ISBN for audiobook pages.
Print ISBNs often return no results — we then try title/author search.
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


class AudibleScraper(BaseScraper):
    """Audible-specific scraper (Module 6)."""

    source_name = "Audible"

    def build_candidate_urls(self, isbn13: str) -> list[str]:
        """Build Audible search URLs for one ISBN (+ ISBN-10 if possible)."""
        queries = [isbn13]
        isbn10 = isbn13_to_isbn10(isbn13)
        if isbn10:
            queries.append(isbn10)
        urls: list[str] = []
        for query in queries:
            encoded = quote(query)
            urls.extend(
                [
                    (
                        "https://www.audible.com/search?"
                        f"keywords={encoded}&ipRedirectOverride=true&overrideBaseCountry=true"
                    ),
                    f"https://www.audible.in/search?keywords={encoded}",
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
        Search Audible by ISBN, then by title/author if the print ISBN misses.

        Soft-fail when neither ISBN nor title finds an audiobook listing.
        """
        result = ScrapedBook(source=self.source_name, isbn13=isbn13)
        result.fields = self._empty_fields(isbn13)

        product_urls = self._resolve_product_urls_from_searches(
            self.build_candidate_urls(isbn13)
        )
        used_title_fallback = False
        if not product_urls:
            title_urls = self._build_title_search_urls(hint_title, hint_authors)
            if title_urls:
                product_urls = self._resolve_product_urls_from_searches(title_urls)
                used_title_fallback = bool(product_urls)

        if not product_urls:
            result.error = (
                f"Audible: no audiobook catalog match for ISBN {isbn13}. "
                f"Audible often does not index print/paperback ISBNs."
            )
            return result

        for product_url in product_urls:
            for method, fetcher in (
                ("requests+bs4", self.fetch_html_requests),
                ("playwright", self.fetch_html_playwright),
            ):
                self.polite_delay()
                html = fetcher(product_url)
                if not html or self._looks_like_block(html):
                    continue
                soup = self.make_soup(html)
                parsed = self.parse_book_page(soup, page_url=product_url, isbn13=isbn13)
                if self.is_parse_useful(parsed):
                    parsed.method_used = method
                    parsed.success = True
                    if used_title_fallback:
                        edition = str(parsed.fields.get("edition", config.MISSING_VALUE))
                        note = "matched by title (audiobook ASIN may differ)"
                        if edition in {config.MISSING_VALUE, "", None}:
                            parsed.fields["edition"] = note
                        elif note not in edition:
                            parsed.fields["edition"] = f"{edition} | {note}"
                    return self._enrich_reviews_if_needed(parsed)

        result.error = (
            f"Audible: found product URL(s) for ISBN {isbn13} but could not "
            f"extract usable metadata with requests+bs4 or Playwright."
        )
        return result

    def _build_title_search_urls(self, title: str, authors: str) -> list[str]:
        title = (title or "").strip()
        if not title or title == config.MISSING_VALUE:
            return []
        authors = (authors or "").strip()
        if authors == config.MISSING_VALUE:
            authors = ""
        query = quote(f"{title} {authors}".strip())
        return [
            (
                "https://www.audible.com/search?"
                f"keywords={query}&ipRedirectOverride=true&overrideBaseCountry=true"
            ),
            f"https://www.audible.in/search?keywords={query}",
        ]

    def is_parse_useful(self, parsed: ScrapedBook) -> bool:
        """Reject Audible chrome / unavailable shells."""
        title = str(parsed.fields.get("title", "")).strip().lower()
        if not title or title == config.MISSING_VALUE.lower():
            return False
        banned_fragments = [
            "sorry, it looks like this title is no longer available",
            "no results",
            "download audio books from audible",
            "listen to audiobooks, podcasts",
        ]
        return not any(fragment in title for fragment in banned_fragments)

    def fetch_html_playwright(self, url: str) -> Optional[str]:
        """Level-2 fetch tuned for Audible's JS-heavy storefront."""
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
                page = context.new_page()
                page.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                )
                page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=config.HTTP_TIMEOUT_SECONDS * 1000,
                )
                page.wait_for_timeout(4000)
                html = page.content()
                final_url = page.url
                context.close()
                browser.close()
                if html and len(html) > 2000 and not self._looks_like_block(html):
                    return f"<!-- AUDIBLE_FINAL_URL:{final_url} -->\n" + html
        except Exception:  # noqa: BLE001
            return None
        return None

    def parse_book_page(
        self,
        soup: BeautifulSoup,
        page_url: str,
        isbn13: str,
    ) -> ScrapedBook:
        """Parse an Audible product page into ScrapedBook."""
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
        if not result.cover_urls:
            result.cover_urls = self._extract_cover_urls_html(soup)
        if not result.reviews:
            result.reviews = self._extract_reviews_html(soup)

        # Audible product is an audiobook edition.
        if result.fields.get("format") in {config.MISSING_VALUE, None, ""}:
            result.fields["format"] = "Audiobook"

        result.fields["isbn13"] = isbn13
        result.fields["source"] = self.source_name
        return result

    # ------------------------------------------------------------------
    # URL resolution
    # ------------------------------------------------------------------
    def _resolve_product_urls_from_searches(self, search_urls: list[str]) -> list[str]:
        """
        Search Audible storefronts and collect /pd/ product URLs.

        Only accepts links inside real search-result items. Homepage
        recommendation carousels are ignored (they caused false positives).
        """
        found: list[str] = []
        for search_url in search_urls:
            self.polite_delay()
            html = self.fetch_html_requests(search_url)
            final_url = search_url
            if not html or self._looks_like_block(html):
                html = self.fetch_html_playwright(search_url)
                if html:
                    final_url = self._extract_final_url_marker(html[:800]) or search_url

            if not html or self._looks_like_block(html):
                continue
            if self._is_no_results(html, final_url):
                continue

            soup = self.make_soup(html)
            for href in self._extract_pd_links(soup, base_url=final_url):
                found.append(href)
            if found:
                break
        return self.unique_non_empty(found)

    def _extract_pd_links(self, soup: BeautifulSoup, base_url: str) -> list[str]:
        """
        Extract Audible /pd/ links from SEARCH RESULT rows only.

        We intentionally do NOT scrape every a[href*='/pd/'] on the page,
        because no-result / homepage shells contain unrelated recommendations.
        """
        links: list[str] = []
        # Primary: classic search result list items
        anchors = soup.select("li.productListItem h3 a[href*='/pd/']")
        if not anchors:
            # Secondary: result heading links nested under product list region
            anchors = soup.select(
                "div.adbl-search-results li h3 a[href*='/pd/'], "
                "ul.bc-list li.productListItem a[href*='/pd/']"
            )

        for anchor in anchors:
            href = str(anchor.get("href") or "")
            if "/pd/" not in href:
                continue
            absolute = urljoin(base_url, href.split("?")[0])
            links.append(absolute)
        return links

    # ------------------------------------------------------------------
    # JSON-LD / HTML extractors
    # ------------------------------------------------------------------
    def _extract_from_json_ld(
        self, soup: BeautifulSoup
    ) -> tuple[dict[str, str], list[str], list[str], str]:
        fields: dict[str, str] = {}
        covers: list[str] = []
        reviews: list[str] = []
        blurb = ""
        genres: list[str] = []

        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                payload = json.loads(script.string or "")
            except (TypeError, json.JSONDecodeError):
                continue

            for item in self._as_list(payload):
                if not isinstance(item, dict):
                    continue
                types = {str(t) for t in self._as_list(item.get("@type"))}

                if "BreadcrumbList" in types:
                    for element in item.get("itemListElement") or []:
                        if not isinstance(element, dict):
                            continue
                        crumb = element.get("item")
                        if isinstance(crumb, dict):
                            name = str(crumb.get("name") or "").strip()
                            if name and name.lower() not in {"home", "audible"}:
                                genres.append(name)

                if types & {"Audiobook", "Book"}:
                    fields["title"] = str(item.get("name") or fields.get("title") or "")
                    authors = []
                    for author in self._as_list(item.get("author")):
                        if isinstance(author, dict) and author.get("name"):
                            authors.append(str(author["name"]))
                        elif isinstance(author, str):
                            authors.append(author)
                    if authors:
                        fields["authors"] = ", ".join(self.unique_non_empty(authors))

                    narrators = []
                    for narrator in self._as_list(item.get("readBy")):
                        if isinstance(narrator, dict) and narrator.get("name"):
                            narrators.append(str(narrator["name"]))
                    duration = item.get("duration")
                    format_bits = ["Audiobook"]
                    if duration:
                        format_bits.append(self._format_duration(str(duration)))
                    if narrators:
                        format_bits.append("Narrated by " + ", ".join(narrators))
                    fields["format"] = " | ".join(format_bits)

                    if item.get("publisher"):
                        fields["publisher"] = str(item["publisher"])
                    if item.get("datePublished"):
                        fields["publication_date"] = str(item["datePublished"])[:10]
                    if item.get("inLanguage"):
                        fields["language"] = str(item["inLanguage"])
                    if item.get("description"):
                        blurb = self._strip_html(str(item["description"]))
                        fields["description"] = blurb
                    if item.get("image"):
                        covers.extend([str(u) for u in self._as_list(item["image"])])
                    rating = item.get("aggregateRating")
                    if isinstance(rating, dict):
                        if rating.get("ratingValue") is not None:
                            # Keep a short readable rating.
                            try:
                                fields["rating"] = f"{float(rating['ratingValue']):.2f}"
                            except (TypeError, ValueError):
                                fields["rating"] = str(rating["ratingValue"])
                        if rating.get("ratingCount") is not None:
                            fields["ratings_count"] = str(rating["ratingCount"])
                    offers = item.get("offers")
                    if isinstance(offers, dict) and offers.get("price") is not None:
                        currency = offers.get("priceCurrency", "")
                        fields["price"] = f"{offers.get('price')} {currency}".strip()

                if "Product" in types:
                    fields.setdefault("title", str(item.get("name") or ""))
                    if item.get("image"):
                        covers.extend([str(u) for u in self._as_list(item["image"])])
                    brand = item.get("brand")
                    if isinstance(brand, str) and brand:
                        fields.setdefault("publisher", brand)
                    elif isinstance(brand, dict) and brand.get("name"):
                        fields.setdefault("publisher", str(brand["name"]))
                    rating = item.get("aggregateRating")
                    if isinstance(rating, dict):
                        if rating.get("ratingValue") is not None and "rating" not in fields:
                            try:
                                fields["rating"] = f"{float(rating['ratingValue']):.2f}"
                            except (TypeError, ValueError):
                                fields["rating"] = str(rating["ratingValue"])
                        if rating.get("ratingCount") is not None and "ratings_count" not in fields:
                            fields["ratings_count"] = str(rating["ratingCount"])
                    offers = item.get("offers")
                    if isinstance(offers, dict) and offers.get("price") is not None:
                        currency = offers.get("priceCurrency", "")
                        fields.setdefault(
                            "price",
                            f"{offers.get('price')} {currency}".strip(),
                        )
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

        if genres:
            fields["genres"] = ", ".join(self.unique_non_empty(genres))
        return fields, self.unique_non_empty(covers), self.unique_non_empty(reviews), blurb

    def _extract_title_html(self, soup: BeautifulSoup) -> str:
        for selector in ["h1.bc-heading", "h1", ".adbl-prod-h1-title"]:
            node = soup.select_one(selector)
            if node:
                text = node.get_text(" ", strip=True)
                if text:
                    return self.text_or_na(text)
        return config.MISSING_VALUE

    def _extract_authors_html(self, soup: BeautifulSoup) -> str:
        authors: list[str] = []
        # Common Audible author line patterns.
        for selector in [
            "li.authorLabel a",
            "li.bc-list-item.authorLabel a",
            "a[href*='/author/']",
        ]:
            for node in soup.select(selector):
                text = node.get_text(" ", strip=True)
                if text:
                    authors.append(text)
            if authors:
                break
        authors = self.unique_non_empty(authors)
        return ", ".join(authors) if authors else config.MISSING_VALUE

    def _extract_cover_urls_html(self, soup: BeautifulSoup) -> list[str]:
        urls: list[str] = []
        for img in soup.select("img"):
            src = str(img.get("src") or "")
            alt = str(img.get("alt") or "").lower()
            if not src.startswith("http"):
                continue
            if "cover" in alt or "/images/I/" in src:
                urls.append(src)
        return self.unique_non_empty(urls)[:3]

    def _extract_reviews_html(self, soup: BeautifulSoup) -> list[str]:
        reviews: list[str] = []
        selectors = [
            ".reviewText",
            ".bc-review-content",
            "div.USreviews0 p",
            "[class*='review'] p",
        ]
        for selector in selectors:
            for node in soup.select(selector):
                text = node.get_text("\n", strip=True)
                if text and len(text) > 30 and "get this deal" not in text.lower():
                    reviews.append(text)
            if reviews:
                break
        return self.unique_non_empty(reviews)

    def _enrich_reviews_if_needed(self, parsed: ScrapedBook) -> ScrapedBook:
        if len(parsed.reviews) >= config.MIN_REVIEWS_PER_SOURCE:
            return parsed
        page_url = str(parsed.fields.get("url", "")).strip()
        if not page_url.startswith("http"):
            return parsed

        # Reload and try to expose more review text.
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return parsed

        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"],
                )
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
                page.wait_for_timeout(2500)
                for _ in range(5):
                    page.mouse.wheel(0, 2500)
                    page.wait_for_timeout(600)
                try:
                    more = page.locator("text=Show more reviews")
                    if more.count() > 0:
                        more.first.click(timeout=2000)
                        page.wait_for_timeout(1500)
                except Exception:  # noqa: BLE001
                    pass
                html = page.content()
                context.close()
                browser.close()
            extra = self._extract_reviews_html(self.make_soup(html))
            merged = self.unique_non_empty(parsed.reviews + extra)
            parsed.reviews = merged[: max(config.MIN_REVIEWS_PER_SOURCE, len(merged))]
        except Exception:  # noqa: BLE001
            return parsed
        return parsed

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _looks_like_block(html: str) -> bool:
        if len(html) < 2000:
            return True
        lowered = html.lower()
        return (
            "challenges.cloudflare.com" in lowered
            or "api-services-support@amazon.com" in lowered
            or "enter the characters you see below" in lowered
        )

    @staticmethod
    def _is_no_results(html: str, url: str) -> bool:
        if "no-search-results" in url:
            return True
        lowered = html.lower()
        markers = [
            "no results",
            "we did not find any matches",
            "did not match any products",
            'no results\n for "',
            "no results for",
        ]
        return any(marker in lowered for marker in markers)

    @staticmethod
    def _extract_final_url_marker(text: str) -> str:
        match = re.search(r"AUDIBLE_FINAL_URL:(https://[^\s>-]+)", text)
        return match.group(1) if match else ""

    @staticmethod
    def _canonical_url(soup: BeautifulSoup) -> str:
        link = soup.find("link", rel="canonical")
        if isinstance(link, Tag) and link.get("href"):
            return str(link.get("href"))
        return ""

    @staticmethod
    def _format_duration(duration: str) -> str:
        """
        Convert ISO-8601 duration like PT10H1M into '10h 1m'.
        """
        match = re.fullmatch(
            r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?",
            duration.upper(),
        )
        if not match:
            return duration
        hours, minutes, seconds = match.groups()
        parts: list[str] = []
        if hours:
            parts.append(f"{int(hours)}h")
        if minutes:
            parts.append(f"{int(minutes)}m")
        if seconds and not parts:
            parts.append(f"{int(seconds)}s")
        return " ".join(parts) if parts else duration

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
