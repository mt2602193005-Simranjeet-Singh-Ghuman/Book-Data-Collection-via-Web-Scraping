"""
utils/isbn.py

Clean and check ISBN-10 / ISBN-13 values, and convert ISBN-10 to ISBN-13.
Scrapers should only ever see a normalized ISBN-13.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IsbnResult:
    """
    Structured result after cleaning / validating / normalizing one ISBN.

    Attributes
    ----------
    original : str
        Exactly what the user or CSV provided (before cleaning).
    cleaned : str
        Digits/X only (hyphens and spaces removed).
    isbn13 : str
        Normalized ISBN-13 used everywhere else in the project.
    was_converted_from_isbn10 : bool
        True if input was a valid ISBN-10 that we converted.
    detail : str
        Short message for the preprocessing log.
    """

    original: str
    cleaned: str
    isbn13: str
    was_converted_from_isbn10: bool
    detail: str


class InvalidIsbnError(ValueError):
    """Raised when an ISBN cannot be cleaned, validated, or converted."""


def clean_isbn(raw: str) -> str:
    """
    Remove whitespace and hyphens; uppercase trailing X for ISBN-10.

    Parameters
    ----------
    raw : str
        User-typed or CSV ISBN, possibly with hyphens/spaces.

    Returns
    -------
    str
        Compact ISBN containing only digits and optional trailing X.

    Raises
    ------
    InvalidIsbnError
        If input is empty after cleaning.
    """
    if raw is None:
        raise InvalidIsbnError("ISBN is empty.")

    # Keep digits and X/x only. Example: "0-306-40615-2" -> "0306406152"
    cleaned_chars: list[str] = []
    for ch in str(raw).strip():
        if ch.isdigit():
            cleaned_chars.append(ch)
        elif ch in {"X", "x"}:
            cleaned_chars.append("X")
        # spaces, hyphens, and other punctuation are intentionally ignored

    cleaned = "".join(cleaned_chars)
    if not cleaned:
        raise InvalidIsbnError("ISBN is empty after removing spaces/hyphens.")
    return cleaned


def is_valid_isbn10(isbn10: str) -> bool:
    """
    Return True if `isbn10` has a correct ISBN-10 check digit.

    Parameters
    ----------
    isbn10 : str
        Exactly 10 characters: 9 digits + final digit or X.
    """
    if len(isbn10) != 10:
        return False
    if not isbn10[:9].isdigit():
        return False
    if not (isbn10[9].isdigit() or isbn10[9] == "X"):
        return False

    total = 0
    for index, ch in enumerate(isbn10[:9]):
        # Weights go 10, 9, 8, ..., 2
        weight = 10 - index
        total += weight * int(ch)

    check_value = (11 - (total % 11)) % 11
    expected = "X" if check_value == 10 else str(check_value)
    return isbn10[9] == expected


def isbn13_check_digit(body12: str) -> str:
    """
    Compute the final ISBN-13 check digit for the first 12 digits.

    Parameters
    ----------
    body12 : str
        Exactly 12 digits (for books usually starts with 978 or 979).

    Returns
    -------
    str
        One check digit character ('0'..'9').
    """
    if len(body12) != 12 or not body12.isdigit():
        raise InvalidIsbnError("ISBN-13 body must be exactly 12 digits.")

    total = 0
    for index, ch in enumerate(body12):
        # Odd positions (1-based) weight 1; even positions weight 3.
        # In 0-based index: even -> 1, odd -> 3
        weight = 1 if index % 2 == 0 else 3
        total += weight * int(ch)

    check_value = (10 - (total % 10)) % 10
    return str(check_value)


def is_valid_isbn13(isbn13: str) -> bool:
    """
    Return True if `isbn13` has a correct ISBN-13 check digit.
    """
    if len(isbn13) != 13 or not isbn13.isdigit():
        return False
    return isbn13[-1] == isbn13_check_digit(isbn13[:12])


def isbn13_to_isbn10(isbn13: str) -> str:
    """
    Convert a 978-prefixed ISBN-13 to ISBN-10 (empty string if not possible).
    Useful as an extra search key on sites that still index ISBN-10.
    """
    cleaned = clean_isbn(isbn13)
    if len(cleaned) != 13 or not cleaned.startswith("978") or not is_valid_isbn13(cleaned):
        return ""
    body9 = cleaned[3:12]
    total = 0
    for index, ch in enumerate(body9):
        total += (10 - index) * int(ch)
    check = (11 - (total % 11)) % 11
    return body9 + ("X" if check == 10 else str(check))


def isbn10_to_isbn13(isbn10: str) -> str:
    """
    Convert a validated ISBN-10 into ISBN-13 using the 978 prefix.

    Parameters
    ----------
    isbn10 : str
        Clean 10-character ISBN-10.

    Returns
    -------
    str
        Equivalent ISBN-13.

    Raises
    ------
    InvalidIsbnError
        If ISBN-10 is invalid.
    """
    if not is_valid_isbn10(isbn10):
        raise InvalidIsbnError(f"Invalid ISBN-10 check digit: {isbn10}")

    # Drop ISBN-10 check digit; prefix 978; recompute ISBN-13 check digit.
    body12 = "978" + isbn10[:9]
    return body12 + isbn13_check_digit(body12)


def normalize_isbn(raw: str) -> IsbnResult:
    """
    Clean, validate, and normalize any ISBN-10/ISBN-13 input to ISBN-13.

    Parameters
    ----------
    raw : str
        Raw ISBN from keyboard or CSV.

    Returns
    -------
    IsbnResult
        Normalized result including the final isbn13.

    Raises
    ------
    InvalidIsbnError
        If the value is not a valid ISBN-10 or ISBN-13.

    Examples
    --------
    >>> normalize_isbn("0-306-40615-2").isbn13
    '9780306406157'
    >>> normalize_isbn("9780306406157").isbn13
    '9780306406157'
    """
    original = str(raw).strip()
    cleaned = clean_isbn(original)

    if len(cleaned) == 10:
        if not is_valid_isbn10(cleaned):
            raise InvalidIsbnError(f"Invalid ISBN-10: {original}")
        isbn13 = isbn10_to_isbn13(cleaned)
        return IsbnResult(
            original=original,
            cleaned=cleaned,
            isbn13=isbn13,
            was_converted_from_isbn10=True,
            detail="ISBN-10 validated and converted to ISBN-13",
        )

    if len(cleaned) == 13:
        if not is_valid_isbn13(cleaned):
            raise InvalidIsbnError(f"Invalid ISBN-13: {original}")
        return IsbnResult(
            original=original,
            cleaned=cleaned,
            isbn13=cleaned,
            was_converted_from_isbn10=False,
            detail="ISBN-13 validated",
        )

    raise InvalidIsbnError(
        f"ISBN must be 10 or 13 digits after cleaning; got length {len(cleaned)}: {original}"
    )


def normalize_isbn_list(raw_isbns: list[str]) -> tuple[list[IsbnResult], list[tuple[str, str]]]:
    """
    Normalize many ISBNs; collect successes and failures without crashing.

    Parameters
    ----------
    raw_isbns : list[str]
        Raw ISBN strings.

    Returns
    -------
    tuple[list[IsbnResult], list[tuple[str, str]]]
        (valid_results, invalid_pairs)
        invalid_pairs entries are (original_raw, error_message).

    Design Decision
    ---------------
    The whole program must NEVER stop because one bad ISBN appears in a CSV.
    Invalid rows are returned separately so main.py can log them and continue.

    Time Complexity
    ---------------
    O(N) for N input ISBNs.
    """
    valid: list[IsbnResult] = []
    invalid: list[tuple[str, str]] = []
    seen_isbn13: set[str] = set()

    for raw in raw_isbns:
        try:
            result = normalize_isbn(raw)
        except InvalidIsbnError as exc:
            invalid.append((str(raw), str(exc)))
            continue

        # Duplicate ISBN-13 after normalization: keep first, report the rest.
        if result.isbn13 in seen_isbn13:
            invalid.append(
                (
                    str(raw),
                    f"Duplicate ISBN after normalization: {result.isbn13}",
                )
            )
            continue

        seen_isbn13.add(result.isbn13)
        valid.append(result)

    return valid, invalid
