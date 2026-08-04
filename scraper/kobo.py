"""
scraper/kobo.py

Finds a book on Kobo by ISBN.
Kobo often blocks plain requests, and paperback ISBNs may not match
ebook listings — we try ISBN first, then title/author from Amazon/Goodreads.
"""

from __future__ import annotations

import json
import re
from html import unescape
from typing import Any, Optional
from urllib.parse import quote

from bs4 import BeautifulSoup, Tag

import config
from scraper.base import BaseScraper, ScrapedBook
from utils.isbn import isbn13_to_isbn10


class KoboScraper(BaseScraper):
    """Kobo-specific scraper (Module 5)."""

    source_name = "Kobo"

    def build_candidate_urls(self, isbn13: str) -> list[str]:
        """Build Kobo storefront search URLs for one ISBN (+ ISBN-10 if possible)."""
        queries = [isbn13]
        isbn10 = isbn13_to_isbn10(isbn13)
        if isbn10:
            queries.append(isbn10)
        urls: list[str] = []
        for query in queries:
            encoded = quote(query)
            urls.extend(
                [
                    f"https://www.kobo.com/us/en/search?query={encoded}",
                    f"https://www.kobo.com/in/en/search?query={encoded}",
                    f"https://www.kobo.com/ww/en/search?query={encoded}",
                    f"https://www.kobo.com/ca/en/search?query={encoded}",
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
        Scrape Kobo: exact ISBN first, then title/author search fallback.

        Paperback ISBNs often are not in Kobo's ebook catalog, so after ISBN
        search fails we search by title (from Amazon/Goodreads) and take the
        best ebook hit.
        """
        result = ScrapedBook(source=self.source_name, isbn13=isbn13)
        result.fields = self._empty_fields(isbn13)

        # Pass 1: ISBN / ISBN-10 exact match
        hit = self._try_search_urls(
            isbn13,
            self.build_candidate_urls(isbn13),
            require_isbn_match=True,
        )
        if hit is not None:
            return hit

        # Pass 2: title (+ author) when Amazon/Goodreads already found the book
        title_urls = self._build_title_search_urls(hint_title, hint_authors)
        if title_urls:
            hit = self._try_search_urls(
                isbn13,
                title_urls,
                require_isbn_match=False,
            )
            if hit is not None:
                return hit

        result.error = (
            f"Kobo: no catalog match for ISBN {isbn13} "
            f"(requests+bs4 / Playwright). "
            f"Kobo often lists a different ebook ISBN for the same title."
        )
        return result

    def _build_title_search_urls(self, title: str, authors: str) -> list[str]:
        title = (title or "").strip()
        if not title or title == config.MISSING_VALUE:
            return []
        authors = (authors or "").strip()
        if authors == config.MISSING_VALUE:
            authors = ""
        query = f"{title} {authors}".strip()
        encoded = quote(query)
        return [
            f"https://www.kobo.com/us/en/search?query={encoded}",
            f"https://www.kobo.com/ww/en/search?query={encoded}",
        ]

    def _try_search_urls(
        self,
        isbn13: str,
        urls: list[str],
        *,
        require_isbn_match: bool,
    ) -> Optional[ScrapedBook]:
        """Try a list of Kobo search/product URLs; return first useful parse."""
        for url in urls:
            for method, fetcher in (
                ("requests+bs4", self.fetch_html_requests),
                ("playwright", self.fetch_html_playwright),
            ):
                self.polite_delay()
                html = fetcher(url)
                if not html or self._looks_like_challenge(html):
                    continue

                soup = self.make_soup(html)
                parsed = self.parse_book_page(soup, page_url=url, isbn13=isbn13)

                product_url = self._find_matching_product_url(soup, isbn13)
                if not product_url and not require_isbn_match:
                    product_url = self._find_first_product_url(soup)

                if (
                    not self.is_parse_useful(parsed)
                    or (require_isbn_match and not self._structured_isbn_match(soup, isbn13))
                ) and product_url:
                    self.polite_delay()
                    product_html = fetcher(product_url)
                    if product_html and not self._looks_like_challenge(product_html):
                        soup = self.make_soup(product_html)
                        parsed = self.parse_book_page(
                            soup,
                            page_url=product_url,
                            isbn13=isbn13,
                        )

                if not self.is_parse_useful(parsed):
                    continue

                if require_isbn_match and not self._structured_isbn_match(soup, isbn13):
                    continue

                parsed.method_used = method
                parsed.success = True
                if not require_isbn_match:
                    # Title-fallback hit: ebook ISBN may differ from print ISBN.
                    edition = str(parsed.fields.get("edition", config.MISSING_VALUE))
                    note = "matched by title (ebook ISBN may differ)"
                    if edition in {config.MISSING_VALUE, "", None}:
                        parsed.fields["edition"] = note
                    elif note not in edition:
                        parsed.fields["edition"] = f"{edition} | {note}"
                return self._enrich_reviews_if_needed(parsed)
        return None

    def _find_first_product_url(self, soup: BeautifulSoup) -> str:
        """First ebook product link from search HTML (title fallback)."""
        script = soup.find("script", id="__NEXT_DATA__")
        if isinstance(script, Tag) and script.string:
            try:
                data = json.loads(script.string)
                page_props = data.get("props", {}).get("pageProps", {})
                items = (page_props.get("searchResultSSR") or {}).get("Items") or []
                store = page_props.get("storeFront") or "us"
                for item in items:
                    book = item.get("Book") if isinstance(item, dict) else None
                    if not isinstance(book, dict):
                        continue
                    slug = book.get("Slug")
                    if slug:
                        return f"https://www.kobo.com/{store}/en/ebook/{slug}"
            except json.JSONDecodeError:
                pass

        for anchor in soup.select('a[href*="/ebook/"]'):
            href = str(anchor.get("href") or "")
            if "/ebook/" in href and "search" not in href:
                if href.startswith("http"):
                    return href.split("?")[0]
                return "https://www.kobo.com" + href.split("?")[0]
        return ""

    def is_parse_useful(self, parsed: ScrapedBook) -> bool:
        """Reject Kobo chrome/search shell pages that are not real books."""
        title = str(parsed.fields.get("title", "")).strip().lower()
        if not title or title == config.MISSING_VALUE.lower():
            return False
        banned = {
            "rakuten kobo",
            "kobo",
            "search results",
            "ebooks and audiobooks search results",
        }
        if title in banned or "search results" in title:
            return False
        return True

    def fetch_html_requests(self, url: str) -> Optional[str]:
        """Level-1 fetch; treat Kobo challenge pages as failure."""
        html = super().fetch_html_requests(url)
        if html and self._looks_like_challenge(html):
            return None
        return html

    def fetch_html_playwright(self, url: str) -> Optional[str]:
        """
        Level-2 fetch with a slightly longer wait for Kobo's JS storefront.
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
                # Kobo search often redirects to /ebook/... after hydration.
                page.wait_for_timeout(4500)
                html = page.content()
                final_url = page.url
                context.close()
                browser.close()
                if html and len(html) > 500 and not self._looks_like_challenge(html):
                    # Preserve redirected product URL for the parser.
                    return f"<!-- KOBO_FINAL_URL:{final_url} -->\n" + html
        except Exception:  # noqa: BLE001
            return None
        return None

    def parse_book_page(
        self,
        soup: BeautifulSoup,
        page_url: str,
        isbn13: str,
    ) -> ScrapedBook:
        """Parse a Kobo search or product page into ScrapedBook."""
        result = ScrapedBook(source=self.source_name, isbn13=isbn13)
        result.fields = self._empty_fields(isbn13)

        final_url = self._extract_final_url_marker(str(soup)[:800]) or self._canonical_url(soup) or page_url
        result.fields["url"] = self.text_or_na(final_url)

        # 1) JSON-LD Book / Product (best quality on product pages)
        ld_fields, covers, reviews, blurb = self._extract_from_json_ld(soup)
        for key, value in ld_fields.items():
            if key in result.fields and value:
                result.fields[key] = self.text_or_na(value)
        result.cover_urls = covers
        result.reviews = reviews
        result.blurb = blurb
        if blurb and result.fields.get("description") in {config.MISSING_VALUE, "", None}:
            result.fields["description"] = self.text_or_na(blurb)

        # 2) __NEXT_DATA__ search hit with exact ISBN
        if result.fields.get("title") in {config.MISSING_VALUE, None, ""}:
            next_fields, next_covers, next_blurb, product_url = self._extract_from_next_data(
                soup, isbn13
            )
            for key, value in next_fields.items():
                if key in result.fields and value:
                    result.fields[key] = self.text_or_na(value)
            if next_covers:
                result.cover_urls = self.unique_non_empty(result.cover_urls + next_covers)
            if next_blurb:
                result.blurb = next_blurb
                result.fields["description"] = self.text_or_na(next_blurb)
            if product_url:
                result.fields["url"] = product_url

        # 3) Visible HTML fallback
        if result.fields.get("title") in {config.MISSING_VALUE, None, ""}:
            result.fields["title"] = self._extract_title_html(soup)

        if result.fields.get("authors") in {config.MISSING_VALUE, None, ""}:
            result.fields["authors"] = self._extract_authors_html(soup)

        if not result.cover_urls:
            result.cover_urls = self._extract_cover_urls_html(soup)

        if not result.reviews:
            result.reviews = self._extract_reviews_html(soup)

        result.fields["isbn13"] = isbn13
        result.fields["source"] = self.source_name
        if result.blurb and result.fields.get("description") == config.MISSING_VALUE:
            result.fields["description"] = self.text_or_na(result.blurb)
        return result

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
                type_name = item.get("@type")
                types = set(self._as_list(type_name))

                if "Book" in types:
                    fields["title"] = str(item.get("name") or fields.get("title") or "")
                    authors = item.get("author")
                    names: list[str] = []
                    for author in self._as_list(authors):
                        if isinstance(author, dict) and author.get("name"):
                            names.append(str(author["name"]))
                        elif isinstance(author, str):
                            names.append(author)
                    if names:
                        fields["authors"] = ", ".join(self.unique_non_empty(names))
                    genres = item.get("genre")
                    if genres:
                        fields["genres"] = ", ".join(
                            self.unique_non_empty([str(g) for g in self._as_list(genres)])
                        )
                    if item.get("inLanguage"):
                        fields["language"] = str(item["inLanguage"])
                    work = item.get("workExample")
                    if isinstance(work, dict):
                        if work.get("isbn"):
                            fields["_page_isbn"] = re.sub(r"[^0-9Xx]", "", str(work["isbn"]))
                        if work.get("bookFormat"):
                            fields["format"] = str(work["bookFormat"]).split("/")[-1]
                        offer = (
                            (work.get("potentialAction") or {})
                            .get("expectsAcceptanceOf")
                            if isinstance(work.get("potentialAction"), dict)
                            else None
                        )
                        if isinstance(offer, dict) and offer.get("price") is not None:
                            currency = offer.get("priceCurrency", "")
                            fields["price"] = f"{offer.get('price')} {currency}".strip()

                if "Product" in types:
                    fields.setdefault("title", str(item.get("name") or ""))
                    if item.get("description"):
                        blurb = self._strip_html(str(item["description"]))
                        fields["description"] = blurb
                    if item.get("image"):
                        covers.extend([str(u) for u in self._as_list(item["image"])])
                    if item.get("gtin13"):
                        fields["_page_isbn"] = re.sub(r"[^0-9Xx]", "", str(item["gtin13"]))
                    elif item.get("sku"):
                        fields["_page_isbn"] = re.sub(r"[^0-9Xx]", "", str(item["sku"]))
                    if item.get("releasedate"):
                        fields["publication_date"] = str(item["releasedate"])[:10]
                    brand = item.get("brand")
                    if isinstance(brand, dict) and brand.get("name"):
                        fields["publisher"] = str(brand["name"])
                    offers = item.get("offers")
                    if isinstance(offers, dict) and offers.get("price") is not None:
                        currency = offers.get("priceCurrency", "")
                        fields["price"] = f"{offers.get('price')} {currency}".strip()
                        if offers.get("url"):
                            fields["url"] = str(offers["url"])
                    rating = item.get("aggregateRating")
                    if isinstance(rating, dict):
                        if rating.get("ratingValue") is not None:
                            fields["rating"] = str(rating["ratingValue"])
                        if rating.get("ratingCount") is not None:
                            fields["ratings_count"] = str(rating["ratingCount"])
                    for review in self._as_list(item.get("review")):
                        if isinstance(review, dict):
                            body = review.get("reviewBody") or review.get("description")
                            title = review.get("name")
                            chunks = [str(title or "").strip(), str(body or "").strip()]
                            text = "\n".join(c for c in chunks if c)
                            if text:
                                reviews.append(text)

        return fields, self.unique_non_empty(covers), self.unique_non_empty(reviews), blurb

    def _extract_from_next_data(
        self, soup: BeautifulSoup, isbn13: str
    ) -> tuple[dict[str, str], list[str], str, str]:
        fields: dict[str, str] = {}
        covers: list[str] = []
        blurb = ""
        product_url = ""

        script = soup.find("script", id="__NEXT_DATA__")
        if not isinstance(script, Tag) or not script.string:
            return fields, covers, blurb, product_url

        try:
            data = json.loads(script.string)
        except json.JSONDecodeError:
            return fields, covers, blurb, product_url

        page_props = data.get("props", {}).get("pageProps", {})
        items = (page_props.get("searchResultSSR") or {}).get("Items") or []
        matched_book: Optional[dict[str, Any]] = None
        for item in items:
            book = item.get("Book") if isinstance(item, dict) else None
            if not isinstance(book, dict):
                continue
            book_isbn = re.sub(r"[^0-9Xx]", "", str(book.get("ISBN") or ""))
            if book_isbn == isbn13:
                matched_book = book
                break

        if matched_book is None:
            return fields, covers, blurb, product_url

        fields["title"] = str(matched_book.get("Title") or "")
        fields["subtitle"] = str(matched_book.get("Subtitle") or "") or config.MISSING_VALUE
        fields["authors"] = str(matched_book.get("Contributors") or "")
        roles = matched_book.get("ContributorRoles") or []
        if isinstance(roles, list) and roles:
            names = [
                str(role.get("Name"))
                for role in roles
                if isinstance(role, dict) and role.get("Name")
            ]
            if names:
                fields["authors"] = ", ".join(names)
        fields["publisher"] = str(matched_book.get("PublisherName") or "")
        fields["language"] = str(matched_book.get("Language") or "")
        pub = matched_book.get("PublicationDate")
        if pub:
            fields["publication_date"] = str(pub)[:10]
        if matched_book.get("Rating") is not None:
            fields["rating"] = str(matched_book.get("Rating"))
        if matched_book.get("TotalRating") is not None:
            fields["ratings_count"] = str(matched_book.get("TotalRating"))
        price = matched_book.get("Price")
        if isinstance(price, dict) and price.get("Price") is not None:
            fields["price"] = f"{price.get('Price')} {price.get('Currency', '')}".strip()
        desc = matched_book.get("Description")
        if desc:
            blurb = self._strip_html(str(desc))
            fields["description"] = blurb
        slug = matched_book.get("Slug")
        store = page_props.get("storeFront") or "us"
        if slug:
            product_url = f"https://www.kobo.com/{store}/en/ebook/{slug}"
            fields["url"] = product_url
        image_id = matched_book.get("ImageId")
        if image_id and slug:
            covers.append(
                f"https://cdn.kobo.com/book-images/{image_id}/180/1000/False/{slug}.jpg"
            )
        fields["format"] = "EBook"
        return fields, covers, blurb, product_url

    def _find_matching_product_url(self, soup: BeautifulSoup, isbn13: str) -> str:
        _, _, _, product_url = self._extract_from_next_data(soup, isbn13)
        if product_url:
            return product_url

        # Already on a product page?
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                payload = json.loads(script.string or "")
            except (TypeError, json.JSONDecodeError):
                continue
            for item in self._as_list(payload):
                if isinstance(item, dict) and item.get("@type") == "Product":
                    offers = item.get("offers")
                    if isinstance(offers, dict) and offers.get("url"):
                        gtin = re.sub(r"[^0-9Xx]", "", str(item.get("gtin13") or item.get("sku") or ""))
                        if gtin == isbn13:
                            return str(offers["url"])
        return ""

    def _extract_title_html(self, soup: BeautifulSoup) -> str:
        for selector in ["h1", ".item-title", "[data-testid='title']"]:
            node = soup.select_one(selector)
            if node:
                text = node.get_text(" ", strip=True)
                if text:
                    return self.text_or_na(text)
        return config.MISSING_VALUE

    def _extract_authors_html(self, soup: BeautifulSoup) -> str:
        authors: list[str] = []
        for selector in [
            "a.contributor-name",
            ".contributor-name",
            "a[href*='/author/']",
            "[data-testid='author']",
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
            src = str(img.get("src") or img.get("data-src") or "")
            alt = str(img.get("alt") or "").lower()
            if not src.startswith("http"):
                continue
            if "book-images" in src or "cover" in alt or "cdn.kobo.com" in src:
                urls.append(src)
        return self.unique_non_empty(urls)[:3]

    def _extract_reviews_html(self, soup: BeautifulSoup) -> list[str]:
        reviews: list[str] = []
        selectors = [
            "[data-testid='review-body']",
            ".review-body",
            ".customer-review",
            "div.review",
        ]
        for selector in selectors:
            for node in soup.select(selector):
                text = node.get_text("\n", strip=True)
                if text and len(text) > 20:
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

        # Reload product page via Playwright and re-parse reviews/JSON-LD.
        html = self.fetch_html_playwright(page_url)
        if not html:
            return parsed
        soup = self.make_soup(html)
        _, _, reviews, _ = self._extract_from_json_ld(soup)
        reviews = self.unique_non_empty(parsed.reviews + reviews + self._extract_reviews_html(soup))
        parsed.reviews = reviews[: max(config.MIN_REVIEWS_PER_SOURCE, len(reviews))]
        return parsed

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _looks_like_challenge(html: str) -> bool:
        lowered = html.lower()
        return (
            "challenged | kobo" in lowered
            or "challenges.cloudflare.com" in lowered
            or "cf-browser-verification" in lowered
        )

    def _structured_isbn_match(self, soup: BeautifulSoup, isbn13: str) -> bool:
        """
        Return True only when JSON-LD or __NEXT_DATA__ contains this ISBN.
        """
        # JSON-LD gtin13 / sku / workExample.isbn
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                payload = json.loads(script.string or "")
            except (TypeError, json.JSONDecodeError):
                continue
            for item in self._as_list(payload):
                if not isinstance(item, dict):
                    continue
                candidates = [
                    item.get("gtin13"),
                    item.get("sku"),
                    item.get("isbn"),
                ]
                work = item.get("workExample")
                if isinstance(work, dict):
                    candidates.append(work.get("isbn"))
                for candidate in candidates:
                    if candidate and re.sub(r"[^0-9Xx]", "", str(candidate)) == isbn13:
                        return True

        # __NEXT_DATA__ Book.ISBN exact match
        _, _, _, product_url = self._extract_from_next_data(soup, isbn13)
        return bool(product_url)

    @staticmethod
    def _extract_final_url_marker(text: str) -> str:
        """Read redirected URL marker injected by fetch_html_playwright()."""
        match = re.search(r"KOBO_FINAL_URL:(https://[^\s>-]+)", text)
        return match.group(1) if match else ""

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
