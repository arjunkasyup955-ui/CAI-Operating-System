import base64
import os
from typing import Any, Protocol

from dotenv import load_dotenv
from pydantic import BaseModel

# Deliberately self-contained (own env reading, not core.config.Settings) so this tool
# module never needs the frozen Phase 0 kernel to change to gain a new config key -
# same convention as tools/n8n, tools/docker, tools/database, tools/chroma.
load_dotenv()


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


class PlaywrightHealthStatus(BaseModel):
    healthy: bool
    installed: bool
    error: str = ""


class PlaywrightOperationResult(BaseModel):
    success: bool
    operation: str
    browser_id: str = ""
    url: str = ""
    title: str = ""
    html: str = ""
    text: str = ""
    screenshot_base64: str = ""
    js_result: Any = None
    error: str = ""


class PlaywrightProvider(Protocol):
    """Persistent-session browser automation. Every method either returns a
    PlaywrightOperationResult on success or raises on failure - the agent layer's
    try/except (with ToolRegistry's RetryPolicy) handles graceful degradation
    uniformly, the same pattern as every prior provider. Field names deliberately
    avoid "name" anywhere (ToolRegistry.invoke()'s own positional parameter is
    literally called `name` - a schema field called `name` collides with it; found
    and fixed in the Chroma component, Phase 3 Component 3, avoided proactively here
    via "browser_id").
    """

    name: str

    def health_check(self) -> PlaywrightHealthStatus: ...
    def launch_browser(self, headless: bool = True) -> PlaywrightOperationResult: ...
    def close_browser(self, browser_id: str) -> PlaywrightOperationResult: ...
    def open_url(self, browser_id: str, url: str, timeout_ms: int = 15000) -> PlaywrightOperationResult: ...
    def screenshot(self, browser_id: str, full_page: bool = False) -> PlaywrightOperationResult: ...
    def extract_html(self, browser_id: str) -> PlaywrightOperationResult: ...
    def extract_text(self, browser_id: str) -> PlaywrightOperationResult: ...
    def execute_javascript(self, browser_id: str, script: str) -> PlaywrightOperationResult: ...
    def fill_form(self, browser_id: str, fields: dict[str, str]) -> PlaywrightOperationResult: ...
    def click_element(self, browser_id: str, selector: str) -> PlaywrightOperationResult: ...
    def wait_for_selector(self, browser_id: str, selector: str, timeout_ms: int = 10000) -> PlaywrightOperationResult: ...


