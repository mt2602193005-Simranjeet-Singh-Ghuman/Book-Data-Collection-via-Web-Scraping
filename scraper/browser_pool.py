"""
Shared Playwright Chromium for the whole scrape run.

Launching a new browser per page is the biggest slowdown. This module keeps
one browser alive and only opens/closes short-lived contexts and pages.
"""

from __future__ import annotations

import atexit
from contextlib import contextmanager
from typing import Any, Generator, Optional

_playwright: Any = None
_browser: Any = None
_started: bool = False
# Persistent Amazon contexts (cookies survive across product fetches).
_amazon_contexts: dict[str, Any] = {}
_amazon_warmed: set[str] = set()


def is_ready() -> bool:
    """True when a shared browser is currently open."""
    return bool(_started and _browser is not None)


def start_shared_browser(verbose: bool = False) -> bool:
    """
    Start (or reuse) the shared Chromium instance.

    Prefers installed Chrome channel when available, else bundled Chromium.
    """
    global _playwright, _browser, _started

    if is_ready():
        return True

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        if verbose:
            print("[WARN] Playwright not installed. Level-2 scraping disabled.")
        return False

    try:
        _playwright = sync_playwright().start()
        browser = None
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
                browser = _playwright.chromium.launch(**launch_kwargs)
                break
            except Exception:  # noqa: BLE001
                browser = None
        if browser is None:
            raise RuntimeError("Could not launch Chromium/Chrome")
        _browser = browser
        _started = True
        atexit.register(stop_shared_browser)
        if verbose:
            print("[OK] Playwright shared browser ready (reused for all sites)")
        return True
    except Exception as exc:  # noqa: BLE001
        stop_shared_browser()
        if verbose:
            print("[WARN] Playwright Chromium missing/unusable.")
            print("       Run: python -m playwright install chromium")
            print(f"       Detail: {exc}")
        return False


def stop_shared_browser() -> None:
    """Close the shared browser and Playwright driver."""
    global _playwright, _browser, _started, _amazon_contexts, _amazon_warmed

    for context in list(_amazon_contexts.values()):
        try:
            context.close()
        except Exception:  # noqa: BLE001
            pass
    _amazon_contexts = {}
    _amazon_warmed = set()

    if _browser is not None:
        try:
            _browser.close()
        except Exception:  # noqa: BLE001
            pass
    _browser = None

    if _playwright is not None:
        try:
            _playwright.stop()
        except Exception:  # noqa: BLE001
            pass
    _playwright = None
    _started = False


def _get_amazon_context(
    store_key: str,
    *,
    user_agent: str,
    locale: str,
    timezone_id: str,
    extra_http_headers: dict[str, str],
) -> Any:
    """Return a long-lived Amazon context for .in / .com."""
    global _amazon_contexts

    if not start_shared_browser(verbose=False):
        raise RuntimeError("Shared Playwright browser unavailable")
    assert _browser is not None

    context = _amazon_contexts.get(store_key)
    if context is not None:
        return context

    context = _browser.new_context(
        user_agent=user_agent,
        locale=locale,
        timezone_id=timezone_id,
        viewport={"width": 1366, "height": 768},
        extra_http_headers=extra_http_headers,
    )
    _amazon_contexts[store_key] = context
    return context


@contextmanager
def amazon_page(
    store_key: str,
    *,
    user_agent: str,
    locale: str,
    timezone_id: str,
    extra_http_headers: dict[str, str],
    home_url: str,
    warm_ms: int = 800,
    timeout_ms: int = 45000,
) -> Generator[Any, None, None]:
    """
    Yield a page on a persistent Amazon context.

    Homepage warm-up runs once per storefront per process, then cookies stay.
    """
    context = _get_amazon_context(
        store_key,
        user_agent=user_agent,
        locale=locale,
        timezone_id=timezone_id,
        extra_http_headers=extra_http_headers,
    )
    page = context.new_page()
    page.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )
    try:
        if store_key not in _amazon_warmed:
            try:
                page.goto(
                    home_url,
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )
                page.wait_for_timeout(warm_ms)
                _amazon_warmed.add(store_key)
            except Exception:  # noqa: BLE001
                pass
        yield page
    finally:
        try:
            page.close()
        except Exception:  # noqa: BLE001
            pass


def reset_shared_browser() -> bool:
    """Restart after a crashed browser/context."""
    stop_shared_browser()
    return start_shared_browser(verbose=False)


@contextmanager
def shared_page(
    *,
    user_agent: str,
    locale: str = "en-US",
    viewport: Optional[dict[str, int]] = None,
    timezone_id: Optional[str] = None,
    extra_http_headers: Optional[dict[str, str]] = None,
    cookies: Optional[list[dict[str, Any]]] = None,
    hide_webdriver: bool = True,
) -> Generator[Any, None, None]:
    """
    Yield a Playwright Page on the shared browser.

    The context (and page) are closed on exit; the browser stays open.
    """
    if not start_shared_browser(verbose=False):
        raise RuntimeError("Shared Playwright browser unavailable")

    assert _browser is not None
    context_kwargs: dict[str, Any] = {
        "user_agent": user_agent,
        "locale": locale,
        "viewport": viewport or {"width": 1366, "height": 768},
    }
    if timezone_id:
        context_kwargs["timezone_id"] = timezone_id
    if extra_http_headers:
        context_kwargs["extra_http_headers"] = extra_http_headers

    context = None
    try:
        try:
            context = _browser.new_context(**context_kwargs)
        except Exception:  # noqa: BLE001
            if not reset_shared_browser():
                raise
            assert _browser is not None
            context = _browser.new_context(**context_kwargs)

        if cookies:
            try:
                context.add_cookies(cookies)
            except Exception:  # noqa: BLE001
                pass

        page = context.new_page()
        if hide_webdriver:
            page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', "
                "{get: () => undefined});"
            )
        yield page
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass
