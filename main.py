"""
simranjeet singh ghuman

main.py by simranjeet singh ghuman

Run this file to start the scraper:

    python main.py
    python main.py --refresh          # re-scrape ISBNs already in master.json
    python main.py --refresh 9780...  # refresh one ISBN

Flow:
1. Create output folders if needed
2. Ask for one ISBN or a CSV (or use --refresh)
3. For CSV: ask how many ISBNs (number or "all")
4. Normalize ISBN-13
5. For each ISBN ask if already scraped (rescrape / skip)
   (--refresh always re-scrapes; never skips)
6. Scrape Goodreads, Amazon, Kobo, Audible, BookBub
7. Save JSON + covers / blurbs / reviews (always under the input ISBN)
8. Show progress like 04/20
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import config
from scraper.amazon import AmazonScraper
from scraper.audible import AudibleScraper
from scraper.base import BaseScraper
from scraper.bookbub import BookBubScraper
from scraper.goodreads import GoodreadsScraper
from scraper.kobo import KoboScraper
from utils.folder_setup import create_project_folders, ensure_preprocessing_csv_header
from utils.io_handlers import (
    append_preprocessing_log,
    empty_source_record,
    ensure_isbn_placeholders,
    isbn_already_scraped,
    load_master_json,
    load_source_json,
    merge_source_record,
    save_master_json,
    save_source_json,
)
from utils.isbn import IsbnResult, normalize_isbn_list
from utils.keep_awake import KeepAwake
from utils.media_saver import download_covers, save_blurb, save_reviews


def print_banner() -> None:
    """Print a clear terminal banner."""
    print("=" * 50)
    print("  BOOK WEB SCRAPER - Programming Lab Assignment 1")
    print("  Sources: Goodreads | Amazon | Kobo | Audible | BookBub")
    print("=" * 50)
    print()


def ensure_playwright_ready(verbose: bool = True) -> bool:
    """
    Make sure Playwright Chromium is installed (needed for Level-2 scraping).

    Returns True when a browser launch smoke-test succeeds.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        if verbose:
            print("[WARN] Playwright not installed. Level-2 scraping disabled.")
        return False

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            browser.close()
        if verbose:
            print("[OK] Playwright Chromium ready")
        return True
    except Exception as exc:  # noqa: BLE001
        if verbose:
            print("[WARN] Playwright Chromium missing/unusable.")
            print("       Run: python -m playwright install chromium")
            print(f"       Detail: {exc}")
        return False


def prompt_manual_isbn() -> str:
    """Ask the user to type one ISBN-10 or ISBN-13."""
    print("Manual ISBN entry selected.")
    print("Enter ISBN-10 or ISBN-13 (you may include hyphens):")
    return input("> ").strip()


def prompt_csv_path() -> Path:
    """Ask for a CSV path, or Enter for default input/2602193005.csv."""
    default_csv = config.INPUT_DIR / "2602193005.csv"
    print("CSV input selected.")
    print(f"Press Enter to use default: {default_csv}")
    print("Or type a full / relative CSV path:")
    typed = input("> ").strip()
    if not typed:
        return default_csv
    return Path(typed).expanduser().resolve()


def prompt_csv_limit(total_available: int) -> int:
    """
    Ask how many ISBNs to take from the CSV.

    Examples:
      20   -> first 20
      34   -> first 34
      all  -> every ISBN in the file
    """
    print()
    print("How many ISBNs should be taken from the CSV?")
    print(f"  - type a number (example: 20 or 34)  [file has {total_available} rows]")
    print('  - type "all" to process every ISBN in the CSV')
    print(f"  - press Enter for default ({config.CSV_ISBN_LIMIT})")
    typed = input("> ").strip().lower()

    if typed == "":
        return min(config.CSV_ISBN_LIMIT, total_available)
    if typed == "all":
        return total_available
    if typed.isdigit() and int(typed) > 0:
        return min(int(typed), total_available)

    print(f"[WARN] Invalid value. Using default {config.CSV_ISBN_LIMIT}.")
    return min(config.CSV_ISBN_LIMIT, total_available)


def count_csv_isbns(csv_path: Path) -> int:
    """Count ISBN data rows in a CSV (skips header)."""
    count = 0
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for line_number, line in enumerate(handle, start=1):
            value = line.strip().strip(",")
            if not value:
                continue
            if line_number == 1 and "isbn" in value.lower() and not any(
                ch.isdigit() for ch in value
            ):
                continue
            count += 1
    return count


