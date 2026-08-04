"""
scraper package

One file per website, plus base.py for the shared fetch/parse logic.
"""

from scraper.amazon import AmazonScraper
from scraper.audible import AudibleScraper
from scraper.bookbub import BookBubScraper
from scraper.goodreads import GoodreadsScraper
from scraper.kobo import KoboScraper

__all__ = [
    "AmazonScraper",
    "AudibleScraper",
    "BookBubScraper",
    "GoodreadsScraper",
    "KoboScraper",
]
