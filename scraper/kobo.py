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
from utils.title_match import (
    ambiguous_match_detail,
    classify_title_match,
    note_title_match,
)


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
            # Keep storefront list short — extra locales mostly add delay.
            urls.extend(
                [
                    f"https://www.kobo.com/us/en/search?query={encoded}",
                    f"https://www.kobo.com/ww/en/search?query={encoded}",
                ]
            )
        return urls

    def scrape(
        self,
        isbn13: str,
        *,
        hint_title: str = "",
        hint_authors: str = "",
        hint_titles: list[str] | None = None,
        allow_author_query: bool = False,
        allow_title_search: bool = False,
    ) -> ScrapedBook:
        """
        Scrape Kobo: ISBN first; title search only when allowed
        (Goodreads + Amazon confirmed). Title query never includes author.
        """
        from utils.title_match import build_title_query_variants

        result = ScrapedBook(source=self.source_name, isbn13=isbn13)
        result.fields = self._empty_fields(isbn13)

        hit = self._try_search_urls(
            isbn13,
            self.build_candidate_urls(isbn13),
            require_isbn_match=True,
        )
        if hit is not None:
            return hit

        if not allow_title_search:
            result.error = (
                f"Kobo: ISBN search failed for {isbn13}; "
                "title search skipped (needs Goodreads+Amazon confirm)."
            )
            return result

        titles = list(hint_titles or [])
        if hint_title and hint_title not in titles:
            titles = build_title_query_variants(hint_title, *titles)
        titles = titles or build_title_query_variants(hint_title)
        primary = titles[0] if titles else hint_title

        title_urls = self._build_title_search_urls(
            titles,
            authors=hint_authors if allow_author_query else "",
        )
        ambiguous_notes: list[str] = []
        if title_urls:
            hit, ambiguous_notes = self._try_search_urls(
                isbn13,
                title_urls,
                require_isbn_match=False,
                hint_title=primary,
                hint_authors=hint_authors,
                collect_ambiguous=True,
            )
            if hit is not None:
                return hit
            if ambiguous_notes:
                result.error = (
                    f"Kobo: AMBIGUOUS_TITLE_MATCH for ISBN {isbn13}. "
                    + " | ".join(ambiguous_notes[:3])
                )
                return result

        result.error = f"Kobo: could not fetch book data for ISBN {isbn13}."
        return result

    def _build_title_search_urls(
        self,
        titles: list[str] | str,
        *,
        authors: str = "",
    ) -> list[str]:
        """Build Kobo search URLs from title variants (+ optional author)."""
        from utils.title_match import clean_hint_title

        if isinstance(titles, str):
            titles = [titles]
        authors = (authors or "").strip()
        if authors == config.MISSING_VALUE:
            authors = ""
        primary_author = authors.split(",")[0].strip() if authors else ""

        urls: list[str] = []
        queries: list[str] = []
        for title in titles:
            title = clean_hint_title(title) or (title or "").strip()
            if not title or title == config.MISSING_VALUE:
                continue
            queries.append(title)
            if primary_author:
                queries.append(f"{title} {primary_author}")
        for query in self.unique_non_empty(queries):
            encoded = quote(query)
            urls.extend(
                [
                    f"https://www.kobo.com/us/en/search?query={encoded}",
                    f"https://www.kobo.com/ww/en/search?query={encoded}",
                ]
            )
        return self.unique_non_empty(urls)

    def _try_search_urls(
        self,
        isbn13: str,
        urls: list[str],
        *,
        require_isbn_match: bool,
        hint_title: str = "",
        hint_authors: str = "",
        collect_ambiguous: bool = False,
    ):
        """
        Try a list of Kobo search/product URLs; return first useful parse.

        When collect_ambiguous=True, returns (ScrapedBook|None, list[str]).
        Otherwise returns ScrapedBook|None (legacy callers).
        """
        ambiguous_notes: list[str] = []
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

                product_urls: list[str] = []
                matched = self._find_matching_product_url(soup, isbn13)
                if matched:
                    product_urls.append(matched)
                if not require_isbn_match:
                    product_urls.extend(self._find_product_urls(soup, limit=5))
                product_urls = self.unique_non_empty(product_urls)

                candidates: list[tuple[BeautifulSoup, ScrapedBook, str]] = [
                    (soup, parsed, url)
                ]
                for product_url in product_urls:
                    if (
                        self.is_parse_useful(parsed)
                        and (
                            not require_isbn_match
                            or self._structured_isbn_match(soup, isbn13)
                        )
                    ):
                        break
                    self.polite_delay()
                    product_html = fetcher(product_url)
                    if not product_html or self._looks_like_challenge(product_html):
                        continue
                    product_soup = self.make_soup(product_html)
                    product_parsed = self.parse_book_page(
                        product_soup,
                        page_url=product_url,
                        isbn13=isbn13,
                    )
                    candidates.append((product_soup, product_parsed, product_url))

                for cand_soup, cand_parsed, _cand_url in candidates:
                    if not self.is_parse_useful(cand_parsed):
                        continue
                    if require_isbn_match and not self._structured_isbn_match(
                        cand_soup, isbn13
                    ):
                        continue
                    if not require_isbn_match:
                        decision = classify_title_match(
                            hint_title=hint_title,
                            hint_authors=hint_authors,
                            found_title=str(cand_parsed.fields.get("title", "")),
                            found_authors=str(cand_parsed.fields.get("authors", "")),
                        )
                        if decision == "ambiguous":
                            ambiguous_notes.append(
                                ambiguous_match_detail(
                                    hint_title=hint_title,
                                    found_title=str(
                                        cand_parsed.fields.get("title", "")
                                    ),
                                )
                            )
                            continue
                        if decision != "accept":
                            continue
                    cand_parsed.method_used = method
                    cand_parsed.success = True
                    if not require_isbn_match:
                        note_title_match(cand_parsed.fields)
                    enriched = self._enrich_reviews_if_needed(cand_parsed)
                    if collect_ambiguous:
                        return enriched, ambiguous_notes
                    return enriched
        if collect_ambiguous:
            return None, ambiguous_notes
        return None

    @staticmethod
    def _store_code(page_props: dict[str, Any]) -> str:
        """
        Kobo pageProps.storeFront is sometimes a string ('us') and sometimes
        a dict like {'country': 'us', ...}. Always return a 2-letter code.
        """
        store = page_props.get("storeFront") or "us"
        if isinstance(store, dict):
            country = str(store.get("country") or "us").strip().lower()
            return country or "us"
        text = str(store).strip().lower()
        return text if text and "{" not in text else "us"

    def _find_product_urls(self, soup: BeautifulSoup, limit: int = 5) -> list[str]:
        """Collect ebook product links from a Kobo search page."""
        urls: list[str] = []
        script = soup.find("script", id="__NEXT_DATA__")
        if isinstance(script, Tag) and script.string:
            try:
                data = json.loads(script.string)
                page_props = data.get("props", {}).get("pageProps", {})
                items = (page_props.get("searchResultSSR") or {}).get("Items") or []
                store = self._store_code(page_props)
                for item in items:
                    book = item.get("Book") if isinstance(item, dict) else None
                    if not isinstance(book, dict):
                        continue
                    slug = book.get("Slug")
                    if slug:
                        urls.append(f"https://www.kobo.com/{store}/en/ebook/{slug}")
                    if len(urls) >= limit:
                        return urls
            except json.JSONDecodeError:
                pass

        for anchor in soup.select('a[href*="/ebook/"]'):
            href = str(anchor.get("href") or "")
            if "/ebook/" not in href or "search" in href:
                continue
            if href.startswith("http"):
                urls.append(href.split("?")[0])
            else:
                urls.append("https://www.kobo.com" + href.split("?")[0])
            if len(urls) >= limit:
                break
        return self.unique_non_empty(urls)

    def _find_first_product_url(self, soup: BeautifulSoup) -> str:
        """First ebook product link from search HTML (title fallback)."""
        urls = self._find_product_urls(soup, limit=1)
        return urls[0] if urls else ""

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
        Uses the shared Playwright browser.
        """
        try:
            from scraper.browser_pool import shared_page
        except ImportError:
            return None

        try:
            with shared_page(
                user_agent=self.DEFAULT_HEADERS["User-Agent"],
                locale="en-US",
            ) as page:
                page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=config.PLAYWRIGHT_NAV_TIMEOUT_MS,
                )
                # Kobo search often redirects to /ebook/... after hydration.
                page.wait_for_timeout(1500)
                html = page.content()
                final_url = page.url
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

        # 4) "eBook Details" panel from Inspect Element, including Book ID.
        # Example:
        #   Book ID: 9781101634615
        #   Release Date: June 26, 2014
        #   Language: English
        #   Imprint / publisher links
        detail_fields = self._extract_ebook_details_html(soup)
        for key, value in detail_fields.items():
            if key.startswith("_"):
                continue
            if key in result.fields and value:
                current = result.fields.get(key)
                if current in {config.MISSING_VALUE, "", None}:
                    result.fields[key] = self.text_or_na(value)

        if result.fields.get("genres") in {config.MISSING_VALUE, None, ""}:
            genres = self._extract_genres_html(soup)
            if genres:
                result.fields["genres"] = ", ".join(genres)

        # Always try visible/meta ratings (JSON-LD often omits them on ebook pages).
        rating, count = self._extract_rating_html(soup)
        if rating and result.fields.get("rating") in {config.MISSING_VALUE, None, ""}:
            result.fields["rating"] = rating
        if count and result.fields.get("ratings_count") in {config.MISSING_VALUE, None, ""}:
            result.fields["ratings_count"] = count

        if not result.cover_urls:
            result.cover_urls = self._extract_cover_urls_html(soup)

        if not result.reviews:
            result.reviews = self._extract_reviews_html(soup)

        result.fields["isbn13"] = isbn13
        result.fields["source"] = self.source_name
        if result.blurb and result.fields.get("description") == config.MISSING_VALUE:
            result.fields["description"] = self.text_or_na(result.blurb)
        return result

    def _extract_ebook_details_html(self, soup: BeautifulSoup) -> dict[str, str]:
        """
        Parse Kobo 'eBook Details' list items.

        Kobo labels the ISBN as Book ID on the product page (not always as ISBN).
        That is the key mapping for PL Assignment ISBN matching on Kobo.
        """
        fields: dict[str, str] = {}
        # Heading "eBook Details" then following list items.
        heading = None
        for node in soup.find_all(["h2", "h3"]):
            if "ebook details" in node.get_text(" ", strip=True).lower():
                heading = node
                break

        list_items: list[Tag] = []
        if heading is not None:
            sibling = heading.find_next(["ul", "ol", "div"])
            if isinstance(sibling, Tag):
                list_items = [li for li in sibling.find_all("li") if isinstance(li, Tag)]

        if not list_items:
            # Fallback: any list item that mentions Book ID / Release Date.
            for li in soup.find_all("li"):
                text = li.get_text(" ", strip=True)
                if re.search(r"Book ID\s*:", text, flags=re.I) or re.search(
                    r"Release Date\s*:", text, flags=re.I
                ):
                    list_items.append(li)

        for li in list_items:
            text = " ".join(li.get_text(" ", strip=True).split())
            # Label: Value rows
            match = re.match(r"^\s*([^:]+)\s*:\s*(.+)\s*$", text)
            if match:
                label = match.group(1).strip().lower()
                value = match.group(2).strip()
                if label == "book id":
                    # Map Book ID -> ISBN-13 for matching / diagnostics.
                    fields["_page_isbn"] = re.sub(r"[^0-9Xx]", "", value)
                elif label == "release date":
                    fields["publication_date"] = value
                elif label == "language":
                    fields["language"] = value
                elif label == "imprint":
                    fields.setdefault("publisher", value)
                elif label in {"page count", "pages", "number of pages", "print length"}:
                    pages_match = re.search(r"(\d+)", value)
                    if pages_match:
                        fields["pages"] = pages_match.group(1)
                continue

            # Publisher sometimes appears as a bare linked list item
            # (e.g. "Penguin Publishing Group") near Book ID rows.
            if (
                "publisher" not in fields
                and len(text) <= 80
                and not re.search(r"book id|release date|language|download|file size", text, re.I)
                and li.find("a")
            ):
                fields["publisher"] = text

        # Also accept Book ID anywhere in page text as a safety net.
        if "_page_isbn" not in fields:
            page_text = soup.get_text("\n", strip=True)
            book_id = re.search(r"Book ID\s*:\s*([0-9Xx\-]{10,17})", page_text, flags=re.I)
            if book_id:
                fields["_page_isbn"] = re.sub(r"[^0-9Xx]", "", book_id.group(1))

        return fields

    def _extract_genres_html(self, soup: BeautifulSoup) -> list[str]:
        """
        Genres on Kobo appear as category breadcrumbs / rank lines, e.g.
        Fiction & Literature, Thrillers, Literary.
        """
        genres: list[str] = []
        # Prefer product-local ranking / breadcrumb widgets (avoid site nav).
        for anchor in soup.select(
            ".category-rankings a, "
            "[class*='category-ranking'] a, "
            "[data-testid*='categor'] a, "
            "ol.breadcrumb a, "
            "nav.breadcrumb a"
        ):
            text = anchor.get_text(" ", strip=True)
            if not text:
                continue
            lowered = text.lower()
            if lowered in {"home", "ebooks", "audiobooks", "kobo", "store"}:
                continue
            cleaned = re.sub(r"^#?\d+\s*in\s*", "", text, flags=re.I).strip(" ,")
            parts = [p.strip() for p in cleaned.split(",") if p.strip()]
            genres.extend(parts if parts else [text])
        if genres:
            return self.unique_non_empty(genres)[:8]

        # Fallback: only /ebooks/category-style links with short labels.
        for anchor in soup.select('a[href*="/ebook/"][href*="category"], a[href*="/ebooks/"]'):
            text = anchor.get_text(" ", strip=True)
            if not text or len(text) > 48:
                continue
            lowered = text.lower()
            if lowered in {"home", "ebooks", "audiobooks", "kobo", "see all"}:
                continue
            genres.append(text)
        return self.unique_non_empty(genres)[:8]

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
        store = self._store_code(page_props)
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

    def _extract_rating_html(self, soup: BeautifulSoup) -> tuple[str, str]:
        """
        Pull rating + count from Kobo product meta / star widgets.

        Inspect Element examples:
          <meta property="og:rating" content="4.36">
          <meta property="og:rating_count" content="445">
          <div class="kobo star-rating ..." aria-label="Rated 4.5 out of 5 stars">
        """
        rating = ""
        count = ""
        for meta_sel, target in (
            ('meta[property="og:rating"]', "rating"),
            ('meta[name="og:rating"]', "rating"),
            ('meta[property="og:rating_count"]', "count"),
            ('meta[name="og:rating_count"]', "count"),
            ('meta[itemprop="ratingValue"]', "rating"),
            ('meta[itemprop="ratingCount"]', "count"),
        ):
            meta = soup.select_one(meta_sel)
            if not isinstance(meta, Tag) or not meta.get("content"):
                continue
            content = str(meta.get("content")).strip()
            if target == "rating" and not rating:
                try:
                    rating = f"{float(content):.2f}"
                except (TypeError, ValueError):
                    rating = content
            elif target == "count" and not count:
                count = re.sub(r"[^\d]", "", content)

        if not rating:
            star = soup.select_one(
                ".rating-star-container [aria-label], "
                ".star-rating[aria-label], "
                ".kobo.star-rating[aria-label], "
                "[class*='star-rating'][aria-label]"
            )
            if isinstance(star, Tag):
                label = str(star.get("aria-label") or "")
                match = re.search(r"([\d.]+)\s*out of", label, flags=re.I)
                if match:
                    rating = match.group(1)

        # Visible "4.3 (445 ratings)" style text near the title.
        if not count or not rating:
            page_text = soup.get_text(" ", strip=True)
            combo = re.search(
                r"(\d+(?:\.\d+)?)\s*(?:out of\s*5)?\s*\(?\s*([\d,]+)\s*(?:ratings?|reviews?)",
                page_text,
                flags=re.I,
            )
            if combo:
                if not rating:
                    rating = combo.group(1)
                if not count:
                    count = combo.group(2).replace(",", "")

        return (
            self.text_or_na(rating) if rating else "",
            self.text_or_na(count) if count else "",
        )

    def _extract_title_html(self, soup: BeautifulSoup) -> str:
        # Inspect Element (Kobo product): h1.title is the canonical title node.
        for selector in [
            "h1.title",
            "h1.item-title",
            ".item-info h1",
            "h1",
            ".item-title",
            "[data-testid='title']",
        ]:
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
        if not urls:
            for meta in soup.select('meta[property="og:image"], meta[name="og:image"]'):
                content = str(meta.get("content") or "").strip()
                if content.startswith("http"):
                    urls.append(content)
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
        Return True when this ISBN matches Kobo's Book ID / structured ISBN.

        Important (professor note):
            On Kobo product pages the ISBN is labeled **Book ID**, e.g.
            Book ID: 9781101634615
        """
        details = self._extract_ebook_details_html(soup)
        page_isbn = details.get("_page_isbn", "")
        if page_isbn and page_isbn.upper() == isbn13.upper():
            return True

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