def load_isbns_from_csv(csv_path: Path, limit: int) -> list[str]:
    """Read up to `limit` ISBN values from a CSV file."""
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    print("Reading CSV...")
    isbns: list[str] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for line_number, line in enumerate(handle, start=1):
            value = line.strip().strip(",")
            if not value:
                continue
            if line_number == 1 and value.replace("-", "").lower() in {
                "isbn13",
                "isbn",
                "isbn_13",
            }:
                continue
            if line_number == 1 and "isbn" in value.lower() and not any(
                ch.isdigit() for ch in value
            ):
                continue
            isbns.append(value)
            if len(isbns) >= limit:
                break

    if not isbns:
        raise ValueError(f"No ISBN rows found in: {csv_path}")

    print(f"{len(isbns)} ISBNs loaded from CSV")
    return isbns


def show_menu() -> str:
    """Display the input-method menu and return the user's choice."""
    print("Select input method:")
    print("  1) Enter ONE ISBN manually")
    print("  2) Load ISBNs from CSV file")
    print("       (you will choose how many: a number, or type all)")
    print("  3) Refresh already-scraped ISBNs in master.json")
    print("       (re-scrape all 5 sites; update N/A with newly found values)")
    print("  0) Exit")
    return input("> ").strip()


def process_isbn_inputs(raw_isbns: list[str]) -> list[IsbnResult]:
    """Validate/normalize ISBNs, log problems, create JSON placeholders."""
    print()
    print("Validating / normalizing ISBNs...")
    print("-" * 40)

    valid_results, invalid_pairs = normalize_isbn_list(raw_isbns)

    for original, message in invalid_pairs:
        issue_type = "Duplicate ISBN" if "Duplicate" in message else "Invalid ISBN"
        append_preprocessing_log(
            isbn13=config.MISSING_VALUE,
            source="Input",
            issue_type=issue_type,
            details=f"{original} | {message}",
            action_taken="Skipped and continued",
        )
        print(f"[WARN] {issue_type}: {original}")

    for result in valid_results:
        ensure_isbn_placeholders(result.isbn13)
        append_preprocessing_log(
            isbn13=result.isbn13,
            source="Input",
            issue_type="ISBN Normalized",
            details=result.detail,
            action_taken="Stored ISBN-13 skeleton in master + source JSON files",
        )
        converted = "yes" if result.was_converted_from_isbn10 else "no"
        print(
            f"[OK] {result.original} -> {result.isbn13} "
            f"(from ISBN-10: {converted})"
        )

    print("-" * 40)
    print(f"Valid ISBNs ready: {len(valid_results)}")
    print(f"Skipped invalid/duplicate: {len(invalid_pairs)}")
    return valid_results


def ask_rescrape_or_skip(isbn13: str) -> bool:
    """
    Decide whether to scrape an ISBN that may already exist in master.json.

    Always re-scrapes (no interactive prompt) so batch CSV / long runs are not
    blocked waiting for keyboard input. Existing good values are preserved by
    merge_source_record (N/A does not wipe prior data).
    """
    if isbn_already_scraped(isbn13):
        print(f"ISBN {isbn13} already in master — re-scraping to fill gaps.")
        append_preprocessing_log(
            isbn13=isbn13,
            source="Input",
            issue_type="Already Scraped",
            details="Auto re-scrape (no prompt) to maximize filled fields",
            action_taken="Re-scrape continued",
        )
    return True


def _reset_source_record(isbn13: str, source: str) -> None:
    """Replace one source block with an all-N/A skeleton (keeps other sources)."""
    ensure_isbn_placeholders(isbn13)
    blank = empty_source_record(isbn13, source)
    master = load_master_json()
    master[isbn13][source] = blank
    save_master_json(master)
    source_data = load_source_json(source)
    source_data[isbn13] = blank
    save_source_json(source, source_data)


def _best_hints_from_master(isbn13: str) -> tuple[str, str]:
    """
    Pull title/authors from any source that already succeeded for this ISBN.

    Prefer Goodreads (usually cleaner titles) over Amazon (often includes
    series text in parentheses that breaks other sites' search/slugs).
    """
    from utils.title_match import clean_hint_title

    master = load_master_json()
    record = master.get(isbn13) or {}
    title = ""
    authors = ""
    for source in ("Goodreads", "Amazon", "Kobo", "Audible", "BookBub"):
        block = record.get(source) or {}
        t = str(block.get("title", "")).strip()
        a = str(block.get("authors", "")).strip()
        if t and t != config.MISSING_VALUE and not title:
            title = clean_hint_title(t) or t
        if a and a != config.MISSING_VALUE and not authors:
            authors = a
    return title, authors