class PlaywrightRealProvider:
    """Default PlaywrightProvider - real headless-browser automation via the
    `playwright` package (sync API), lazily imported so this module loads fine even
    when playwright isn't installed (confirmed not installed in this sandbox). A
    call then raises a clear, actionable error rather than crashing at import time -
    installing playwright later requires no code change here. Each launch_browser
    call keeps its Playwright/Browser/Page objects alive across calls (keyed by
    browser_id) using the non-context-manager `sync_playwright().start()` form,
    since tool calls are stateless individual invocations, not a single `with` block.
    """

    name = "playwright_real"

    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}
        self._counter = 0

    def health_check(self) -> PlaywrightHealthStatus:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            return PlaywrightHealthStatus(healthy=False, installed=False, error=str(exc))

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                browser.close()
            return PlaywrightHealthStatus(healthy=True, installed=True)
        except Exception as exc:
            # Package is importable but browser binaries aren't installed
            # (`playwright install chromium`) or the launch otherwise failed.
            return PlaywrightHealthStatus(healthy=False, installed=True, error=str(exc))

    def _get_session(self, browser_id: str) -> dict[str, Any]:
        if browser_id not in self._sessions:
            raise ValueError(f"browser '{browser_id}' does not exist or is not launched")
        return self._sessions[browser_id]

    def launch_browser(self, headless: bool = True) -> PlaywrightOperationResult:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "playwright is not installed - `pip install playwright` and `playwright install chromium`"
            ) from exc

        pw = sync_playwright().start()
        try:
            browser = pw.chromium.launch(headless=headless)
        except Exception:
            pw.stop()
            raise
        page = browser.new_page()
        self._counter += 1
        browser_id = f"browser-{self._counter}"
        self._sessions[browser_id] = {"playwright": pw, "browser": browser, "page": page}
        return PlaywrightOperationResult(success=True, operation="launch_browser", browser_id=browser_id)

    def close_browser(self, browser_id: str) -> PlaywrightOperationResult:
        session = self._get_session(browser_id)
        session["browser"].close()
        session["playwright"].stop()
        del self._sessions[browser_id]
        return PlaywrightOperationResult(success=True, operation="close_browser", browser_id=browser_id)

    def open_url(self, browser_id: str, url: str, timeout_ms: int = 15000) -> PlaywrightOperationResult:
        page = self._get_session(browser_id)["page"]
        page.goto(url, timeout=timeout_ms)
        return PlaywrightOperationResult(success=True, operation="open_url", browser_id=browser_id, url=page.url, title=page.title())

    def screenshot(self, browser_id: str, full_page: bool = False) -> PlaywrightOperationResult:
        page = self._get_session(browser_id)["page"]
        data = page.screenshot(full_page=full_page)
        return PlaywrightOperationResult(success=True, operation="screenshot", browser_id=browser_id, screenshot_base64=base64.b64encode(data).decode())

    def extract_html(self, browser_id: str) -> PlaywrightOperationResult:
        page = self._get_session(browser_id)["page"]
        return PlaywrightOperationResult(success=True, operation="extract_html", browser_id=browser_id, html=page.content())

    def extract_text(self, browser_id: str) -> PlaywrightOperationResult:
        page = self._get_session(browser_id)["page"]
        return PlaywrightOperationResult(success=True, operation="extract_text", browser_id=browser_id, text=page.inner_text("body"))

    def execute_javascript(self, browser_id: str, script: str) -> PlaywrightOperationResult:
        page = self._get_session(browser_id)["page"]
        result = page.evaluate(script)
        return PlaywrightOperationResult(success=True, operation="execute_javascript", browser_id=browser_id, js_result=result)

    def fill_form(self, browser_id: str, fields: dict[str, str]) -> PlaywrightOperationResult:
        page = self._get_session(browser_id)["page"]
        for selector, value in fields.items():
            page.fill(selector, value)
        return PlaywrightOperationResult(success=True, operation="fill_form", browser_id=browser_id)

    def click_element(self, browser_id: str, selector: str) -> PlaywrightOperationResult:
        page = self._get_session(browser_id)["page"]
        page.click(selector)
        return PlaywrightOperationResult(success=True, operation="click_element", browser_id=browser_id)

    def wait_for_selector(self, browser_id: str, selector: str, timeout_ms: int = 10000) -> PlaywrightOperationResult:
        page = self._get_session(browser_id)["page"]
        page.wait_for_selector(selector, timeout=timeout_ms)
        return PlaywrightOperationResult(success=True, operation="wait_for_selector", browser_id=browser_id)


