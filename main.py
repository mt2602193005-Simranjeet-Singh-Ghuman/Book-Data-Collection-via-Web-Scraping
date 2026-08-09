"""
simranjeet singh ghuman

main.py by simranjeet singh ghuman

Run this file to start the scraper:

    python main.py
    python main.py --goodreads-only   # Phase 1: Goodreads only (then pick menu)
    python main.py --source Goodreads # same as --goodreads-only
    python main.py --refresh          # re-scrape ISBNs already in master.json
    python main.py --refresh 9780...  # refresh one ISBN

Menu modes:
1. Single ISBN
2. First N rows from CSV
3. Inclusive CSV range (start–end)
4. Entire CSV
5. Refresh already-scraped ISBNs

Pipeline (viva):
  ISBN -> Goodreads (canonical title) -> Amazon (ISBN/hints)
       -> Kobo / Audible / BookBub search by TITLE ONLY
       -> author used only as secondary validation
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
    ensure_isbn_placeholders_batch,
    isbn_already_scraped,
    load_master_json,
    merge_source_record,
)
from utils.isbn import IsbnResult, normalize_isbn_list
from utils.keep_awake import KeepAwake
from utils.media_saver import download_covers, save_blurb, save_reviews
from utils.title_match import (
    build_title_query_variants,
    clean_hint_title,
    titles_roughly_match,
)


def print_banner() -> None:
    """Print a clear terminal banner."""
    print("=" * 50)
    print("  BOOK WEB SCRAPER - Programming Lab Assignment 1")
    print("  Sources: Goodreads | Amazon | Kobo | Audible | BookBub")
    print("=" * 50)
    print()


def ensure_playwright_ready(verbose: bool = True) -> bool:
    """
    Start the shared Playwright Chromium used by all Level-2 fetches.
    """
    from scraper.browser_pool import start_shared_browser

    return start_shared_browser(verbose=verbose)


def prompt_manual_isbn() -> str:
    """Ask the user to type one ISBN-10 or ISBN-13."""
    print("Manual ISBN entry selected.")
    print("Enter ISBN-10 or ISBN-13 (you may include hyphens):")
    return input("> ").strip()


def prompt_csv_path() -> Path:
    """Ask for a CSV path, or Enter for default input/2602193005.csv."""
    default_csv = config.INPUT_DIR / "2602193005.csv"
    root_csv = config.PROJECT_ROOT / "2602193005.csv"
    if not default_csv.exists() and root_csv.exists():
        default_csv = root_csv
    print("CSV input selected.")
    print(f"Press Enter to use default: {default_csv}")
    print("Or type a full / relative CSV path:")
    typed = input("> ").strip()
    if not typed:
        return default_csv
    return Path(typed).expanduser().resolve()


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


def _is_header_row(line_number: int, value: str) -> bool:
    if line_number != 1:
        return False
    lowered = value.replace("-", "").lower()
    if lowered in {"isbn13", "isbn", "isbn_13"}:
        return True
    return "isbn" in value.lower() and not any(ch.isdigit() for ch in value)


def load_isbns_from_csv(csv_path: Path, limit: int) -> list[str]:
    """Read the first `limit` ISBN data rows from a CSV file."""
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    print("Reading CSV...")
    isbns: list[str] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for line_number, line in enumerate(handle, start=1):
            value = line.strip().strip(",")
            if not value or _is_header_row(line_number, value):
                continue
            isbns.append(value)
            if len(isbns) >= limit:
                break

    if not isbns:
        raise ValueError(f"No ISBN rows found in: {csv_path}")

    print(f"{len(isbns)} ISBNs loaded from CSV (first {limit})")
    return isbns


def load_isbns_from_csv_range(csv_path: Path, start: int, end: int) -> list[str]:
    """
    Read inclusive 1-based data-row range [start, end] from a CSV.

    Example: start=3, end=7 loads data rows 3 through 7 (not file line numbers).
    """
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")
    if start < 1 or end < start:
        raise ValueError(f"Invalid range: start={start}, end={end}")

    print(f"Reading CSV rows {start}–{end} (inclusive)...")
    isbns: list[str] = []
    data_index = 0
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for line_number, line in enumerate(handle, start=1):
            value = line.strip().strip(",")
            if not value or _is_header_row(line_number, value):
                continue
            data_index += 1
            if data_index < start:
                continue
            if data_index > end:
                break
            isbns.append(value)

    if not isbns:
        raise ValueError(
            f"No ISBN rows found in range {start}–{end} for: {csv_path}"
        )
    print(f"{len(isbns)} ISBNs loaded from CSV range {start}–{end}")
    return isbns


def prompt_first_n(total_available: int) -> int:
    """Ask how many leading CSV rows to process (First N)."""
    print()
    print(f"First N mode — CSV has {total_available} data rows.")
    print("Enter N (positive integer), or press Enter for default "
          f"({config.CSV_ISBN_LIMIT}):")
    typed = input("> ").strip().lower()
    if typed == "":
        return min(config.CSV_ISBN_LIMIT, total_available)
    if typed.isdigit() and int(typed) > 0:
        return min(int(typed), total_available)
    print(f"[WARN] Invalid value. Using default {config.CSV_ISBN_LIMIT}.")
    return min(config.CSV_ISBN_LIMIT, total_available)


def prompt_inclusive_range(total_available: int) -> tuple[int, int]:
    """
    Ask for inclusive 1-based start/end data-row indexes.

    Validates: 1 <= start <= end <= available.
    """
    print()
    print(f"Inclusive range mode — CSV has {total_available} data rows.")
    print("Enter start row (1-based data row):")
    start_raw = input("> ").strip()
    print("Enter end row (inclusive):")
    end_raw = input("> ").strip()
    if not (start_raw.isdigit() and end_raw.isdigit()):
        raise ValueError("Start and end must be positive integers.")
    start = int(start_raw)
    end = int(end_raw)
    if not (1 <= start <= end <= total_available):
        raise ValueError(
            f"Range must satisfy 1 <= start <= end <= {total_available} "
            f"(got start={start}, end={end})."
        )
    return start, end


def show_menu() -> str:
    """Display the input-method menu and return the user's choice."""
    print("Select run mode:")
    print("  1) Single ISBN (manual entry)")
    print("  2) First N ISBNs from CSV")
    print("  3) Inclusive CSV range (start–end)")
    print("  4) Entire CSV")
    print("  5) Refresh already-scraped ISBNs in master.json")
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

    # One load/save cycle for all placeholders (critical for 1000+ ISBN runs).
    # Per-ISBN master.json rewrites previously crashed Windows with Errno 22.
    if valid_results:
        print(f"Creating JSON placeholders for {len(valid_results)} ISBNs...")
        ensure_isbn_placeholders_batch([result.isbn13 for result in valid_results])
        print("[OK] Placeholders saved to master.json + source JSON files")

    verbose_each = len(valid_results) <= 50
    for index, result in enumerate(valid_results, start=1):
        append_preprocessing_log(
            isbn13=result.isbn13,
            source="Input",
            issue_type="ISBN Normalized",
            details=result.detail,
            action_taken="Stored ISBN-13 skeleton in master + source JSON files",
        )
        if verbose_each:
            converted = "yes" if result.was_converted_from_isbn10 else "no"
            print(
                f"[OK] {result.original} -> {result.isbn13} "
                f"(from ISBN-10: {converted})"
            )
        elif index == 1 or index == len(valid_results) or index % 100 == 0:
            print(f"[OK] Normalized {index}/{len(valid_results)} ISBNs...")

    print("-" * 40)
    print(f"Valid ISBNs ready: {len(valid_results)}")
    print(f"Skipped invalid/duplicate: {len(invalid_pairs)}")
    return valid_results