def scrape_and_persist(
    isbn13: str,
    scraper: BaseScraper,
    *,
    hint_title: str = "",
    hint_authors: str = "",
) -> bool:
    """
    Scrape one website for one ISBN and persist JSON + media files.

    Returns True if scrape succeeded.
    """
    source = scraper.source_name
    print(source)
    try:
        scraped = scraper.scrape(
            isbn13,
            hint_title=hint_title,
            hint_authors=hint_authors,
        )
    except Exception as exc:  # noqa: BLE001
        append_preprocessing_log(
            isbn13=isbn13,
            source=source,
            issue_type="Parsing Failure",
            details=str(exc),
            action_taken="Kept previous values (if any) and continued",
        )
        # Do NOT wipe prior successful data for this source.
        print("Failed")
        return False

    if not scraped.success:
        append_preprocessing_log(
            isbn13=isbn13,
            source=source,
            issue_type=(
                "Network Failure"
                if "could not extract" in (scraped.error or "").lower()
                else "Parsing Failure"
            ),
            details=scraped.error or f"Unknown {source} failure",
            action_taken="Kept previous values (if any) and continued",
        )
        # Keep any earlier good metadata; failed retry must not blank the JSON.
        print("Failed")
        return False

    merge_source_record(isbn13, source, scraped.fields)

    blurb_path = save_blurb(
        isbn13,
        source,
        scraped.blurb or scraped.fields.get("description", ""),
    )
    review_paths = save_reviews(isbn13, source, scraped.reviews)
    cover_paths = download_covers(
        isbn13,
        source,
        scraped.cover_urls,
        session=scraper.session,
    )
    review_count = len([r for r in scraped.reviews if str(r).strip()])

    if not blurb_path:
        append_preprocessing_log(
            isbn13=isbn13,
            source=source,
            issue_type="Missing Fields",
            details="Blurb unavailable",
            action_taken="Stored N/A and continued",
        )
    if not cover_paths:
        append_preprocessing_log(
            isbn13=isbn13,
            source=source,
            issue_type="Missing Cover Image",
            details="No cover downloaded",
            action_taken="Continued without cover",
        )
    if review_count < config.MIN_REVIEWS_PER_SOURCE:
        append_preprocessing_log(
            isbn13=isbn13,
            source=source,
            issue_type="Reviews Shortfall",
            details=(
                f"Saved {review_count} reviews; "
                f"target was {config.MIN_REVIEWS_PER_SOURCE}"
            ),
            action_taken="Saved available reviews and continued",
        )

    append_preprocessing_log(
        isbn13=isbn13,
        source=source,
        issue_type="Scrape Success",
        details=(
            f"method={scraped.method_used}; "
            f"covers={len(cover_paths)}; "
            f"reviews={review_count}; "
            f"blurb={'yes' if blurb_path else 'no'}"
        ),
        action_taken="Master JSON + source JSON updated",
    )

    print("Completed")
    print(f"  method: {scraped.method_used}")
    print(f"  title: {scraped.fields.get('title', config.MISSING_VALUE)}")
    print(f"  covers saved: {len(cover_paths)}")
    print(f"  reviews saved: {review_count}")
    print(f"  blurb saved: {'yes' if blurb_path else 'no'}")
    return True