class FakePlaywrightProvider:
    """In-memory PlaywrightProvider - deterministic, no real browser needed. Requires
    a browser to be launched before any page action, and a URL to be opened before
    any page-content action (extract/screenshot/js/fill/click/wait), mirroring real
    Playwright semantics, so the lifecycle test exercises genuine state transitions
    rather than canned responses.
    """

    name = "fake_playwright"

    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}
        self._counter = 0

    def health_check(self) -> PlaywrightHealthStatus:
        return PlaywrightHealthStatus(healthy=True, installed=True)

    def _get_session(self, browser_id: str) -> dict[str, Any]:
        if browser_id not in self._sessions:
            raise ValueError(f"browser '{browser_id}' does not exist or is not launched")
        return self._sessions[browser_id]

    def _get_opened_session(self, browser_id: str) -> dict[str, Any]:
        session = self._get_session(browser_id)
        if not session["url"]:
            raise RuntimeError(f"browser '{browser_id}' has no page opened - call open_url first")
        return session

    def launch_browser(self, headless: bool = True) -> PlaywrightOperationResult:
        self._counter += 1
        browser_id = f"browser-{self._counter}"
        self._sessions[browser_id] = {"url": "", "title": "", "form_values": {}, "clicks": [], "headless": headless}
        return PlaywrightOperationResult(success=True, operation="launch_browser", browser_id=browser_id)

    def close_browser(self, browser_id: str) -> PlaywrightOperationResult:
        self._get_session(browser_id)
        del self._sessions[browser_id]
        return PlaywrightOperationResult(success=True, operation="close_browser", browser_id=browser_id)

    def open_url(self, browser_id: str, url: str, timeout_ms: int = 15000) -> PlaywrightOperationResult:
        session = self._get_session(browser_id)
        session["url"] = url
        session["title"] = f"Fake page for {url}"
        return PlaywrightOperationResult(success=True, operation="open_url", browser_id=browser_id, url=url, title=session["title"])

    def screenshot(self, browser_id: str, full_page: bool = False) -> PlaywrightOperationResult:
        self._get_opened_session(browser_id)
        fake_png = base64.b64encode(b"fake-png-bytes").decode()
        return PlaywrightOperationResult(success=True, operation="screenshot", browser_id=browser_id, screenshot_base64=fake_png)

    def extract_html(self, browser_id: str) -> PlaywrightOperationResult:
        session = self._get_opened_session(browser_id)
        html = f"<html><head><title>{session['title']}</title></head><body>Fake content for {session['url']}</body></html>"
        return PlaywrightOperationResult(success=True, operation="extract_html", browser_id=browser_id, html=html)

    def extract_text(self, browser_id: str) -> PlaywrightOperationResult:
        session = self._get_opened_session(browser_id)
        return PlaywrightOperationResult(success=True, operation="extract_text", browser_id=browser_id, text=f"Fake visible text for {session['url']}")

    def execute_javascript(self, browser_id: str, script: str) -> PlaywrightOperationResult:
        self._get_opened_session(browser_id)
        return PlaywrightOperationResult(success=True, operation="execute_javascript", browser_id=browser_id, js_result=f"executed: {script}")

    def fill_form(self, browser_id: str, fields: dict[str, str]) -> PlaywrightOperationResult:
        session = self._get_opened_session(browser_id)
        session["form_values"].update(fields)
        return PlaywrightOperationResult(success=True, operation="fill_form", browser_id=browser_id)

    def click_element(self, browser_id: str, selector: str) -> PlaywrightOperationResult:
        session = self._get_opened_session(browser_id)
        if selector == "#does-not-exist":
            raise RuntimeError(f"element '{selector}' not found")
        session["clicks"].append(selector)
        return PlaywrightOperationResult(success=True, operation="click_element", browser_id=browser_id)

    def wait_for_selector(self, browser_id: str, selector: str, timeout_ms: int = 10000) -> PlaywrightOperationResult:
        self._get_opened_session(browser_id)
        if selector == "#does-not-exist":
            # Deliberately NOT a builtin TimeoutError: in Python 3.11+,
            # concurrent.futures.TimeoutError IS builtins.TimeoutError, and
            # ToolRegistry's own timeout mechanism catches that type - a fake
            # provider raising it here would be misreported as a tool timeout
            # rather than this specific "selector not found" failure.
            raise RuntimeError(f"selector '{selector}' not found within {timeout_ms}ms")
        return PlaywrightOperationResult(success=True, operation="wait_for_selector", browser_id=browser_id)


_provider: PlaywrightProvider = PlaywrightRealProvider()


def get_playwright_provider() -> PlaywrightProvider:
    return _provider


def set_playwright_provider(provider: PlaywrightProvider) -> None:
    """Swappable, not cached - lets tests inject a deterministic fake provider."""
    global _provider
    _provider = provider