def _source_has_title(isbn13: str, source: str) -> bool:
    """True when master.json already has a real title for this source."""
    master = load_master_json()
    record = master.get(isbn13)
    if not isinstance(record, dict):
        return False
    block = record.get(source) or {}
    title = str(block.get("title", config.MISSING_VALUE)).strip()
    return bool(title) and title != config.MISSING_VALUE


def ask_rescrape_or_skip(
    isbn13: str,
    *,
    only_sources: list[str] | None = None,
) -> bool:
    """
    Skip ISBNs that already have real data (faster batches).

    - All-sites mode: skip if any source already has a title.
    - Single-source mode (--source / --goodreads-only): skip only if THAT
      source already has a title.

    Use menu option 5 / --refresh when you intentionally want to re-scrape.
    """
    if only_sources and len(only_sources) == 1:
        source = only_sources[0]
        if _source_has_title(isbn13, source):
            print(
                f"[SKIP] {source} already has data for {isbn13} "
                f"— use Refresh to re-scrape."
            )
            append_preprocessing_log(
                isbn13=isbn13,
                source="Input",
                issue_type="Already Scraped",
                details=f"Skipped; {source} already has non-N/A title",
                action_taken="Skipped",
            )
            return False
        return True

    if isbn_already_scraped(isbn13):
        print(f"[SKIP] ISBN {isbn13} already in master — use Refresh to re-scrape.")
        append_preprocessing_log(
            isbn13=isbn13,
            source="Input",
            issue_type="Already Scraped",
            details="Skipped; already has non-N/A title in master.json",
            action_taken="Skipped",
        )
        return False
    return True


