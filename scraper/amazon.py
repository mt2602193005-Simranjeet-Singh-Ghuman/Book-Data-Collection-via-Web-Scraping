"""
scraper/amazon.py

Looks up a book on Amazon (.com and .in) by ISBN.
Amazon blocks plain requests a lot, so Playwright is used when needed.
"""

from __future__ import annotations

import re
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup, Tag

import config
from scraper.base import BaseScraper, ScrapedBook


class AmazonScraper(BaseScraper):
    """Amazon-specific scraper (Module 4)."""

    source_name = "Amazon"

    def build_candidate_urls(self, isbn13: str) -> list[str]:
        """
        Build Amazon product and search URLs for one ISBN.

        Parameters
        ----------
        isbn13 : str
            Normalized ISBN-13.
        """
        encoded = quote(isbn13)
        return [
            f"https://www.amazon.com/dp/{encoded}",
            f"https://www.amazon.in/dp/{encoded}",
            f"https://www.amazon.com/s?k={encoded}&i=stripbooks",
            f"https://www.amazon.in/s?k={encoded}&i=stripbooks",
        ]

    def scrape(
        self,
        isbn13: str,
        *,
        hint_title: str = "",
        hint_authors: str = "",
    ) -> ScrapedBook:
        """
        Resolve a product URL when needed, then run Level-1 / Level-2 parsing.

        Amazon search pages do not contain full book metadata, so we first try
        to convert search hits into /dp/ product URLs before parsing fields.
        """
        _ = hint_title, hint_authors
        result = ScrapedBook(source=self.source_name, isbn13=isbn13)
        result.fields = self._empty_fields(isbn13)

        try:
            candidate_urls = self._resolve_product_urls(isbn13)
        except Exception as exc:  # noqa: BLE001
            result.error = f"URL resolve failed: {exc}"
            return result

        # Keep a fallback if no page hard-matches the ISBN-13 in details.
        fallback: ScrapedBook | None = None

        for url in candidate_urls:
            # ---- Level 1 ----
            self.polite_delay()
            html = self.fetch_html_requests(url)
            if html and not self._looks_like_captcha(html):
                parsed = self.parse_book_page(self.make_soup(html), url, isbn13)
                if self.is_parse_useful(parsed):
                    parsed.method_used = "requests+bs4"
                    if self._isbn_matches_page(html, isbn13):
                        parsed.success = True
                        return self._enrich_reviews_if_needed(parsed)
                    if fallback is None:
                        fallback = parsed

            # ---- Level 2 ----
            self.polite_delay()
            html = self.fetch_html_playwright(url)
            if html and not self._looks_like_captcha(html):
                parsed = self.parse_book_page(self.make_soup(html), url, isbn13)
                if self.is_parse_useful(parsed):
                    parsed.method_used = "playwright"
                    if self._isbn_matches_page(html, isbn13):
                        parsed.success = True
                        return self._enrich_reviews_if_needed(parsed)
                    if fallback is None:
                        fallback = parsed

        if fallback is not None:
            fallback.success = True
            return self._enrich_reviews_if_needed(fallback)

        result.error = (
            f"Amazon: could not extract usable book data "
            f"with requests+bs4 or Playwright for ISBN {isbn13}"
        )
        return result

    @staticmethod
    def _isbn_matches_page(html: str, isbn13: str) -> bool:
        """
        Return True when the product HTML clearly mentions this ISBN-13.

        Helps avoid saving a different edition discovered via Amazon search.
        """
        compact = re.sub(r"[^0-9Xx]", "", html)
        return isbn13 in compact or isbn13 in html

    def parse_book_page(
        self,
        soup: BeautifulSoup,
        page_url: str,
        isbn13: str,
    ) -> ScrapedBook:
        """Parse an Amazon product (or search) page into ScrapedBook."""
        result = ScrapedBook(source=self.source_name, isbn13=isbn13)
        result.fields = self._empty_fields(isbn13)
        result.fields["url"] = self.text_or_na(page_url)

        # Search page: no product title widget yet -> mark useless for fallback.
        if self._is_search_page(page_url, soup):
            return result

        result.fields["title"] = self._extract_title(soup)
        result.fields["authors"] = self._extract_authors(soup)
        result.fields["description"] = self._extract_description(soup)
        result.blurb = (
            result.fields["description"]
            if result.fields["description"] != config.MISSING_VALUE
            else ""
        )

        details = self._extract_detail_bullets(soup)
        for key, value in details.items():
            if key in result.fields and value:
                result.fields[key] = self.text_or_na(value)

        genres = self._extract_genres(soup)
        result.fields["genres"] = ", ".join(genres) if genres else config.MISSING_VALUE

        rating, ratings_count = self._extract_rating(soup)
        result.fields["rating"] = rating
        result.fields["ratings_count"] = ratings_count
        result.fields["price"] = self._extract_price(soup)

        result.cover_urls = self._extract_cover_urls(soup)
        result.reviews = self._extract_reviews_from_soup(soup)
        result.fields["isbn13"] = isbn13
        result.fields["source"] = self.source_name
        result.fields["url"] = self.text_or_na(page_url)
        return result

    # ------------------------------------------------------------------
    # URL resolution helpers
    # ------------------------------------------------------------------
    def _resolve_product_urls(self, isbn13: str) -> list[str]:
        """
        Prefer direct /dp/ URLs; also harvest /dp/ links from search pages.
        """
        encoded = quote(isbn13)
        direct = [
            f"https://www.amazon.com/dp/{encoded}",
            f"https://www.amazon.in/dp/{encoded}",
        ]
        search_urls = [
            f"https://www.amazon.com/s?k={encoded}&i=stripbooks",
            f"https://www.amazon.in/s?k={encoded}&i=stripbooks",
        ]

        found: list[str] = []
        for search_url in search_urls:
            self.polite_delay()
            html = self.fetch_html_requests(search_url)
            if not html or self._looks_like_captcha(html):
                html = self.fetch_html_playwright(search_url)
            if not html or self._looks_like_captcha(html):
                continue
            soup = self.make_soup(html)
            for href in self._extract_dp_links(soup, base_url=search_url):
                found.append(href)

        # Preserve order, unique: direct first, then discovered links.
        return self.unique_non_empty(direct + found)

    def _extract_dp_links(self, soup: BeautifulSoup, base_url: str) -> list[str]:
        """Extract Amazon /dp/ASIN product links from a search page."""
        links: list[str] = []
        for anchor in soup.select("a[href*='/dp/']"):
            href = str(anchor.get("href", ""))
            match = re.search(r"(/dp/[A-Z0-9]{10})", href, flags=re.I)
            if not match:
                # ISBN-style paths also appear sometimes
                match = re.search(r"(/dp/\d{10,13})", href)
            if not match:
                continue
            absolute = urljoin(base_url, match.group(1))
            # Normalize to short /dp/URL without tracking junk
            short = re.sub(r"(/dp/[A-Z0-9]{10,13}).*", r"\1", absolute, flags=re.I)
            links.append(short)
        return links

    @staticmethod
    def _is_search_page(page_url: str, soup: BeautifulSoup) -> bool:
        if "/s?" in page_url or "/s/" in page_url:
            # If product title exists somehow, treat as product page.
            if soup.select_one("#productTitle"):
                return False
            return True
        return False

    @staticmethod
    def _looks_like_captcha(html: str) -> bool:
        """Detect common Amazon bot-check pages."""
        lowered = html.lower()
        markers = [
            "enter the characters you see below",
            "type the characters you see in this image",
            "api-services-support@amazon.com",
            "/errors/validatecaptcha",
            "sorry, we just need to make sure you're not a robot",
        ]
        return any(marker in lowered for marker in markers)

    # ------------------------------------------------------------------
    # Field extractors
    # ------------------------------------------------------------------
    def _extract_title(self, soup: BeautifulSoup) -> str:
        selectors = ["#productTitle", "#title span", "h1#title", "span#ebooksProductTitle"]
        for selector in selectors:
            node = soup.select_one(selector)
            if node:
                text = node.get_text(" ", strip=True)
                if text:
                    return self.text_or_na(text)
        return config.MISSING_VALUE

    def _extract_authors(self, soup: BeautifulSoup) -> str:
        authors: list[str] = []
        selectors = [
            "#bylineInfo .author a",
            "#bylineInfo a.a-link-normal",
            ".author a",
            "span.author a",
            "#follow_author_link",
        ]
        for selector in selectors:
            for node in soup.select(selector):
                text = node.get_text(" ", strip=True)
                if text and text.lower() not in {"visit amazon's", "follow"}:
                    authors.append(text)
            if authors:
                break
        authors = [
            a for a in self.unique_non_empty(authors)
            if "amazon" not in a.lower()
        ]
        return ", ".join(authors) if authors else config.MISSING_VALUE

    def _extract_description(self, soup: BeautifulSoup) -> str:
        selectors = [
            "#bookDescription_feature_div",
            "#productDescription",
            "#bookDesc_iframe_wrapper",
            "div[data-feature-name='bookDescription']",
            "#feature-bullets",
        ]
        for selector in selectors:
            node = soup.select_one(selector)
            if not node:
                continue
            text = node.get_text("\n", strip=True)
            if text and len(text) > 40:
                return self.text_or_na(text)
        return config.MISSING_VALUE

    def _extract_genres(self, soup: BeautifulSoup) -> list[str]:
        """Use breadcrumb categories as Amazon 'genres'."""
        genres: list[str] = []
        for node in soup.select(
            "#wayfinding-breadcrumbs_feature_div ul li span.a-list-item a, "
            "#wayfinding-breadcrumbs_feature_div a"
        ):
            text = node.get_text(" ", strip=True)
            if text and text.lower() not in {"books", "kindle store"}:
                genres.append(text)
        return self.unique_non_empty(genres)

    def _extract_rating(self, soup: BeautifulSoup) -> tuple[str, str]:
        rating = config.MISSING_VALUE
        count = config.MISSING_VALUE

        rating_node = soup.select_one(
            "span[data-hook='rating-out-of-text'], "
            "#acrPopover span.a-icon-alt, "
            "i.a-icon-star span.a-icon-alt"
        )
        if rating_node:
            raw = rating_node.get_text(" ", strip=True)
            match = re.search(r"(\d+(?:\.\d+)?)", raw)
            rating = self.text_or_na(match.group(1) if match else raw)

        count_node = soup.select_one("#acrCustomerReviewText, span[data-hook='total-review-count']")
        if count_node:
            raw = count_node.get_text(" ", strip=True)
            match = re.search(r"([\d,]+)", raw)
            count = self.text_or_na(match.group(1) if match else raw)
        return rating, count

    def _extract_price(self, soup: BeautifulSoup) -> str:
        selectors = [
            "span.a-price .a-offscreen",
            "#price",
            "#price_inside_buybox",
            ".kindle-price .a-color-price",
            "#kindle-price",
        ]
        for selector in selectors:
            node = soup.select_one(selector)
            if node:
                text = node.get_text(" ", strip=True)
                if text:
                    return self.text_or_na(text)
        return config.MISSING_VALUE

    def _extract_detail_bullets(self, soup: BeautifulSoup) -> dict[str, str]:
        """
        Parse Amazon detail bullets / product overview into our schema keys.
        """
        details: dict[str, str] = {}
        rows: list[tuple[str, str]] = []

        # detailBullets style: <span class="a-text-bold">Publisher</span> <span>Value</span>
        for li in soup.select("#detailBullets_feature_div li, #detailBulletsWrapper_feature_div li"):
            text = li.get_text(" ", strip=True)
            if ":" in text:
                key, value = text.split(":", 1)
                rows.append((key.strip(), value.strip()))

        # Older product details table
        for row in soup.select("#productDetailsTable tr, #detailBullets_averageCustomerReviews"):
            header = row.select_one("td.bucket h2, th, .a-text-bold")
            value_node = row.select_one("td.value, td:last-child")
            if header and value_node:
                rows.append(
                    (
                        header.get_text(" ", strip=True),
                        value_node.get_text(" ", strip=True),
                    )
                )

        # Product overview (carousel cards)
        for row in soup.select("#productOverview_feature_div tr"):
            cols = row.select("td")
            if len(cols) >= 2:
                rows.append(
                    (
                        cols[0].get_text(" ", strip=True),
                        cols[1].get_text(" ", strip=True),
                    )
                )

        key_map = {
            "publisher": "publisher",
            "publication date": "publication_date",
            "publish date": "publication_date",
            "language": "language",
            "paperback": "pages",
            "hardcover": "pages",
            "print length": "pages",
            "page count": "pages",
            "item weight": "ignored",
            "dimensions": "ignored",
            "series": "series",
            "edition": "edition",
            "listening length": "format",
        }

        for raw_key, raw_value in rows:
            key_l = re.sub(r"\s+", " ", raw_key).strip().lower().rstrip(":")
            value = raw_value.strip()
            if not value:
                continue

            # Keep observed ISBN text for matching/debug (not a schema field).
            if key_l in {"isbn-13", "isbn13", "isbn-10", "isbn10"}:
                details["_page_isbn"] = re.sub(r"[^0-9Xx]", "", value)
                continue

            if key_l in {"publisher"}:
                # Often: "Penguin Books (May 12, 2015)"
                pub_match = re.match(r"(.+?)\s*\((.+?)\)\s*$", value)
                if pub_match:
                    details["publisher"] = pub_match.group(1).strip()
                    details.setdefault("publication_date", pub_match.group(2).strip())
                else:
                    details["publisher"] = value
                continue

            if "page" in key_l or key_l in {"paperback", "hardcover"}:
                pages_match = re.search(r"(\d+)\s*pages", value, flags=re.I)
                if pages_match:
                    details["pages"] = pages_match.group(1)
                    if key_l in {"paperback", "hardcover"}:
                        details["format"] = key_l.title()
                else:
                    num = re.search(r"(\d+)", value)
                    if num:
                        details["pages"] = num.group(1)
                continue

            mapped = key_map.get(key_l)
            if mapped and mapped not in {"ignored", "isbn13_check", "isbn10_check"}:
                details[mapped] = value
            elif "language" in key_l:
                details["language"] = value
            elif "publication" in key_l or key_l.endswith("date"):
                details["publication_date"] = value
            elif "series" in key_l:
                details["series"] = value
            elif "edition" in key_l:
                details["edition"] = value

        # Best-sellers rank / format clues from title badges
        format_node = soup.select_one("#productSubtitle, #binding")
        if format_node and "format" not in details:
            details["format"] = format_node.get_text(" ", strip=True)

        return details

    def _extract_cover_urls(self, soup: BeautifulSoup) -> list[str]:
        urls: list[str] = []
        selectors = [
            "#imgBlkFront",
            "#landingImage",
            "#ebooksImgBlkFront",
            "img[data-a-image-name='landingImage']",
        ]
        for selector in selectors:
            for img in soup.select(selector):
                if not isinstance(img, Tag):
                    continue
                for attr in ("data-old-hires", "data-a-dynamic-image", "src"):
                    raw = img.get(attr)
                    if not raw:
                        continue
                    raw_s = str(raw)
                    if raw_s.startswith("http"):
                        urls.append(raw_s.split()[0])
                        break
                    # data-a-dynamic-image is a JSON-like dict of url->size
                    for match in re.findall(r"(https://[^\"']+)", raw_s):
                        urls.append(match)
            if urls:
                break
        # Prefer larger images when duplicates differ only by size params
        return self.unique_non_empty(urls)[:3]

    def _extract_reviews_from_soup(self, soup: BeautifulSoup) -> list[str]:
        """
        One list entry per reviewer card (not one giant merged string).

        Prefer full review cards so each saved file / blank-line block is
        clearly a different user's review.
        """
        reviews: list[str] = []

        cards = soup.select(
            "[data-hook='review'], li[data-hook='review'], "
            "div[id^='customer_review'], div.review"
        )
        for card in cards:
            author_node = card.select_one(
                ".a-profile-name, [data-hook='review-author'], span.a-profile-name"
            )
            title_node = card.select_one(
                "[data-hook='review-title'] span:not(.a-icon-alt), "
                "a[data-hook='review-title']"
            )
            body_node = card.select_one(
                "[data-hook='review-body'], [data-hook='reviewText'], "
                ".review-text-content"
            )
            parts: list[str] = []
            if author_node:
                author = author_node.get_text(" ", strip=True)
                if author:
                    parts.append(f"Reviewer: {author}")
            if title_node:
                title = self._clean_review_text(title_node.get_text(" ", strip=True))
                if title:
                    parts.append(title)
            if body_node:
                body = self._clean_review_text(body_node.get_text("\n", strip=True))
                if body:
                    parts.append(body)
            text = "\n".join(parts).strip()
            if text and len(text) > 20:
                reviews.append(text)

        if reviews:
            return self.unique_non_empty(reviews)

        # Fallback: older / flatter markup without review cards.
        selectors = [
            "[data-hook='reviewText']",
            "[data-hook='reviewTextContainer']",
            "div[data-hook='review-body']",
            ".review-text-content",
        ]
        for selector in selectors:
            for node in soup.select(selector):
                text = self._clean_review_text(node.get_text("\n", strip=True))
                if text and len(text) > 20:
                    reviews.append(text)
            if reviews:
                break
        return self.unique_non_empty(reviews)

    @staticmethod
    def _clean_review_text(text: str) -> str:
        """Remove common Amazon accessibility / expand-collapse helper phrases."""
        cleaned = " ".join(str(text).split())
        junk_phrases = [
            "Brief content visible, double tap to read full content.",
            "Full content visible, double tap to read brief content.",
            "Read more",
        ]
        for phrase in junk_phrases:
            cleaned = cleaned.replace(phrase, " ")
        return " ".join(cleaned.split())

    def _enrich_reviews_if_needed(self, parsed: ScrapedBook) -> ScrapedBook:
        """
        Collect more reviews from product + dedicated review pages.

        Strategy:
            1) Level-1 requests on /product-reviews/{ASIN}?pageNumber=N
            2) Playwright fallback on those review pages if needed
        """
        if len(parsed.reviews) >= config.MIN_REVIEWS_PER_SOURCE:
            return parsed

        page_url = str(parsed.fields.get("url", "")).strip()
        if not page_url.startswith("http"):
            return parsed

        asin = self._extract_asin(page_url)
        review_urls: list[str] = []
        if asin:
            # Try both marketplaces; one host may serve review HTML while the
            # other returns a soft-block shell page.
            for host in ("https://www.amazon.com", "https://www.amazon.in"):
                for page_number in range(1, 4):
                    review_urls.append(
                        f"{host}/product-reviews/{asin}"
                        f"?pageNumber={page_number}&reviewerType=all_reviews"
                    )

        collected = list(parsed.reviews)

        # Fast path: requests on review listing pages
        for review_url in review_urls:
            if len(collected) >= config.MIN_REVIEWS_PER_SOURCE:
                break
            self.polite_delay()
            html = self.fetch_html_requests(review_url)
            if not html or self._looks_like_captcha(html):
                continue
            collected.extend(self._extract_reviews_from_soup(self.make_soup(html)))
            collected = self.unique_non_empty(collected)

        # Slow path: Playwright on product page + first review page
        if len(collected) < config.MIN_REVIEWS_PER_SOURCE:
            targets = [page_url]
            if review_urls:
                targets.append(review_urls[0])
            for target in targets:
                extra = self._collect_reviews_with_playwright(target)
                collected = self.unique_non_empty(collected + extra)
                if len(collected) >= config.MIN_REVIEWS_PER_SOURCE:
                    break

        parsed.reviews = collected[: max(config.MIN_REVIEWS_PER_SOURCE, len(collected))]
        return parsed

    @staticmethod
    def _extract_asin(page_url: str) -> str:
        """Extract 10-char ASIN (or ISBN used as /dp/ id) from a product URL."""
        match = re.search(r"/dp/([A-Z0-9]{10,13})", page_url, flags=re.I)
        return match.group(1) if match else ""

    def _collect_reviews_with_playwright(self, page_url: str) -> list[str]:
        """Load a product/review page in Chromium and scrape review bodies."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return []

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

                for _ in range(6):
                    page.mouse.wheel(0, 2800)
                    page.wait_for_timeout(600)

                html = page.content()
                context.close()
                browser.close()
                if self._looks_like_captcha(html):
                    return []
                return self._extract_reviews_from_soup(self.make_soup(html))
        except Exception:  # noqa: BLE001
            return []
