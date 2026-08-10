"""
config.py

Holds paths, website names, file-name patterns, and small settings like
request delay and the CSV ISBN limit. Other modules import from here so we
do not hardcode the same values in five different scrapers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

# ------------------------------------------------------------------------------
# PROJECT ROOT
# ------------------------------------------------------------------------------
# config.py lives at the project root, so .parent of this file IS the root.
# Using resolve() converts any relative path into a full absolute path.
PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent

# ------------------------------------------------------------------------------
# TOP-LEVEL DIRECTORIES
# ------------------------------------------------------------------------------
# Agreed architecture: input/ for CSV drops, output/ for all scraped artifacts.
INPUT_DIR: Final[Path] = PROJECT_ROOT / "input"
OUTPUT_DIR: Final[Path] = PROJECT_ROOT / "output"

# ------------------------------------------------------------------------------
# SIX OUTPUT FOLDERS (exact set approved for this lab)
# ------------------------------------------------------------------------------
# 1) JSON_Master  -> one master.json with every ISBN and all sources
# 2) JSON         -> per-website subfolders, each with "<source> metadata.json"
# 3) Cover_Page   -> per-source cover image folders
# 4) Blurb        -> per-source blurb / description text folders
# 5) Reviews      -> per-source review text folders
# 6) Preprocessing-> CSV logs (no separate Logs folder)
JSON_MASTER_DIR: Final[Path] = OUTPUT_DIR / "JSON_Master"
JSON_DIR: Final[Path] = OUTPUT_DIR / "JSON"
COVER_PAGE_DIR: Final[Path] = OUTPUT_DIR / "Cover_Page"
BLURB_DIR: Final[Path] = OUTPUT_DIR / "Blurb"
REVIEWS_DIR: Final[Path] = OUTPUT_DIR / "Reviews"
PREPROCESSING_DIR: Final[Path] = OUTPUT_DIR / "Preprocessing"

# ------------------------------------------------------------------------------
# WEBSITES (PL Assignment 1 + Open Library)
# ------------------------------------------------------------------------------
# Folder names under output/JSON/ use these display names.
# JSON filename tokens are lowercase (see SOURCE_FILE_TOKENS).
SOURCES: Final[tuple[str, ...]] = (
    "Goodreads",
    "Amazon",
    "Kobo",
    "Audible",
    "BookBub",
    "OpenLibrary",
)

# Lowercase tokens used inside JSON metadata filenames only.
# Example: goodreads metadata.json
SOURCE_FILE_TOKENS: Final[dict[str, str]] = {
    "Amazon": "amazon",
    "Kobo": "kobo",
    "Audible": "audible",
    "BookBub": "bookbub",
    "Goodreads": "goodreads",
    "OpenLibrary": "openlibrary",
}

# Capitalized source tokens for Cover / Blurb / Review filenames.
# Example: 9780143127550_c_Amazon_1.jpg
ASSET_SOURCE_TOKENS: Final[dict[str, str]] = {
    "Amazon": "Amazon",
    "Kobo": "Kobo",
    "Audible": "Audible",
    "BookBub": "BookBub",
    "Goodreads": "Goodreads",
    "OpenLibrary": "OpenLibrary",
}

# Blurb filename source tokens (professor-required casing).
# Example: 9780131103627_b_Amazon_1.txt
BLURB_SOURCE_TOKENS: Final[dict[str, str]] = dict(ASSET_SOURCE_TOKENS)

# Per-source Blurb subfolders under output/Blurb/
BLURB_SOURCE_FOLDERS: Final[dict[str, str]] = {
    "Amazon": "Amazon_Blurb",
    "Kobo": "Kobo_Blurb",
    "Audible": "Audible_Blurb",
    "BookBub": "BookBub_Blurb",
    "Goodreads": "Goodreads_Blurb",
    "OpenLibrary": "OpenLibrary_Blurb",
}

# Per-source Cover subfolders under output/Cover_Page/
COVER_SOURCE_FOLDERS: Final[dict[str, str]] = {
    "Amazon": "Amazon_Cover",
    "Kobo": "Kobo_Cover",
    "Audible": "Audible_Cover",
    "BookBub": "BookBub_Cover",
    "Goodreads": "Goodreads_Cover",
    "OpenLibrary": "OpenLibrary_Cover",
}

# Per-source Reviews subfolders under output/Reviews/
REVIEWS_SOURCE_FOLDERS: Final[dict[str, str]] = {
    "Amazon": "Amazon_Reviews",
    "Kobo": "Kobo_Reviews",
    "Audible": "Audible_Reviews",
    "BookBub": "BookBub_Reviews",
    "Goodreads": "Goodreads_Reviews",
    "OpenLibrary": "OpenLibrary_Reviews",
}

# Fast Open Library API batching (used by --openlibrary-only).
OPENLIBRARY_BATCH_SIZE: Final[int] = 25
OPENLIBRARY_REQUEST_DELAY_SECONDS: Final[tuple[float, float]] = (0.15, 0.30)

# ------------------------------------------------------------------------------
# JSON FILE NAMING (PL Assignment 1.pdf — MANDATORY)
# ------------------------------------------------------------------------------
# Pattern from assignment:
#     <source> metadata.json
# Example:
#     goodreads metadata.json
#
# NOTE: There IS a space between <source> and "metadata.json".
# Master file name is our approved addition (not named in the PDF).
METADATA_JSON_FILENAME_TEMPLATE: Final[str] = "{source} metadata.json"
MASTER_JSON_FILENAME: Final[str] = "master.json"
MASTER_JSON_PATH: Final[Path] = JSON_MASTER_DIR / MASTER_JSON_FILENAME

# Preprocessing log (CSV) lives inside Preprocessing/ only.
PREPROCESSING_CSV_FILENAME: Final[str] = "preprocessing_report.csv"
PREPROCESSING_CSV_PATH: Final[Path] = PREPROCESSING_DIR / PREPROCESSING_CSV_FILENAME

# ------------------------------------------------------------------------------
# MEDIA / TEXT FILE NAMING (underscores — approved)
# ------------------------------------------------------------------------------
# Cover : <isbn13>_c_<Source>_<n>.jpg   (capitalized Source)
# Blurb : <isbn13>_b_<Source>_<n>.txt   (capitalized Source)
# Review: <isbn13>_r_<Source>_<n>.txt   (one file per review)
COVER_FILENAME_TEMPLATE: Final[str] = "{isbn13}_c_{source}_{n}.jpg"
BLURB_FILENAME_TEMPLATE: Final[str] = "{isbn13}_b_{source}_{n}.txt"
REVIEW_FILENAME_TEMPLATE: Final[str] = "{isbn13}_r_{source}_{n}.txt"

# ------------------------------------------------------------------------------
# SCRAPING / RUNTIME SETTINGS
# ------------------------------------------------------------------------------
# Assignment constraint: delay 1–2 seconds between consecutive HTTP requests.
# Stay inside the required range but prefer the low end for lab runtime.
REQUEST_DELAY_SECONDS: Final[tuple[float, float]] = (1.0, 1.25)

# HTTP timeout (seconds) — used later by scrapers / Playwright navigation.
HTTP_TIMEOUT_SECONDS: Final[int] = 45

# Playwright navigation timeout (ms). Keep below HTTP timeout so pages fail fast.
PLAYWRIGHT_NAV_TIMEOUT_MS: Final[int] = 25000

# Hard watchdog: if one Playwright operation exceeds this, kill/reset the browser
# so the whole batch cannot freeze on a hung Goodreads/Amazon page.
PLAYWRIGHT_HARD_TIMEOUT_SECONDS: Final[int] = 70

# How many times to retry a failed network request before logging and continuing.
MAX_RETRIES: Final[int] = 2

# Placeholder for any unavailable field (never delete the field).
MISSING_VALUE: Final[str] = "N/A"

# Default CSV count when the user presses Enter (they can type a number or "all").
CSV_ISBN_LIMIT: Final[int] = 20

# Target minimum reviews per source when the site has that many available.
# (Lowered from 25 for runtime; still saves one file per review under Reviews/.)
MIN_REVIEWS_PER_SOURCE: Final[int] = 10

# ------------------------------------------------------------------------------
# METADATA FIELD ORDER (shared schema for master + per-source JSON)
# ------------------------------------------------------------------------------
# Genres are stored INSIDE JSON as a comma-separated string.
# There is NO separate genres folder.
METADATA_FIELDS: Final[tuple[str, ...]] = (
    "isbn13",
    "title",
    "subtitle",
    "authors",
    "publisher",
    "origin_country",
    "publication_date",
    "language",
    "pages",
    "format",
    "series",
    "edition",
    "description",
    "price",
    "url",
    "rating",
    "ratings_count",
    "genres",  # comma-separated, e.g. "Fantasy, Adventure"
    "source",
)


def metadata_json_filename(source: str) -> str:
    """
    Build the PL Assignment 1 metadata JSON filename for one source.

    Parameters
    ----------
    source : str
        Display source name, e.g. "Goodreads".

    Returns
    -------
    str
        Filename such as "goodreads metadata.json".
    """
    token = SOURCE_FILE_TOKENS[source]
    return METADATA_JSON_FILENAME_TEMPLATE.format(source=token)


def source_json_path(source: str) -> Path:
    """
    Full path to one website's metadata JSON file.

    Example
    -------
    output/JSON/Goodreads/goodreads metadata.json
    """
    return JSON_DIR / source / metadata_json_filename(source)


def all_output_directories() -> list[Path]:
    """
    Return every directory that must exist before scraping starts.

    Includes:
        - input/
        - the six output folders
        - five JSON/<Source>/ subfolders
        - Cover / Blurb / Reviews per-source subfolders

    Returns
    -------
    list[Path]
        Deduplicated list of directory paths to create.
    """
    directories: list[Path] = [
        INPUT_DIR,
        OUTPUT_DIR,
        JSON_MASTER_DIR,
        JSON_DIR,
        COVER_PAGE_DIR,
        BLURB_DIR,
        REVIEWS_DIR,
        PREPROCESSING_DIR,
    ]

    # One subfolder per website under output/JSON/
    for source in SOURCES:
        directories.append(JSON_DIR / source)

    # Professor-required asset subfolders:
    # output/Blurb/Amazon_Blurb/, Cover_Page/Amazon_Cover/, Reviews/Amazon_Reviews/
    for source in SOURCES:
        directories.append(BLURB_DIR / BLURB_SOURCE_FOLDERS[source])
        directories.append(COVER_PAGE_DIR / COVER_SOURCE_FOLDERS[source])
        directories.append(REVIEWS_DIR / REVIEWS_SOURCE_FOLDERS[source])

    return directories


def blurb_dir_for_source(source: str) -> Path:
    """Return output/Blurb/<Source>_Blurb/ for one website."""
    return BLURB_DIR / BLURB_SOURCE_FOLDERS[source]


def cover_dir_for_source(source: str) -> Path:
    """Return output/Cover_Page/<Source>_Cover/ for one website."""
    return COVER_PAGE_DIR / COVER_SOURCE_FOLDERS[source]


def reviews_dir_for_source(source: str) -> Path:
    """Return output/Reviews/<Source>_Reviews/ for one website."""
    return REVIEWS_DIR / REVIEWS_SOURCE_FOLDERS[source]
