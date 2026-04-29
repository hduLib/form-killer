from __future__ import annotations

from typing import Any

from form_killer.browser import BrowserLaunchError, launch_chromium


class CaptchaHandoffError(RuntimeError):
    pass


class BrowserCaptchaSession:
    def __init__(self, playwright: Any, browser: Any, context: Any) -> None:
        self._playwright = playwright
        self._browser = browser
        self._context = context

    @classmethod
    def open(cls, url: str, cookies: list[dict[str, Any]]) -> "BrowserCaptchaSession":
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise CaptchaHandoffError("缺少 Playwright，无法打开会话浏览器。") from exc

        playwright = sync_playwright().start()
        try:
            browser = launch_chromium(playwright, headless=False)
            context = browser.new_context()
            if cookies:
                context.add_cookies(cookies)
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded")
        except BrowserLaunchError as exc:
            playwright.stop()
            raise CaptchaHandoffError(str(exc)) from exc
        except Exception:
            playwright.stop()
            raise
        return cls(playwright, browser, context)

    def cookies(self) -> list[dict[str, Any]]:
        return list(self._context.cookies())

    def close(self) -> None:
        try:
            self._browser.close()
        finally:
            self._playwright.stop()
