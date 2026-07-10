from typing import Any

from pydantic import BaseModel

from core.registries import ToolSpec, get_tool_registry
from core.registries.tool_registry import RetryPolicy
from tools.playwright.playwright_providers import get_playwright_provider

_RETRY_POLICY = RetryPolicy(max_attempts=2, backoff_seconds=1.0)
_TIMEOUT_SECONDS = 20.0

# Field names deliberately never "name" - see the docstring in
# playwright_providers.py for why.


class HealthCheckArgs(BaseModel):
    pass


class LaunchBrowserArgs(BaseModel):
    headless: bool = True


class CloseBrowserArgs(BaseModel):
    browser_id: str


class OpenUrlArgs(BaseModel):
    browser_id: str
    url: str
    timeout_ms: int = 15000


class ScreenshotArgs(BaseModel):
    browser_id: str
    full_page: bool = False


class ExtractHtmlArgs(BaseModel):
    browser_id: str


class ExtractTextArgs(BaseModel):
    browser_id: str


class ExecuteJavascriptArgs(BaseModel):
    browser_id: str
    script: str


class FillFormArgs(BaseModel):
    browser_id: str
    fields: dict[str, str]


class ClickElementArgs(BaseModel):
    browser_id: str
    selector: str


class WaitForSelectorArgs(BaseModel):
    browser_id: str
    selector: str
    timeout_ms: int = 10000


def playwright_health_check() -> dict:
    return get_playwright_provider().health_check().model_dump()


def playwright_launch_browser(headless: bool = True) -> dict:
    return get_playwright_provider().launch_browser(headless).model_dump()


def playwright_close_browser(browser_id: str) -> dict:
    return get_playwright_provider().close_browser(browser_id).model_dump()


def playwright_open_url(browser_id: str, url: str, timeout_ms: int = 15000) -> dict:
    return get_playwright_provider().open_url(browser_id, url, timeout_ms).model_dump()


def playwright_screenshot(browser_id: str, full_page: bool = False) -> dict:
    return get_playwright_provider().screenshot(browser_id, full_page).model_dump()


def playwright_extract_html(browser_id: str) -> dict:
    return get_playwright_provider().extract_html(browser_id).model_dump()


def playwright_extract_text(browser_id: str) -> dict:
    return get_playwright_provider().extract_text(browser_id).model_dump()


def playwright_execute_javascript(browser_id: str, script: str) -> dict:
    return get_playwright_provider().execute_javascript(browser_id, script).model_dump()


def playwright_fill_form(browser_id: str, fields: dict[str, str] | None = None) -> dict:
    return get_playwright_provider().fill_form(browser_id, fields or {}).model_dump()


def playwright_click_element(browser_id: str, selector: str) -> dict:
    return get_playwright_provider().click_element(browser_id, selector).model_dump()


def playwright_wait_for_selector(browser_id: str, selector: str, timeout_ms: int = 10000) -> dict:
    return get_playwright_provider().wait_for_selector(browser_id, selector, timeout_ms).model_dump()


_TOOLS: list[tuple[str, str, type[BaseModel], object]] = [
    ("playwright_health_check", "Check whether Playwright is installed and can launch a browser", HealthCheckArgs, playwright_health_check),
    ("playwright_launch_browser", "Launch a new headless browser session", LaunchBrowserArgs, playwright_launch_browser),
    ("playwright_close_browser", "Close a browser session", CloseBrowserArgs, playwright_close_browser),
    ("playwright_open_url", "Navigate a browser session to a URL", OpenUrlArgs, playwright_open_url),
    ("playwright_screenshot", "Take a screenshot of the current page", ScreenshotArgs, playwright_screenshot),
    ("playwright_extract_html", "Extract the current page's full HTML", ExtractHtmlArgs, playwright_extract_html),
    ("playwright_extract_text", "Extract the current page's visible text", ExtractTextArgs, playwright_extract_text),
    ("playwright_execute_javascript", "Execute arbitrary JavaScript in the page context", ExecuteJavascriptArgs, playwright_execute_javascript),
    ("playwright_fill_form", "Fill form fields by selector", FillFormArgs, playwright_fill_form),
    ("playwright_click_element", "Click an element by selector", ClickElementArgs, playwright_click_element),
    ("playwright_wait_for_selector", "Wait for a selector to appear", WaitForSelectorArgs, playwright_wait_for_selector),
]

for _name, _description, _schema, _func in _TOOLS:
    get_tool_registry().register(
        ToolSpec(
            name=_name,
            description=_description,
            input_schema=_schema,
            permissions=["internet_access"],
            retry_policy=_RETRY_POLICY,
            timeout_seconds=_TIMEOUT_SECONDS,
            cost_per_call_usd=0.0,
        ),
        _func,
    )
