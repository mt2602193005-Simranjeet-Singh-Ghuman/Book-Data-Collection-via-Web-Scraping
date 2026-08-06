"""
scraper/amazon.py

Looks up a book on Amazon (.com and .in) by ISBN.
Amazon blocks plain requests a lot, so Playwright is used when needed.
Prefers amazon.in and title/author search (after Goodreads) to reduce CAPTCHA.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup, Tag

import config
from scraper.base import BaseScraper, ScrapedBook
from utils.title_match import listing_matches_hints, note_title_match


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
        # amazon.in first — often less CAPTCHA-heavy from India networks.
        return [
            f"https://www.amazon.in/dp/{encoded}",
            f"https://www.amazon.com/dp/{encoded}",
            f"https://www.amazon.in/s?k={encoded}&i=stripbooks",
            f"https://www.amazon.com/s?k={encoded}&i=stripbooks",
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

        When Goodreads (or another site) already found title/author, prefer
        title search first — direct /dp/{ISBN} URLs trigger CAPTCHA more often.
        """
        result = ScrapedBook(source=self.source_name, isbn13=isbn13)
        result.fields = self._empty_fields(isbn13)

        has_hints = bool(
            (hint_title or "").strip()
            and hint_title.strip() != config.MISSING_VALUE
        )

        # 1) Title/author path first when hints exist (CAPTCHA avoidance).
        if has_hints:
            title_urls = self._resolve_product_urls_from_query(hint_title, hint_authors)
            if title_urls:
                hit = self._try_product_urls(
                    isbn13,
                    title_urls,
                    require_isbn_match=False,
                    hint_title=hint_title,
                    hint_authors=hint_authors,
                )
                if hit is not None:
                    note_title_match(hit.fields)
                    return hit

        # 2) ISBN /dp + ISBN search
        try:
            candidate_urls = self._resolve_product_urls(isbn13)
        except Exception as exc:  # noqa: BLE001
            result.error = f"URL resolve failed: {exc}"
            return result

        hit = self._try_product_urls(
            isbn13,
            candidate_urls,
            require_isbn_match=True,
            hint_title="",
            hint_authors="",
        )
        if hit is not None:
            return hit

        # 3) Title/author again if ISBN path failed and we skipped step 1
        if not has_hints:
            title_urls = self._resolve_product_urls_from_query(hint_title, hint_authors)
        else:
            title_urls = []
        if title_urls:
            hit = self._try_product_urls(
                isbn13,
                title_urls,
                require_isbn_match=False,
                hint_title=hint_title,
                hint_authors=hint_authors,
            )
            if hit is not None:
                note_title_match(hit.fields)
                return hit

        result.error = f"Amazon: could not fetch book data for ISBN {isbn13}."
        return result

    def fetch_html_requests(self, url: str) -> Optional[str]:
        """Level-1 fetch; treat CAPTCHA shells as failure immediately."""
        html = super().fetch_html_requests(url)
        if html and self._looks_like_captcha(html):
            return None
        return html

    def fetch_html_playwright(self, url: str) -> Optional[str]:
        """
        Level-2 Amazon fetch with anti-bot hardening.

        - Prefer stealth Chromium settings
        - Warm up on the store homepage (cookies)
        - Retry once if a CAPTCHA shell appears
        - Use en-IN locale for amazon.in
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return None

        is_in = "amazon.in" in url
        home = "https://www.amazon.in/" if is_in else "https://www.amazon.com/"
        locale = "en-IN" if is_in else "en-US"
        timezone = "Asia/Kolkata" if is_in else "America/New_York"

        try:
            with sync_playwright() as playwright:
                browser = None
                # Real Chrome channel helps when available; else bundled Chromium.
                for launch_kwargs in (
                    {
                        "channel": "chrome",
                        "headless": True,
                        "args": ["--disable-blink-features=AutomationControlled"],
                    },
                    {
                        "headless": True,
                        "args": ["--disable-blink-features=AutomationControlled"],
                    },
                ):
                    try:
                        browser = playwright.chromium.launch(**launch_kwargs)
                        break
                    except Exception:  # noqa: BLE001
                        browser = None
                if browser is None:
                    return None

                context = browser.new_context(
                    user_agent=self.DEFAULT_HEADERS["User-Agent"],
                    locale=locale,
                    timezone_id=timezone,
                    viewport={"width": 1366, "height": 768},
                    extra_http_headers={
                        "Accept-Language": (
                            "en-IN,en;q=0.9" if is_in else "en-US,en;q=0.9"
                        ),
                    },
                )
                page = context.new_page()
                page.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', "
                    "{get: () => undefined});"
                )

                # Homepage warm-up reduces empty/CAPTCHA shells on /dp/ pages.
                try:
                    page.goto(
                        home,
                        wait_until="domcontentloaded",
                        timeout=config.HTTP_TIMEOUT_SECONDS * 1000,
                    )
                    page.wait_for_timeout(1200)
                except Exception:  # noqa: BLE001
                    pass

                html = ""
                for attempt in range(2):
                    page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=config.HTTP_TIMEOUT_SECONDS * 1000,
                    )
                    page.wait_for_timeout(2500 + attempt * 1500)
                    # Light scroll helps product widgets hydrate.
                    try:
                        page.mouse.wheel(0, 1200)
                        page.wait_for_timeout(800)
                    except Exception:  # noqa: BLE001
                        pass
                    html = page.content()
                    if html and not self._looks_like_captcha(html) and len(html) > 5000:
                        break
                    page.wait_for_timeout(2000)

                final_url = page.url
                context.close()
                browser.close()
                if html and len(html) > 500 and not self._looks_like_captcha(html):
                    return f"<!-- AMAZON_FINAL_URL:{final_url} -->\n" + html
        except Exception:  # noqa: BLE001
            return None
        return None

    def _try_product_urls(
        self,
        isbn13: str,
        candidate_urls: list[str],
        *,
        require_isbn_match: bool,
        hint_title: str,
        hint_authors: str,
    ) -> ScrapedBook | None:
        """Try product URLs; optionally require ISBN or title/author match."""
        fallback: ScrapedBook | None = None

        for url in candidate_urls:
            # Playwright first: Amazon requests almost always hit CAPTCHA.
            for method, fetcher in (
                ("playwright", self.fetch_html_playwright),
                ("requests+bs4", self.fetch_html_requests),
            ):
                self.polite_delay()
                html = fetcher(url)
                if not html or self._looks_like_captcha(html):
                    continue
                # Prefer final URL marker when Playwright redirected.
                page_url = url
                marker = re.search(
                    r"AMAZON_FINAL_URL:(https://[^\s\"'<>]+)", html[:900]
                )
                if marker:
                    page_url = marker.group(1).rstrip(".,;)")
                soup = self.make_soup(html)
                parsed = self.parse_book_page(soup, page_url, isbn13)
                if not self.is_parse_useful(parsed) or self._looks_like_bundle_title(
                    parsed
                ):
                    continue
                parsed.method_used = method
                if require_isbn_match:
                    if self._isbn_matches_page(soup, html, isbn13):
                        parsed.success = True
                        return self._enrich_reviews_if_needed(parsed)
                    if fallback is None and self._has_pl_details(parsed):
                        fallback = parsed
                    continue
                if listing_matches_hints(
                    hint_title=hint_title,
                    hint_authors=hint_authors,
                    found_title=str(parsed.fields.get("title", "")),
                    found_authors=str(parsed.fields.get("authors", "")),
                ):
                    parsed.success = True
                    return self._enrich_reviews_if_needed(parsed)

        if require_isbn_match and fallback is not None:
            fallback.success = True
            return self._enrich_reviews_if_needed(fallback)
        return None

    def _isbn_matches_page(
        self,
        soup: BeautifulSoup,
        html: str,
        isbn13: str,
    ) -> bool:
        """
        Return True when THIS product's detail bullets contain the ISBN-13.

        Do not search the whole HTML (related products / ads caused false matches
        and led to box-set pages being accepted).
        """
        details = self._extract_detail_bullets(soup)
        page_isbn = re.sub(r"[^0-9Xx]", "", str(details.get("_page_isbn", "")))
        if page_isbn and page_isbn.upper() == isbn13.upper():
            return True

        # Narrow check inside detail-bullet markup only.
        detail_html = ""
        for node in soup.select(
            "#detailBullets_feature_div, #detailBulletsWrapper_feature_div, "
            "#productDetails_detailBullets_sections1, #productDetailsTable"
        ):
            detail_html += str(node)
        if not detail_html:
            return False
        compact = re.sub(r"[^0-9Xx]", "", detail_html)
        return isbn13 in compact

    @staticmethod
    def _looks_like_bundle_title(parsed: ScrapedBook) -> bool:
        """Reject Amazon box-sets / multipacks that are not the single book."""
        title = str(parsed.fields.get("title", "")).lower()
        banned = (
            "box set",
            "boxed set",
            "bestselling",
            " collection",
            "bundle",
            "3 set",
            "2 set",
            "set -",
            "omnibus",
        )
        return any(token in title for token in banned)

    @staticmethod
    def _has_pl_details(parsed: ScrapedBook) -> bool:
        """True if at least one PL Assignment detail field was extracted."""
        for key in ("publisher", "publication_date", "language", "origin_country"):
            value = str(parsed.fields.get(key, config.MISSING_VALUE)).strip()
            if value and value != config.MISSING_VALUE:
                return True
        return False
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
            f"https://www.amazon.in/dp/{encoded}",
            f"https://www.amazon.com/dp/{encoded}",
        ]
        search_urls = [
            f"https://www.amazon.in/s?k={encoded}&i=stripbooks",
            f"https://www.amazon.com/s?k={encoded}&i=stripbooks",
        ]

        found: list[str] = []
        for search_url in search_urls:
            self.polite_delay()
            # Playwright first — search via requests is almost always CAPTCHA.
            html = self.fetch_html_playwright(search_url)
            if not html:
                html = self.fetch_html_requests(search_url)
            if not html or self._looks_like_captcha(html):
                continue
            soup = self.make_soup(html)
            for href in self._extract_dp_links(soup, base_url=search_url):
                found.append(href)
            if found:
                break

        # Preserve order, unique: direct first, then discovered links.
        return self.unique_non_empty(direct + found)

    def _resolve_product_urls_from_query(
        self,
        title: str,
        authors: str,
    ) -> list[str]:
        """Harvest /dp/ product links from an Amazon title/author search."""
        title = (title or "").strip()
        if not title or title == config.MISSING_VALUE:
            return []
        authors = (authors or "").strip()
        if authors == config.MISSING_VALUE:
            authors = ""
        encoded = quote(f"{title} {authors}".strip())
        search_urls = [
            f"https://www.amazon.in/s?k={encoded}&i=stripbooks",
            f"https://www.amazon.com/s?k={encoded}&i=stripbooks",
        ]
        found: list[str] = []
        for search_url in search_urls:
            self.polite_delay()
            html = self.fetch_html_playwright(search_url)
            if not html:
                html = self.fetch_html_requests(search_url)
            if not html or self._looks_like_captcha(html):
                continue
            soup = self.make_soup(html)
            for href in self._extract_dp_links(soup, base_url=search_url):
                found.append(href)
            if found:
                break
        return self.unique_non_empty(found)

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
            "#bylineInfo span.author a",
            "span.author a.a-link-normal",
            ".author a",
            "span.author a",
            "#follow_author_link",
            "a.contributorNameID",
            "#booksTitle .author a",
        ]
        banned = {
            "visit amazon's",
            "follow",
            "search",
            "audible",
            "kindle",
            "(author)",
            "(editor)",
        }
        for selector in selectors:
            for node in soup.select(selector):
                text = node.get_text(" ", strip=True)
                low = text.lower()
                if not text or any(b in low for b in banned):
                    continue
                # Drop trailing role labels: "Joni Hilton (Author)"
                text = re.sub(r"\s*\((author|editor|narrator)\)\s*$", "", text, flags=re.I)
                if text:
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

    @staticmethod
    def _clean_amazon_label(text: str) -> str:
        """
        Amazon detail rows contain invisible RTL marks (U+200E / U+200F).

        Example raw text from Inspect Element:
            Publisher ‏ : ‎ Penguin Press
        Without stripping those marks, key matching fails and publisher
        incorrectly becomes N/A even though the value is on the page.
        """
        cleaned = re.sub(r"[\u200e\u200f\u202a-\u202e]", "", text or "")
        return re.sub(r"\s+", " ", cleaned).strip()

    def _extract_detail_bullets(self, soup: BeautifulSoup) -> dict[str, str]:
        """
        Parse Amazon detail bullets / product overview into our schema keys.

        PL Assignment fields commonly found here:
        Publisher, Publication date, Language, and sometimes Country of Origin.
        """
        details: dict[str, str] = {}
        rows: list[tuple[str, str]] = []

        # Prefer bold label span + following value span (stable Inspect Element pattern).
        for li in soup.select("#detailBullets_feature_div li, #detailBulletsWrapper_feature_div li"):
            bold = li.select_one("span.a-text-bold")
            if bold:
                key = self._clean_amazon_label(bold.get_text(" ", strip=True)).rstrip(":")
                # Value is usually the last non-bold span in the row.
                value_spans = [
                    self._clean_amazon_label(s.get_text(" ", strip=True))
                    for s in li.select("span")
                    if "a-text-bold" not in (s.get("class") or [])
                ]
                value = next((v for v in reversed(value_spans) if v and v != ":"), "")
                if key and value:
                    rows.append((key, value))
                    continue
            text = self._clean_amazon_label(li.get_text(" ", strip=True))
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
            "country of origin": "origin_country",
            "country/region of origin": "origin_country",
            "manufacturer": "ignored",
        }

        for raw_key, raw_value in rows:
            key_l = self._clean_amazon_label(raw_key).lower().rstrip(":")
            value = self._clean_amazon_label(raw_value)
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
            elif "country of origin" in key_l or key_l == "country":
                # PL Assignment: Origin / Country of publication
                details["origin_country"] = value
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
            "#main-image",
            "img.a-dynamic-image",
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
        if not urls:
            for meta in soup.select('meta[property="og:image"], meta[name="og:image"]'):
                content = str(meta.get("content") or "").strip()
                if content.startswith("http"):
                    urls.append(content)
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
        html = self.fetch_html_playwright(page_url)
        if not html or self._looks_like_captcha(html):
            return []
        return self._extract_reviews_from_soup(self.make_soup(html))
