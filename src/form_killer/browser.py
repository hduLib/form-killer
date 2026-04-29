from __future__ import annotations

import subprocess
import sys
from typing import Any


class BrowserLaunchError(RuntimeError):
    pass


def launch_chromium(playwright: Any, *, headless: bool) -> Any:
    try:
        return playwright.chromium.launch(headless=headless)
    except Exception as exc:
        if not _looks_like_missing_playwright_browser(exc):
            raise BrowserLaunchError(_launch_error_message(headless)) from exc

    install_playwright_chromium()
    try:
        return playwright.chromium.launch(headless=headless)
    except Exception as exc:
        raise BrowserLaunchError(_launch_error_message(headless)) from exc


def install_playwright_chromium() -> None:
    try:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        raise BrowserLaunchError("Playwright Chromium 自动安装失败，请检查网络后重新运行 uv run form-killer。") from exc


def _looks_like_missing_playwright_browser(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "executable doesn't exist" in text
        or "browser was not found" in text
        or "playwright install" in text
        or "please run the following command" in text
    )


def _launch_error_message(headless: bool) -> str:
    if headless:
        return "无法启动 Playwright Chromium。请检查当前系统是否支持运行 Chromium。"
    return "无法启动 Playwright Chromium。当前环境可能没有图形界面，无法进行人工登录/验证交接。"