def run_scraping(
    valid_results: list[IsbnResult],
    *,
    force_refresh: bool = False,
) -> None:
    """Scrape all five websites for each ISBN, with progress XX/YY."""
    # Goodreads first (reliable ISBN lookup), then Amazon and others can
    # reuse title/author when their own ISBN search misses or is blocked.
    scrapers: list[BaseScraper] = [
        GoodreadsScraper(),
        AmazonScraper(),
        KoboScraper(),
        AudibleScraper(),
        BookBubScraper(),
    ]

    total = len(valid_results)
    completed = 0

    for result in valid_results:
        print()
        print("-" * 40)
        print("ISBN")
        print(result.isbn13)
        print()

        if force_refresh:
            print("Refresh mode: re-scraping all 5 websites...")
        elif not ask_rescrape_or_skip(result.isbn13):
            completed += 1
            print(f"Progress: {completed:02d}/{total:02d}")
            print("-" * 40)
            continue

        # Retry only sites that failed before any source had title/authors.
        needs_retry: list[BaseScraper] = []

        for scraper in scrapers:
            hint_title, hint_authors = _best_hints_from_master(result.isbn13)
            had_metadata = bool(hint_title)
            ok = scrape_and_persist(
                result.isbn13,
                scraper,
                hint_title=hint_title,
                hint_authors=hint_authors,
            )
            if not ok and not had_metadata:
                needs_retry.append(scraper)

        hint_title, hint_authors = _best_hints_from_master(result.isbn13)
        if needs_retry and hint_title:
            for scraper in needs_retry:
                scrape_and_persist(
                    result.isbn13,
                    scraper,
                    hint_title=hint_title,
                    hint_authors=hint_authors,
                )

        completed += 1
        print()
        print("Master JSON Updated")
        print("Images / Reviews / Blurb saved when available")
        print(f"Progress: {completed:02d}/{total:02d}")
        print("-" * 40)


def _parse_cli_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ISBN book web scraper (Programming Lab Assignment 1)",
    )
    parser.add_argument(
        "--refresh",
        nargs="*",
        metavar="ISBN",
        help=(
            "Re-scrape ISBNs already in master.json (all five sites). "
            "Pass optional ISBN(s); with no ISBN, refresh every ISBN in master.json."
        ),
    )
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    """Program entry orchestrator."""
    args = _parse_cli_args(list(argv) if argv is not None else sys.argv[1:])

    print_banner()

    create_project_folders(verbose=True)
    ensure_preprocessing_csv_header(verbose=True)
    ensure_playwright_ready(verbose=True)
    print()

    raw_isbns: list[str] = []
    force_refresh = False

    # CLI refresh path: python main.py --refresh [ISBN ...]
    if args.refresh is not None:
        force_refresh = True
        if args.refresh:
            raw_isbns = list(args.refresh)
            print(f"Refresh mode: {len(raw_isbns)} ISBN(s) from command line.")
        else:
            master = load_master_json()
            raw_isbns = list(master.keys())
            print(f"Refresh mode: {len(raw_isbns)} ISBN(s) from master.json.")
            if not raw_isbns:
                print("[ERROR] master.json has no ISBNs to refresh.")
                return 1
    else:
        choice = show_menu()
        print()

        if choice == "0":
            print("Exiting without scraping.")
            return 0

        if choice == "1":
            isbn = prompt_manual_isbn()
            if not isbn:
                print("[ERROR] ISBN cannot be empty.")
                return 1
            raw_isbns = [isbn]
        elif choice == "2":
            try:
                csv_path = prompt_csv_path()
                total_available = count_csv_isbns(csv_path)
                if total_available <= 0:
                    print("[ERROR] No ISBN rows found in CSV.")
                    return 1
                limit = prompt_csv_limit(total_available)
                raw_isbns = load_isbns_from_csv(csv_path, limit=limit)
            except (FileNotFoundError, ValueError) as exc:
                print(f"[ERROR] {exc}")
                return 1
        elif choice == "3":
            force_refresh = True
            master = load_master_json()
            raw_isbns = list(master.keys())
            print(f"Refresh mode: {len(raw_isbns)} ISBN(s) from master.json.")
            if not raw_isbns:
                print("[ERROR] master.json has no ISBNs to refresh.")
                return 1
        else:
            print("[ERROR] Invalid menu choice. Please enter 1, 2, 3, or 0.")
            return 1

    valid_results = process_isbn_inputs(raw_isbns)
    if not valid_results:
        print("[ERROR] No valid ISBNs to process.")
        return 1

    # Keep Windows from sleeping / turning the screen off during long scrapes.
    with KeepAwake(verbose=True):
        run_scraping(valid_results, force_refresh=force_refresh)

    print()
    print("=" * 50)
    print("Run finished")
    print("=" * 50)
    print("Active sources: Goodreads, Amazon, Kobo, Audible, BookBub")
    print("Engine        : requests+BeautifulSoup -> Playwright fallback")
    print("Blurb folders : output/Blurb/<Source>_Blurb/")
    print("Docs          : see README.md")
    print("=" * 50)
    return 0


if __name__ == "__main__":
    sys.exit(run())
