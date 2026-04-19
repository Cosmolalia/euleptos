"""
Browser Tool — Headless Chrome via Playwright.

Navigate pages, screenshot, fill forms, click buttons, extract text/HTML,
read page content for the agent to reason about. This is the tool that
lets the agent interact with the web like a human.

The browser instance persists across calls within a session so you can
navigate multi-step flows (login → fill form → submit → verify).

Config in tools_config.json:
{
    "browser": {
        "enabled": true,
        "headless": true,
        "timeout_ms": 30000,
        "screenshot_dir": "data_dev/agent/screenshots",
        "user_agent": null
    }
}
"""

import base64
import json
from pathlib import Path
from datetime import datetime
from typing import Optional

from . import BaseTool

# Lazy import — playwright may not be installed
_playwright_available = True
try:
    from playwright.sync_api import sync_playwright, Browser, Page, BrowserContext
except ImportError:
    _playwright_available = False


class BrowserTool(BaseTool):
    name = "browser"
    description = (
        "Headless Chrome browser automation. Navigate websites, fill forms, "
        "click buttons, take screenshots, extract page content. Supports "
        "multi-step flows — the browser stays open between calls so you can "
        "log in, navigate, and interact like a human would."
    )
    actions = {
        "navigate": "Go to a URL. Params: url, wait_for (optional: 'load'|'domcontentloaded'|'networkidle')",
        "screenshot": "Take a screenshot. Params: full_page (bool, default false), selector (optional CSS selector)",
        "click": "Click an element. Params: selector (CSS selector or text='...')",
        "fill": "Fill a form field. Params: selector (CSS selector), value (text to type)",
        "type": "Type text character by character (for JS-heavy inputs). Params: selector, text",
        "select": "Select dropdown option. Params: selector, value",
        "text": "Extract visible text from the page or a selector. Params: selector (optional)",
        "html": "Get HTML content. Params: selector (optional, default full page), outer (bool, default true)",
        "eval": "Evaluate JavaScript on the page. Params: script (JS code string)",
        "wait": "Wait for an element. Params: selector, timeout_ms (optional, default 10000)",
        "back": "Go back one page. No params.",
        "forward": "Go forward one page. No params.",
        "cookies": "Get all cookies. No params.",
        "status": "Get browser status — current URL, title, viewport. No params.",
        "close": "Close the browser. No params.",
        "pdf": "Save page as PDF. Params: path (optional). Requires headless mode.",
    }

    def __init__(self, config: dict = None):
        super().__init__(config)
        self._playwright = None
        self._browser: Optional[object] = None
        self._context: Optional[object] = None
        self._page: Optional[object] = None
        self._screenshot_dir = Path(self.config.get("screenshot_dir", "data_dev/agent/screenshots"))

    def is_configured(self) -> bool:
        return _playwright_available

    def _ensure_browser(self):
        """Launch browser if not already running."""
        if self._page and not self._page.is_closed():
            return

        if not _playwright_available:
            raise RuntimeError("Playwright is not installed. Run: pip install playwright && playwright install chromium")

        # Clean up stale state
        self._close_browser()

        self._playwright = sync_playwright().start()
        headless = self.config.get("headless", True)
        self._browser = self._playwright.chromium.launch(headless=headless)

        context_opts = {}
        ua = self.config.get("user_agent")
        if ua:
            context_opts["user_agent"] = ua
        # Default to a reasonable viewport
        context_opts["viewport"] = {"width": 1280, "height": 720}

        self._context = self._browser.new_context(**context_opts)
        self._page = self._context.new_page()

        timeout = self.config.get("timeout_ms", 30000)
        self._page.set_default_timeout(timeout)

    def _close_browser(self):
        """Clean shutdown."""
        try:
            if self._context:
                self._context.close()
        except Exception:
            pass
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright:
                self._playwright.stop()
        except Exception:
            pass
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    def execute(self, action: str, params: dict = None) -> dict:
        params = params or {}

        if not _playwright_available:
            return {"ok": False, "error": "Playwright not installed. Run: pip install playwright && playwright install chromium"}

        if action == "close":
            self._close_browser()
            return {"ok": True, "message": "Browser closed"}

        # All other actions need a live browser
        try:
            self._ensure_browser()
        except Exception as e:
            return {"ok": False, "error": f"Failed to launch browser: {e}"}

        dispatch = {
            "navigate": self._navigate,
            "screenshot": self._screenshot,
            "click": self._click,
            "fill": self._fill,
            "type": self._type,
            "select": self._select,
            "text": self._text,
            "html": self._html,
            "eval": self._eval,
            "wait": self._wait,
            "back": self._back,
            "forward": self._forward,
            "cookies": self._cookies,
            "status": self._status,
            "pdf": self._pdf,
        }

        handler = dispatch.get(action)
        if not handler:
            return {"ok": False, "error": f"Unknown action: {action}"}

        try:
            return handler(params)
        except Exception as e:
            return {"ok": False, "error": str(e), "url": self._page.url if self._page else None}

    def _navigate(self, params: dict) -> dict:
        url = params.get("url")
        if not url:
            return {"ok": False, "error": "Missing required param: 'url'"}

        wait_for = params.get("wait_for", "load")
        response = self._page.goto(url, wait_until=wait_for)

        return {
            "ok": True,
            "url": self._page.url,
            "title": self._page.title(),
            "status": response.status if response else None,
        }

    def _screenshot(self, params: dict) -> dict:
        self._screenshot_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"screenshot_{timestamp}.png"
        path = self._screenshot_dir / filename

        opts = {"path": str(path)}
        if params.get("full_page"):
            opts["full_page"] = True
        if params.get("selector"):
            element = self._page.query_selector(params["selector"])
            if element:
                element.screenshot(path=str(path))
                return {
                    "ok": True,
                    "path": str(path),
                    "selector": params["selector"],
                    "url": self._page.url,
                }
            return {"ok": False, "error": f"Selector '{params['selector']}' not found"}

        self._page.screenshot(**opts)
        return {
            "ok": True,
            "path": str(path),
            "url": self._page.url,
            "title": self._page.title(),
        }

    def _click(self, params: dict) -> dict:
        selector = params.get("selector")
        if not selector:
            return {"ok": False, "error": "Missing required param: 'selector'"}

        self._page.click(selector)
        self._page.wait_for_load_state("domcontentloaded")

        return {
            "ok": True,
            "clicked": selector,
            "url": self._page.url,
            "title": self._page.title(),
        }

    def _fill(self, params: dict) -> dict:
        selector = params.get("selector")
        value = params.get("value", "")
        if not selector:
            return {"ok": False, "error": "Missing required param: 'selector'"}

        self._page.fill(selector, value)
        return {"ok": True, "filled": selector, "value_length": len(value)}

    def _type(self, params: dict) -> dict:
        selector = params.get("selector")
        text = params.get("text", "")
        if not selector:
            return {"ok": False, "error": "Missing required param: 'selector'"}

        self._page.type(selector, text, delay=50)
        return {"ok": True, "typed": selector, "text_length": len(text)}

    def _select(self, params: dict) -> dict:
        selector = params.get("selector")
        value = params.get("value")
        if not selector or value is None:
            return {"ok": False, "error": "Missing required params: 'selector', 'value'"}

        self._page.select_option(selector, value)
        return {"ok": True, "selected": selector, "value": value}

    def _text(self, params: dict) -> dict:
        selector = params.get("selector")
        if selector:
            element = self._page.query_selector(selector)
            if not element:
                return {"ok": False, "error": f"Selector '{selector}' not found"}
            text = element.inner_text()
        else:
            text = self._page.inner_text("body")

        # Cap at 20k chars to avoid blowing up context
        truncated = len(text) > 20000
        text = text[:20000]

        return {
            "ok": True,
            "text": text,
            "truncated": truncated,
            "url": self._page.url,
        }

    def _html(self, params: dict) -> dict:
        selector = params.get("selector")
        outer = params.get("outer", True)

        if selector:
            element = self._page.query_selector(selector)
            if not element:
                return {"ok": False, "error": f"Selector '{selector}' not found"}
            html = element.evaluate("el => el.outerHTML") if outer else element.inner_html()
        else:
            html = self._page.content()

        truncated = len(html) > 50000
        html = html[:50000]

        return {"ok": True, "html": html, "truncated": truncated}

    def _eval(self, params: dict) -> dict:
        script = params.get("script")
        if not script:
            return {"ok": False, "error": "Missing required param: 'script'"}

        result = self._page.evaluate(script)
        result_str = json.dumps(result, default=str) if result is not None else "undefined"
        if len(result_str) > 10000:
            result_str = result_str[:10000] + "...(truncated)"

        return {"ok": True, "result": result_str}

    def _wait(self, params: dict) -> dict:
        selector = params.get("selector")
        if not selector:
            return {"ok": False, "error": "Missing required param: 'selector'"}

        timeout = params.get("timeout_ms", 10000)
        self._page.wait_for_selector(selector, timeout=timeout)
        return {"ok": True, "found": selector}

    def _back(self, params: dict) -> dict:
        self._page.go_back()
        return {"ok": True, "url": self._page.url, "title": self._page.title()}

    def _forward(self, params: dict) -> dict:
        self._page.go_forward()
        return {"ok": True, "url": self._page.url, "title": self._page.title()}

    def _cookies(self, params: dict) -> dict:
        cookies = self._context.cookies()
        return {"ok": True, "cookies": cookies}

    def _status(self, params: dict) -> dict:
        return {
            "ok": True,
            "url": self._page.url,
            "title": self._page.title(),
            "viewport": self._page.viewport_size,
            "browser_alive": not self._browser.is_connected() if hasattr(self._browser, "is_connected") else True,
        }

    def _pdf(self, params: dict) -> dict:
        self._screenshot_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_path = str(self._screenshot_dir / f"page_{timestamp}.pdf")
        path = params.get("path", default_path)

        self._page.pdf(path=path)
        return {"ok": True, "path": path, "url": self._page.url}
