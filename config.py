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
# 1) JSON_Master  -> one master.json with every ISBN and all five sources
# 2) JSON         -> five website subfolders, each with "<source> metadata.json"
# 3) Cover_Page   -> downloaded cover images
# 4) Blurb        -> blurb / description text files
# 5) Reviews      -> individual review text files
# 6) Preprocessing-> CSV logs (no separate Logs folder)
JSON_MASTER_DIR: Final[Path] = OUTPUT_DIR / "JSON_Master"
JSON_DIR: Final[Path] = OUTPUT_DIR / "JSON"
COVER_PAGE_DIR: Final[Path] = OUTPUT_DIR / "Cover_Page"
BLURB_DIR: Final[Path] = OUTPUT_DIR / "Blurb"
REVIEWS_DIR: Final[Path] = OUTPUT_DIR / "Reviews"
PREPROCESSING_DIR: Final[Path] = OUTPUT_DIR / "Preprocessing"

# ------------------------------------------------------------------------------
# FIVE WEBSITES (PL Assignment 1)
# ------------------------------------------------------------------------------
# Folder names under output/JSON/ use these display names.
# Filename source tokens are lowercase (see SOURCE_FILE_TOKENS).
SOURCES: Final[tuple[str, ...]] = (
    "Amazon",
    "Kobo",
    "Audible",
    "BookBub",
    "Goodreads",
)

# Lowercase tokens used inside cover/blurb/review filenames.
# Example: 9780143127550_cp_goodreads_1.jpg
SOURCE_FILE_TOKENS: Final[dict[str, str]] = {
    "Amazon": "amazon",
    "Kobo": "kobo",
    "Audible": "audible",
    "BookBub": "bookbub",
    "Goodreads": "goodreads",
}

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
# Cover : <isbn13>_cp_<source>_<n>.jpg
# Blurb : <isbn13>_b_<source>_<n>.txt
# Review: <isbn13>_r_<source>_<n>.txt
COVER_FILENAME_TEMPLATE: Final[str] = "{isbn13}_cp_{source}_{n}.jpg"
BLURB_FILENAME_TEMPLATE: Final[str] = "{isbn13}_b_{source}_{n}.txt"
REVIEW_FILENAME_TEMPLATE: Final[str] = "{isbn13}_r_{source}_{n}.txt"

# ------------------------------------------------------------------------------
# SCRAPING / RUNTIME SETTINGS
# ------------------------------------------------------------------------------
# Assignment constraint: delay 1–2 seconds between consecutive HTTP requests.
REQUEST_DELAY_SECONDS: Final[tuple[float, float]] = (1.0, 2.0)

# HTTP timeout (seconds) — used later by scrapers.
HTTP_TIMEOUT_SECONDS: Final[int] = 30

# How many times to retry a failed network request before logging and continuing.
MAX_RETRIES: Final[int] = 3

# Placeholder for any unavailable field (never delete the field).
MISSING_VALUE: Final[str] = "N/A"

# Default CSV count when the user presses Enter (they can type a number or "all").
CSV_ISBN_LIMIT: Final[int] = 20

# Target minimum reviews per source (assignment: at least 25 when available).
MIN_REVIEWS_PER_SOURCE: Final[int] = 25

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

    return directories