def _best_hints_from_master(isbn13: str) -> tuple[str, str]:
    """
    Pull canonical title/authors — Goodreads first (primary discovery source).
    """
    hints = _discovery_hints_from_master(isbn13)
    return hints["title"], hints["authors"]


def _discovery_hints_from_master(isbn13: str) -> dict:
    """
    Build discovery hints after Goodreads / Amazon scrapes.

    When BOTH Goodreads and Amazon have matching titles, Kobo / Audible /
    BookBub search more aggressively (title variants + optional author query).
    Values are still extracted from each target site — not copied from GR/Amazon.
    """
    master = load_master_json()
    record = master.get(isbn13) or {}
    gr = record.get("Goodreads") or {}
    am = record.get("Amazon") or {}

    gr_title_raw = str(gr.get("title", "")).strip()
    am_title_raw = str(am.get("title", "")).strip()
    gr_title = clean_hint_title(gr_title_raw) if gr_title_raw not in {"", config.MISSING_VALUE} else ""
    am_title = clean_hint_title(am_title_raw) if am_title_raw not in {"", config.MISSING_VALUE} else ""

    gr_authors = str(gr.get("authors", "")).strip()
    am_authors = str(am.get("authors", "")).strip()
    if gr_authors == config.MISSING_VALUE:
        gr_authors = ""
    if am_authors == config.MISSING_VALUE:
        am_authors = ""

    dual_confirmed = bool(
        gr_title
        and am_title
        and titles_roughly_match(gr_title, am_title)
    )
    primary = gr_title or am_title
    authors = gr_authors or am_authors
    titles = build_title_query_variants(gr_title, am_title, primary)
    if not titles and primary:
        titles = [primary]

    return {
        "title": primary,
        "authors": authors,
        "titles": titles,
        "dual_confirmed": dual_confirmed,
    }


def scrape_and_persist(
    isbn13: str,
    scraper: BaseScraper,
    *,
    hint_title: str = "",
    hint_authors: str = "",
    hint_titles: list[str] | None = None,
    allow_author_query: bool = False,
) -> bool:
    """
    Scrape one website for one ISBN and persist JSON + media files.

    Returns True if scrape succeeded.
    """
    source = scraper.source_name
    title_search_sources = {"Kobo", "Audible", "BookBub"}
    if source in title_search_sources and (hint_titles or hint_title):
        shown = (hint_titles or [hint_title])[:3]
        confirm = " (GR+Amazon confirmed)" if allow_author_query else ""
        print(f"[{source}] SEARCHING BY TITLE{confirm}: {shown!r}")
    else:
        print(f"[{source}] SEARCHING...")

    try:
        scrape_kwargs = {
            "hint_title": hint_title,
            "hint_authors": hint_authors,
        }
        # Newer discovery args (Kobo / Audible / BookBub); ignore if unsupported.
        if source in title_search_sources:
            scrape_kwargs["hint_titles"] = hint_titles or (
                [hint_title] if hint_title else []
            )
            scrape_kwargs["allow_author_query"] = allow_author_query
        scraped = scraper.scrape(isbn13, **scrape_kwargs)
    except Exception as exc:  # noqa: BLE001
        append_preprocessing_log(
            isbn13=isbn13,
            source=source,
            issue_type="Parsing Failure",
            details=str(exc),
            action_taken="Kept previous values (if any) and continued",
        )
        print(f"[{source}] Failed (exception) — continuing")
        return False

    if not scraped.success:
        error_text = scraped.error or f"Unknown {source} failure"
        if "AMBIGUOUS_TITLE_MATCH" in error_text:
            issue_type = "AMBIGUOUS_TITLE_MATCH"
            action = "Left fields N/A (refused wrong-book match) and continued"
            print(f"[{source}] AMBIGUOUS_TITLE_MATCH — not saving wrong book")
        elif "could not extract" in error_text.lower() or "could not fetch" in error_text.lower():
            issue_type = "Network Failure"
            action = "Kept previous values (if any) and continued"
            print(f"[{source}] Failed — continuing")
        else:
            issue_type = "Parsing Failure"
            action = "Kept previous values (if any) and continued"
            print(f"[{source}] Failed — continuing")
        append_preprocessing_log(
            isbn13=isbn13,
            source=source,
            issue_type=issue_type,
            details=error_text,
            action_taken=action,
        )
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
            f"reviews={len(review_paths)}; "
            f"blurb={'yes' if blurb_path else 'no'}"
        ),
        action_taken="Master JSON + source JSON updated",
    )

    found_title = scraped.fields.get("title", config.MISSING_VALUE)
    print(f"[{source}] FOUND: {found_title}")
    print(
        f"  method={scraped.method_used} | "
        f"covers={len(cover_paths)} | "
        f"reviews={len(review_paths)} | "
        f"blurb={'yes' if blurb_path else 'no'}"
    )
    return True


