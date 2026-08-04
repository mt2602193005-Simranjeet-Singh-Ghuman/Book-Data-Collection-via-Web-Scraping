"""
utils/folder_setup.py

Creates the input/output folders on startup.
Safe to run again — existing folders are left as they are.
"""

from __future__ import annotations

from pathlib import Path

import config


def create_project_folders(verbose: bool = True) -> list[Path]:
    """
    Create the full project folder tree if it does not already exist.

    Parameters
    ----------
    verbose : bool, default True
        If True, print each created/verified directory to the terminal.

    Returns
    -------
    list[Path]
        The list of directories that were ensured to exist.

    Raises
    ------
    OSError
        If the operating system refuses to create a directory.

    Design Decision
    ---------------
    exist_ok=True means re-running the program never crashes with
    "folder already exists" — important for lab demos and retries.
    """
    directories = config.all_output_directories()

    if verbose:
        print("Creating / verifying project folders...")
        print("-" * 40)

    try:
        for directory in directories:
            # parents=True  -> create intermediate folders (e.g. output/ before JSON/)
            # exist_ok=True -> do not error if the folder is already there
            directory.mkdir(parents=True, exist_ok=True)
            if verbose:
                # relative_to keeps terminal output short and readable
                relative = directory.relative_to(config.PROJECT_ROOT)
                print(f"[OK] {relative}")
    except OSError as exc:
        # Common causes: permission denied, invalid path, disk full
        print(f"[ERROR] Could not create folders: {exc}")
        raise

    if verbose:
        print("-" * 40)
        print("Folder setup complete.")
        print()

    return directories


def ensure_preprocessing_csv_header(verbose: bool = True) -> Path:
    """
    Ensure the preprocessing CSV log file exists with a header row.

    Module 1 only creates the empty log shell. Later modules will append rows
    such as Missing Fields, Network Failure, Invalid ISBN, etc.

    Parameters
    ----------
    verbose : bool, default True
        Print a short confirmation message.

    Returns
    -------
    Path
        Path to output/Preprocessing/preprocessing_report.csv
    """
    csv_path = config.PREPROCESSING_CSV_PATH
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    header = (
        "isbn13,source,issue_type,details,action_taken\n"
    )

    # Create file with header only if it does not exist yet.
    # We must not wipe an existing log from a previous run.
    if not csv_path.exists():
        csv_path.write_text(header, encoding="utf-8")
        if verbose:
            relative = csv_path.relative_to(config.PROJECT_ROOT)
            print(f"[OK] Created log shell: {relative}")
    elif verbose:
        relative = csv_path.relative_to(config.PROJECT_ROOT)
        print(f"[OK] Log already present: {relative}")

    return csv_path
