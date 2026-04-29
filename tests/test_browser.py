from __future__ import annotations

from form_killer import browser


def test_launch_chromium_installs_missing_browser_and_retries(monkeypatch) -> None:
    calls: list[str] = []

    class FakeChromium:
        def launch(self, *, headless: bool):
            calls.append(f"launch:{headless}")
            if len(calls) == 1:
                raise RuntimeError("Executable doesn't exist. Please run the following command: playwright install")
            return "browser"

    class FakePlaywright:
        chromium = FakeChromium()

    monkeypatch.setattr(browser, "install_playwright_chromium", lambda: calls.append("install"))

    launched = browser.launch_chromium(FakePlaywright(), headless=True)

    assert launched == "browser"
    assert calls == ["launch:True", "install", "launch:True"]