def _all_scrapers() -> list[BaseScraper]:
    """Default scrape order (Goodreads first for discovery)."""
    return [
        GoodreadsScraper(),
        AmazonScraper(),
        KoboScraper(),
        AudibleScraper(),
        BookBubScraper(),
    ]


def run_scraping(
    valid_results: list[IsbnResult],
    *,
    force_refresh: bool = False,
    only_sources: list[str] | None = None,
) -> None:
    """
    Scrape websites for each ISBN.

    Order is intentional: Goodreads first supplies the canonical title used by
    Kobo / Audible / BookBub title-only discovery.

    only_sources
        If set (e.g. ["Goodreads"]), scrape only those sites.
    """
    scrapers = _all_scrapers()
    if only_sources:
        wanted = {name.lower(): name for name in only_sources}
        scrapers = [s for s in scrapers if s.source_name.lower() in wanted]
        if not scrapers:
            print(f"[ERROR] No scrapers matched sources: {only_sources}")
            return

    active_names = [s.source_name for s in scrapers]
    print(f"[MODE] Scraping source(s): {', '.join(active_names)}")

    total = len(valid_results)
    completed = 0

    for result in valid_results:
        print()
        print("-" * 40)
        print(f"ISBN {result.isbn13}")
        print()

        if force_refresh:
            print(f"[REFRESH] Re-scraping: {', '.join(active_names)}...")
        elif not ask_rescrape_or_skip(result.isbn13, only_sources=only_sources):
            completed += 1
            print(f"Progress: {completed:02d}/{total:02d}")
            print("-" * 40)
            continue

        # Sites that failed before any source had a title get one retry after GR/Amazon.
        needs_retry: list[BaseScraper] = []

        for scraper in scrapers:
            hints = _discovery_hints_from_master(result.isbn13)
            had_metadata = bool(hints["title"])
            if scraper.source_name == "Goodreads":
                print("[Goodreads] PRIMARY discovery by ISBN...")
            elif scraper.source_name == "Amazon":
                print("[Amazon] ISBN / title discovery...")
            elif (
                scraper.source_name in {"Kobo", "Audible", "BookBub"}
                and hints["dual_confirmed"]
            ):
                print(
                    f"[{scraper.source_name}] GR+Amazon confirmed "
                    f"→ search title variants (+ author) for cover/blurb..."
                )
            ok = scrape_and_persist(
                result.isbn13,
                scraper,
                hint_title=hints["title"],
                hint_authors=hints["authors"],
                hint_titles=hints["titles"],
                allow_author_query=bool(hints["dual_confirmed"]),
            )
            if not ok and not had_metadata:
                needs_retry.append(scraper)

        # Retry only makes sense when more than one site can supply a title.
        if len(scrapers) > 1:
            hints = _discovery_hints_from_master(result.isbn13)
            if needs_retry and hints["title"]:
                print(
                    f"[RETRY] Using confirmed title {hints['title']!r} "
                    f"for earlier misses..."
                )
                for scraper in needs_retry:
                    scrape_and_persist(
                        result.isbn13,
                        scraper,
                        hint_title=hints["title"],
                        hint_authors=hints["authors"],
                        hint_titles=hints["titles"],
                        allow_author_query=bool(hints["dual_confirmed"]),
                    )

        completed += 1
        print()
        print("Master JSON Updated")
        print("Assets: Cover_Page/<Source>_Cover | Blurb/<Source>_Blurb | Reviews/<Source>_Reviews")
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
            "Re-scrape ISBNs already in master.json. "
            "Pass optional ISBN(s); with no ISBN, refresh every ISBN in master.json. "
            "Combine with --source / --goodreads-only to refresh one site only."
        ),
    )
    parser.add_argument(
        "--source",
        choices=list(config.SOURCES),
        metavar="SOURCE",
        help=(
            "Scrape only one website. "
            "Choices: Goodreads, Amazon, Kobo, Audible, BookBub."
        ),
    )
    parser.add_argument(
        "--goodreads-only",
        action="store_true",
        help="Shortcut for --source Goodreads (Phase 1).",
    )
    return parser.parse_args(argv)


