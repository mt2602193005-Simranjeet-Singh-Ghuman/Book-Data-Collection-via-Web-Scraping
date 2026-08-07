"""
utils/io_handlers.py

Read/write master.json and the five "<source> metadata.json" files.
Also appends rows to the preprocessing CSV.

Rules we stick to:
- Missing fields stay as "N/A"
- Updating one website never wipes another website's block in master.json
- Genres live in JSON as a comma-separated string
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Iterable

import config


JsonDict = dict[str, Any]


def _atomic_write_text(path: Path, text: str, *, retries: int = 6) -> None:
    """
    Write text safely on Windows (avoids Errno 22 from locked/partial overwrites).

    Writes to a sibling .tmp file, then replaces the destination. Retries briefly
    if antivirus / Explorer briefly locks the file during large batch runs.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    last_error: OSError | None = None
    for attempt in range(retries):
        try:
            tmp_path.write_text(text, encoding="utf-8", newline="\n")
            os.replace(tmp_path, path)
            return
        except OSError as exc:
            last_error = exc
            time.sleep(0.15 * (attempt + 1))
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
    # Final fallback: direct write (still better than crashing the whole batch).
    try:
        path.write_text(text, encoding="utf-8", newline="\n")
    except OSError:
        if last_error is not None:
            raise last_error
        raise


def empty_source_record(isbn13: str, source: str) -> JsonDict:
    """
    Build one website metadata object with every required field set to N/A.

    Parameters
    ----------
    isbn13 : str
        Normalized ISBN-13.
    source : str
        Website display name, e.g. "Goodreads".

    Returns
    -------
    dict
        Complete field dictionary ready for JSON serialization.
    """
    record: JsonDict = {}
    for field in config.METADATA_FIELDS:
        if field == "isbn13":
            record[field] = isbn13
        elif field == "source":
            record[field] = source
        else:
            record[field] = config.MISSING_VALUE
    return record


def empty_master_isbn_record(isbn13: str) -> JsonDict:
    """
    Build one master.json ISBN record containing all five empty source blocks.

    Parameters
    ----------
    isbn13 : str
        Normalized ISBN-13.

    Returns
    -------
    dict
        {
          "isbn13": "...",
          "Amazon": {...},
          ...
        }
    """
    record: JsonDict = {"isbn13": isbn13}
    for source in config.SOURCES:
        record[source] = empty_source_record(isbn13, source)
    return record


def load_json_file(path: Path) -> JsonDict:
    """
    Load a JSON object from disk; return {} if the file is missing or empty.

    Parameters
    ----------
    path : Path
        JSON file path.

    Returns
    -------
    dict
        Parsed JSON object (always a dict for our project files).

    Missing or broken files return {} so the run can continue.
    """
    if not path.exists():
        return {}

    try:
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return {}
        data = json.loads(text)
        if not isinstance(data, dict):
            # Our project stores top-level objects keyed by ISBN, not arrays.
            return {}
        return data
    except (OSError, json.JSONDecodeError):
        return {}


def save_json_file(path: Path, data: JsonDict) -> None:
    """Write a JSON object with UTF-8 encoding and indent=2."""
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    _atomic_write_text(path, text)


ISBN_BLOCK_SEPARATOR: str = "------------------"


def _dump_json_with_isbn_gaps(data: JsonDict) -> str:
    """
    Pretty JSON with a dash line between each top-level ISBN block.

    Example (as seen in an editor):
        "9780...": { ... Amazon/Kobo/... },

        ------------------

        "9781...": { ... },

    The dash lines are stripped again when loading so the file still parses.
    """
    if not data:
        return "{}\n"

    chunks: list[str] = ["{"]
    items = list(data.items())
    for index, (key, value) in enumerate(items):
        piece = json.dumps({key: value}, indent=2, ensure_ascii=False)
        # piece looks like: {\n  "isbn": {\n    ...\n  }\n}
        # Strip outer braces and re-indent body under the root object.
        inner = piece.strip()
        if inner.startswith("{") and inner.endswith("}"):
            inner = inner[1:-1].strip("\n")
        if index < len(items) - 1:
            # Keep comma on the closing brace:  },
            # then a clear dash line before the next ISBN.
            chunks.append(inner + ",")
            chunks.append("")
            chunks.append(ISBN_BLOCK_SEPARATOR)
            chunks.append("")
        else:
            chunks.append(inner)
    chunks.append("}")
    return "\n".join(chunks) + "\n"


def _strip_isbn_separators(text: str) -> str:
    """Remove visual ------------------ lines so json.loads can parse master.json."""
    kept: list[str] = []
    for line in text.splitlines():
        if line.strip() == ISBN_BLOCK_SEPARATOR:
            continue
        kept.append(line)
    return "\n".join(kept)


def load_master_json() -> JsonDict:
    """Load output/JSON_Master/master.json (or {} if not created yet)."""
    path = config.MASTER_JSON_PATH
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return {}
        data = json.loads(_strip_isbn_separators(text))
        if not isinstance(data, dict):
            return {}
        return data
    except (OSError, json.JSONDecodeError):
        return {}


def save_master_json(data: JsonDict) -> None:
    """Save master.json with ------------------ between each ISBN record."""
    _atomic_write_text(config.MASTER_JSON_PATH, _dump_json_with_isbn_gaps(data))


def isbn_already_scraped(isbn13: str) -> bool:
    """
    Return True if master.json already has real data for this ISBN
    (any source title that is not N/A).
    """
    master = load_master_json()
    record = master.get(isbn13)
    if not isinstance(record, dict):
        return False
    for source in config.SOURCES:
        block = record.get(source) or {}
        title = str(block.get("title", config.MISSING_VALUE)).strip()
        if title and title != config.MISSING_VALUE:
            return True
    return False


