"""
simranjeet singh ghuman

main.py by simranjeet singh ghuman

Run this file to start the scraper:

    python main.py

Flow:
1. Create output folders if needed
2. Ask for one ISBN or a CSV
3. For CSV: ask how many ISBNs (number or "all")
4. Normalize ISBN-13
5. For each ISBN ask if already scraped (rescrape / skip)
6. Scrape Amazon, Goodreads, Kobo, Audible, BookBub
7. Save JSON + covers / blurbs / reviews
8. Show progress like 04/20
"""

from __future__ import annotations

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
    ensure_isbn_placeholders,
    isbn_already_scraped,
    load_master_json,
    merge_source_record,
)
from utils.isbn import IsbnResult, normalize_isbn_list
from utils.media_saver import download_covers, save_blurb, save_reviews


def print_banner() -> None:
    """Print a clear terminal banner."""
    print("=" * 50)
    print("  BOOK WEB SCRAPER - Programming Lab Assignment 1")
    print("  Sources: Amazon | Kobo | Audible | BookBub | Goodreads")
    print("=" * 50)
    print()


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
    If this ISBN was scraped earlier, ask the user what to do.

    Returns
    -------
    bool
        True = scrape again, False = skip.
    """
    if not isbn_already_scraped(isbn13):
        return True

    print()
    print(f"ISBN {isbn13} was already scraped earlier.")
    print("  r = scrape again (overwrite/update this ISBN)")
    print("  s = skip this ISBN")
    choice = input("> ").strip().lower()
    if choice in {"r", "again", "yes", "y"}:
        return True
    print(f"Skipping {isbn13}")
    append_preprocessing_log(
        isbn13=isbn13,
        source="Input",
        issue_type="Already Scraped",
        details="User chose to skip re-scrape",
        action_taken="Skipped",
    )
    return False


def _best_hints_from_master(isbn13: str) -> tuple[str, str]:
    """Pull title/authors from any source that already succeeded for this ISBN."""
    master = load_master_json()
    record = master.get(isbn13) or {}
    title = ""
    authors = ""
    for source in ("Amazon", "Goodreads", "Kobo", "Audible", "BookBub"):
        block = record.get(source) or {}
        t = str(block.get("title", "")).strip()
        a = str(block.get("authors", "")).strip()
        if t and t != config.MISSING_VALUE and not title:
            title = t
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
            action_taken="Stored N/A and continued",
        )
        print("Failed")
        return False

    if not scraped.success:
        append_preprocessing_log(
            isbn13=isbn13,
            source=source,
            issue_type=(
                "Network Failure"
                if "could not extract" in scraped.error
                else "Parsing Failure"
            ),
            details=scraped.error or f"Unknown {source} failure",
            action_taken="Stored N/A and continued",
        )
        print("Failed")
        print(f"  reason: {scraped.error}")
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
    if len(review_paths) < config.MIN_REVIEWS_PER_SOURCE:
        append_preprocessing_log(
            isbn13=isbn13,
            source=source,
            issue_type="Reviews Shortfall",
            details=(
                f"Saved {len(review_paths)} reviews; "
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
            f"reviews={len(review_paths)}; "
            f"blurb={'yes' if blurb_path else 'no'}"
        ),
        action_taken="Master JSON + source JSON updated",
    )

    print("Completed")
    print(f"  method: {scraped.method_used}")
    print(f"  title: {scraped.fields.get('title', config.MISSING_VALUE)}")
    print(f"  covers saved: {len(cover_paths)}")
    print(f"  reviews saved: {len(review_paths)}")
    print(f"  blurb saved: {'yes' if blurb_path else 'no'}")
    return True


def run_scraping(valid_results: list[IsbnResult]) -> None:
    """Scrape all five websites for each ISBN, with progress XX/YY."""
    # Amazon + Goodreads first so later sites can use title/author hints.
    scrapers: list[BaseScraper] = [
        AmazonScraper(),
        GoodreadsScraper(),
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

        if not ask_rescrape_or_skip(result.isbn13):
            completed += 1
            print(f"Progress: {completed:02d}/{total:02d}")
            print("-" * 40)
            continue

        hint_title = ""
        hint_authors = ""

        for scraper in scrapers:
            # Refresh hints after each successful source.
            if not hint_title or not hint_authors:
                hint_title, hint_authors = _best_hints_from_master(result.isbn13)
            scrape_and_persist(
                result.isbn13,
                scraper,
                hint_title=hint_title,
                hint_authors=hint_authors,
            )
            # Update hints immediately from master after each site.
            hint_title, hint_authors = _best_hints_from_master(result.isbn13)

        completed += 1
        print()
        print("Master JSON Updated")
        print("Images / Reviews / Blurb saved when available")
        print(f"Progress: {completed:02d}/{total:02d}")
        print("-" * 40)


def run() -> int:
    """Program entry orchestrator."""
    print_banner()

    create_project_folders(verbose=True)
    ensure_preprocessing_csv_header(verbose=True)
    print()

    choice = show_menu()
    print()

    raw_isbns: list[str] = []

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
    else:
        print("[ERROR] Invalid menu choice. Please enter 1, 2, or 0.")
        return 1

    valid_results = process_isbn_inputs(raw_isbns)
    if not valid_results:
        print("[ERROR] No valid ISBNs to process.")
        return 1

    run_scraping(valid_results)

    print()
    print("=" * 50)
    print("Run finished")
    print("=" * 50)
    print("Active sources: Amazon, Goodreads, Kobo, Audible, BookBub")
    print("Engine        : requests+BeautifulSoup -> Playwright fallback")
    print("Docs          : see README.md")
    print("=" * 50)
    return 0


if __name__ == "__main__":
    sys.exit(run())