def _load_from_csv_menu(choice: str) -> list[str]:
    """Shared CSV path prompt + mode-specific slice (2/3/4)."""
    csv_path = prompt_csv_path()
    total_available = count_csv_isbns(csv_path)
    if total_available <= 0:
        raise ValueError("No ISBN rows found in CSV.")

    if choice == "2":
        limit = prompt_first_n(total_available)
        return load_isbns_from_csv(csv_path, limit=limit)
    if choice == "3":
        start, end = prompt_inclusive_range(total_available)
        return load_isbns_from_csv_range(csv_path, start=start, end=end)
    if choice == "4":
        print(f"Entire CSV mode — loading all {total_available} rows...")
        return load_isbns_from_csv(csv_path, limit=total_available)
    raise ValueError(f"Unsupported CSV menu choice: {choice}")


def run(argv: list[str] | None = None) -> int:
    """Program entry orchestrator."""
    args = _parse_cli_args(list(argv) if argv is not None else sys.argv[1:])

    only_sources: list[str] | None = None
    if args.goodreads_only and args.source and args.source != "Goodreads":
        print("[ERROR] Use either --goodreads-only or --source ..., not both.")
        return 1
    if args.goodreads_only:
        only_sources = ["Goodreads"]
    elif args.source:
        only_sources = [args.source]

    print_banner()
    if only_sources:
        print(f"Source filter: {', '.join(only_sources)} only")
        print()

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
        elif choice in {"2", "3", "4"}:
            try:
                raw_isbns = _load_from_csv_menu(choice)
            except (FileNotFoundError, ValueError) as exc:
                print(f"[ERROR] {exc}")
                return 1
        elif choice == "5":
            force_refresh = True
            master = load_master_json()
            raw_isbns = list(master.keys())
            print(f"Refresh mode: {len(raw_isbns)} ISBN(s) from master.json.")
            if not raw_isbns:
                print("[ERROR] master.json has no ISBNs to refresh.")
                return 1
        else:
            print("[ERROR] Invalid menu choice. Please enter 0–5.")
            return 1

    valid_results = process_isbn_inputs(raw_isbns)
    if not valid_results:
        print("[ERROR] No valid ISBNs to process.")
        return 1

    from scraper.browser_pool import stop_shared_browser

    try:
        # Keep Windows from sleeping / turning the screen off during long scrapes.
        with KeepAwake(verbose=True):
            run_scraping(
                valid_results,
                force_refresh=force_refresh,
                only_sources=only_sources,
            )
    finally:
        stop_shared_browser()

    active = ", ".join(only_sources) if only_sources else (
        "Goodreads, Amazon, Kobo, Audible, BookBub"
    )
    print()
    print("=" * 50)
    print("Run finished")
    print("=" * 50)
    print(f"Active sources: {active}")
    print("Engine        : requests+BeautifulSoup -> shared Playwright")
    print("Covers        : output/Cover_Page/<Source>_Cover/  (*_c_<Source>_N.jpg)")
    print("Blurbs        : output/Blurb/<Source>_Blurb/       (*_b_<Source>_N.txt)")
    print("Reviews       : output/Reviews/<Source>_Reviews/   (*_r_<Source>_N.txt)")
    print("Docs          : see README.md")
    print("=" * 50)
    return 0


if __name__ == "__main__":
    sys.exit(run())