def load_source_json(source: str) -> JsonDict:
    """
    Load one website metadata JSON file.

    Example path
    ------------
    output/JSON/Goodreads/goodreads metadata.json
    """
    return load_json_file(config.source_json_path(source))


def save_source_json(source: str, data: JsonDict) -> None:
    """Save one website metadata JSON file using assignment naming."""
    save_json_file(config.source_json_path(source), data)


def _ensure_isbn_in_master(master: JsonDict, isbn13: str) -> bool:
    """Insert/repair one ISBN skeleton in an in-memory master dict. True if changed."""
    changed = False
    if isbn13 not in master:
        master[isbn13] = empty_master_isbn_record(isbn13)
        return True
    master_record = master[isbn13]
    if not isinstance(master_record, dict):
        master[isbn13] = empty_master_isbn_record(isbn13)
        return True
    if master_record.get("isbn13") != isbn13:
        master_record["isbn13"] = isbn13
        changed = True
    for source in config.SOURCES:
        if source not in master_record:
            master_record[source] = empty_source_record(isbn13, source)
            changed = True
    return changed


def ensure_isbn_placeholders_batch(isbn13_list: Iterable[str]) -> None:
    """
    Create N/A skeletons for many ISBNs with one load/save cycle per file.

    Important for large CSV ranges (e.g. 1–1000): the old per-ISBN save loop
    rewrote master.json thousands of times and crashed Windows with Errno 22.
    """
    isbn13_list = [str(x).strip() for x in isbn13_list if str(x).strip()]
    if not isbn13_list:
        return

    master = load_master_json()
    master_changed = False
    for isbn13 in isbn13_list:
        if _ensure_isbn_in_master(master, isbn13):
            master_changed = True
    if master_changed:
        save_master_json(master)

    for source in config.SOURCES:
        source_data = load_source_json(source)
        source_changed = False
        for isbn13 in isbn13_list:
            if isbn13 not in source_data:
                source_data[isbn13] = empty_source_record(isbn13, source)
                source_changed = True
        if source_changed:
            save_source_json(source, source_data)


def ensure_isbn_placeholders(isbn13: str) -> None:
    """
    Ensure master.json and all five source JSON files contain this ISBN.

    If the ISBN already exists, existing values are preserved.
    If it is new, an all-N/A skeleton is inserted.
    """
    ensure_isbn_placeholders_batch([isbn13])


def merge_source_record(
    isbn13: str,
    source: str,
    scraped_fields: JsonDict,
    *,
    overwrite_existing_values: bool = True,
) -> None:
    """
    Merge one website's scraped fields into master + that source JSON.

    IMPORTANT MERGE RULE:
        Updating `source` never removes or overwrites another website's block
        inside master.json.

    Parameters
    ----------
    isbn13 : str
        Normalized ISBN-13.
    source : str
        One of config.SOURCES.
    scraped_fields : dict
        Partial or full field dictionary from a scraper.
    overwrite_existing_values : bool, default True
        If True, non-empty scraped values replace old values for THIS source.
        Values equal to N/A do not wipe a previously good value.
    """
    if source not in config.SOURCES:
        raise ValueError(f"Unknown source: {source}")

    ensure_isbn_placeholders(isbn13)

    # Start from current source record, then apply scraped fields carefully.
    master = load_master_json()
    current = dict(master[isbn13][source])

    for key, value in scraped_fields.items():
        if key not in current and key not in config.METADATA_FIELDS:
            # Ignore unexpected keys to keep schema stable for demos.
            continue

        if value is None:
            continue

        text = str(value).strip()
        if text == "" or text == config.MISSING_VALUE:
            # Do not overwrite a previously filled value with N/A/empty.
            current.setdefault(key, config.MISSING_VALUE)
            continue

        if overwrite_existing_values or current.get(key, config.MISSING_VALUE) in {
            config.MISSING_VALUE,
            "",
            None,
        }:
            current[key] = text

    # Force identity fields.
    current["isbn13"] = isbn13
    current["source"] = source

    # Write back to master (only this source key changes).
    master[isbn13][source] = current
    master[isbn13]["isbn13"] = isbn13
    save_master_json(master)

    # Write back to the individual website JSON file.
    source_data = load_source_json(source)
    source_data[isbn13] = current
    save_source_json(source, source_data)


def append_preprocessing_log(
    isbn13: str,
    source: str,
    issue_type: str,
    details: str,
    action_taken: str,
) -> None:
    """
    Append one preprocessing / error row to the CSV log.

    Parameters
    ----------
    isbn13 : str
        ISBN related to the issue (or "N/A" if unknown).
    source : str
        Website name, "Input", "System", etc.
    issue_type : str
        Short category, e.g. "Invalid ISBN", "Duplicate ISBN".
    details : str
        Human-readable explanation.
    action_taken : str
        What the program did, e.g. "Skipped and continued".
    """
    csv_path = config.PREPROCESSING_CSV_PATH
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    if not csv_path.exists():
        csv_path.write_text(
            "isbn13,source,issue_type,details,action_taken\n",
            encoding="utf-8",
        )

    # Minimal CSV escaping for commas/quotes inside details.
    def _csv_escape(value: str) -> str:
        text = str(value).replace("\n", " ").replace("\r", " ")
        if "," in text or '"' in text:
            return '"' + text.replace('"', '""') + '"'
        return text

    row = ",".join(
        [
            _csv_escape(isbn13),
            _csv_escape(source),
            _csv_escape(issue_type),
            _csv_escape(details),
            _csv_escape(action_taken),
        ]
    )
    with csv_path.open("a", encoding="utf-8", newline="") as handle:
        handle.write(row + "\n")
