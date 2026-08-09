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
from utils.title_match import (
    ambiguous_match_detail,
    authors_roughly_match,
    classify_title_match,
    note_title_match,
    title_match_score,
)


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
        hint_titles: list[str] | None = None,
        allow_author_query: bool = False,
    ) -> ScrapedBook:
        """
        Search Audible by ISBN, then by Goodreads/Amazon title variants.

        When GR+Amazon confirm (allow_author_query=True), also search title+author.
        Author is always used for validation / ranking.
        """
        from utils.title_match import build_title_query_variants

        result = ScrapedBook(source=self.source_name, isbn13=isbn13)
        result.fields = self._empty_fields(isbn13)

        titles = list(hint_titles or [])
        if hint_title and hint_title not in titles:
            titles = build_title_query_variants(hint_title, *titles)
        titles = titles or build_title_query_variants(hint_title)
        primary = titles[0] if titles else hint_title

        # If another site already found title/authors, require a real title match
        # so Audible does not accept a loosely related classic (e.g. ward vs world).
        has_hints = bool(primary and primary != config.MISSING_VALUE)

        product_urls = self._resolve_product_urls_from_searches(
            self.build_candidate_urls(isbn13)
        )
        hit, ambiguous_notes = self._try_product_urls(
            isbn13,
            product_urls,
            require_hint_match=has_hints,
            hint_title=primary,
            hint_authors=hint_authors,
        )
        if hit is not None:
            return hit

        title_search = self._build_title_search_urls(
            titles,
            authors=hint_authors if allow_author_query else "",
        )
        if title_search:
            title_product_urls = self._resolve_product_urls_from_searches(title_search)
            hit, more_ambiguous = self._try_product_urls(
                isbn13,
                title_product_urls,
                require_hint_match=True,
                hint_title=primary,
                hint_authors=hint_authors,
                rank_best=True,
            )
            ambiguous_notes.extend(more_ambiguous)
            if hit is not None:
                note_title_match(hit.fields)
                return hit

        if ambiguous_notes:
            result.error = (
                f"Audible: AMBIGUOUS_TITLE_MATCH for ISBN {isbn13}. "
                + " | ".join(ambiguous_notes[:3])
            )
            return result

        result.error = f"Audible: could not fetch book data for ISBN {isbn13}."
        return result

    def _try_product_urls(
        self,
        isbn13: str,
        product_urls: list[str],
        *,
        require_hint_match: bool,
        hint_title: str,
        hint_authors: str,
        rank_best: bool = False,
    ) -> tuple[Optional[ScrapedBook], list[str]]:
        """
        Fetch and parse product URLs.

        When rank_best=True (title discovery), score accepted candidates and
        prefer exact title + matching author over the first loose hit.
        """
        ambiguous_notes: list[str] = []
        accepted: list[tuple[float, ScrapedBook]] = []

        for product_url in product_urls[:8]:
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
                if not self.is_parse_useful(parsed):
                    continue
                found_title = str(parsed.fields.get("title", ""))
                found_authors = str(parsed.fields.get("authors", ""))
                if require_hint_match:
                    decision = classify_title_match(
                        hint_title=hint_title,
                        hint_authors=hint_authors,
                        found_title=found_title,
                        found_authors=found_authors,
                    )
                    if decision == "ambiguous":
                        ambiguous_notes.append(
                            ambiguous_match_detail(
                                hint_title=hint_title,
                                found_title=found_title,
                            )
                        )
                        continue
                    if decision != "accept":
                        continue
                parsed.method_used = method
                parsed.success = True
                enriched = self._enrich_reviews_if_needed(parsed)
                if not rank_best:
                    return enriched, ambiguous_notes
                score = title_match_score(hint_title, found_title)
                if authors_roughly_match(hint_authors, found_authors):
                    score += 0.25
                accepted.append((score, enriched))
                break  # next product URL

        if accepted:
            accepted.sort(key=lambda item: item[0], reverse=True)
            return accepted[0][1], ambiguous_notes
        return None, ambiguous_notes

    def _build_title_search_urls(
        self,
        titles: list[str] | str,
        *,
        authors: str = "",
    ) -> list[str]:
        """Build Audible search URLs from title variants (+ optional author)."""
        from utils.title_match import clean_hint_title

        if isinstance(titles, str):
            titles = [titles]
        authors = (authors or "").strip()
        if authors == config.MISSING_VALUE:
            authors = ""
        primary_author = authors.split(",")[0].strip() if authors else ""

        queries: list[str] = []
        for title in titles:
            title = clean_hint_title(title) or (title or "").strip()
            if not title or title == config.MISSING_VALUE:
                continue
            queries.append(title)
            if primary_author:
                queries.append(f"{title} {primary_author}")

        urls: list[str] = []
        for query in self.unique_non_empty(queries):
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
        return self.unique_non_empty(urls)

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
            "whoops",
            "page not found",
            "error",
        ]
        if title in {"whoops", "whoops.", "audible"}:
            return False
        return not any(fragment in title for fragment in banned_fragments)

    def fetch_html_playwright(self, url: str) -> Optional[str]:
        """Level-2 fetch tuned for Audible's JS-heavy storefront (shared browser)."""
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
                page.wait_for_timeout(1500)
                html = page.content()
                final_url = page.url
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

        # PL Assignment fields from Audible product detail labels
        # (Publisher, Release date, Language) and category breadcrumbs (genres).
        detail_fields = self._extract_detail_labels_html(soup)
        for key, value in detail_fields.items():
            if key in result.fields and value:
                if result.fields.get(key) in {config.MISSING_VALUE, "", None}:
                    result.fields[key] = self.text_or_na(value)

        if result.fields.get("genres") in {config.MISSING_VALUE, None, ""}:
            genres = self._extract_genres_html(soup)
            if genres:
                result.fields["genres"] = ", ".join(genres)

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

    def _extract_detail_labels_html(self, soup: BeautifulSoup) -> dict[str, str]:
        """
        Parse Audible label rows such as:
          Publisher
          Penguin Audio
          Release date
          07-01-14
          Language
          English
        """
        fields: dict[str, str] = {}
        label_map = {
            "publisher": "publisher",
            "release date": "publication_date",
            "publication date": "publication_date",
            "language": "language",
            "program type": "format",
        }

        # Common Audible list layout: li with label + value text.
        for li in soup.select("li.bc-list-item, li"):
            text = " ".join(li.get_text(" ", strip=True).split())
            if not text:
                continue
            lowered = text.lower()
            for label, field in label_map.items():
                if lowered.startswith(label):
                    value = text[len(label) :].strip(" :-\u00a0")
                    if value and field not in fields:
                        fields[field] = value
                    break

        # Regex fallback on full page text.
        page_text = soup.get_text("\n", strip=True)
        patterns = {
            "publisher": r"Publisher\s*[:\n]\s*([^\n]{2,80})",
            "publication_date": r"Release date\s*[:\n]\s*([^\n]{2,40})",
            "language": r"Language\s*[:\n]\s*([A-Za-z][A-Za-z\-\s]{1,40})",
        }
        for key, pattern in patterns.items():
            if key in fields:
                continue
            match = re.search(pattern, page_text, flags=re.I)
            if match:
                fields[key] = match.group(1).strip()
        return fields

    def _extract_genres_html(self, soup: BeautifulSoup) -> list[str]:
        """Categories / breadcrumbs used as Audible genres."""
        genres: list[str] = []
        for anchor in soup.select(
            "li.categoriesLabel a, a[href*='/cat/'], "
            "nav.bc-breadcrumb a, .bc-breadcrumb a"
        ):
            text = anchor.get_text(" ", strip=True)
            if text and text.lower() not in {"home", "audible", "categories"}:
                genres.append(text)
        return self.unique_non_empty(genres)

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
            # Keep scanning storefronts until we have several candidates to rank.
            if len(self.unique_non_empty(found)) >= 5:
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
                "ul.bc-list li.productListItem a[href*='/pd/'], "
                "li.productListItem a[href*='/pd/']"
            )

        for anchor in anchors:
            href = str(anchor.get("href") or "")
            if "/pd/" not in href:
                continue
            text = anchor.get_text(" ", strip=True).lower()
            if text in {"whoops", "whoops.", "audible"}:
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
            src = str(img.get("src") or img.get("data-src") or "")
            alt = str(img.get("alt") or "").lower()
            if not src.startswith("http"):
                continue
            if "cover" in alt or "/images/I/" in src or "m.media-amazon.com" in src:
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

        # Reload and try to expose more review text (shared browser).
        try:
            from scraper.browser_pool import shared_page
        except ImportError:
            return parsed

        try:
            with shared_page(
                user_agent=self.DEFAULT_HEADERS["User-Agent"],
                locale="en-US",
            ) as page:
                page.goto(
                    page_url,
                    wait_until="domcontentloaded",
                    timeout=config.PLAYWRIGHT_NAV_TIMEOUT_MS,
                )
                page.wait_for_timeout(1500)
                for _ in range(3):
                    page.mouse.wheel(0, 2500)
                    page.wait_for_timeout(500)
                try:
                    more = page.locator("text=Show more reviews")
                    if more.count() > 0:
                        more.first.click(timeout=2000)
                        page.wait_for_timeout(1500)
                except Exception:  # noqa: BLE001
                    pass
                html = page.content()
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
        match = re.search(r"AUDIBLE_FINAL_URL:(https://[^\s\"'<>]+)", text)
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
