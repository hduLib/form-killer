from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, TypeVar
from urllib.parse import urlparse

from form_killer.browser import BrowserLaunchError, launch_chromium
from form_killer.events import LoginTimeoutError, ServiceEvent, ServiceEventHandler
from form_killer.forms.base import FormSchema, Option, Question
from form_killer.forms.schema import FORM_CONTROL_BINDINGS_KEY, build_control_binding


SessionT = TypeVar("SessionT", bound="BrowserFormSession")


class BrowserFormSession:
    def __init__(self, playwright: Any, browser: Any, context: Any, page: Any) -> None:
        self._playwright = playwright
        self._browser = browser
        self._context = context
        self.page = page

    @classmethod
    def open_for_provider(
        cls: type[SessionT],
        *,
        storage_path: Path,
        headless: bool,
        error_type: type[Exception],
        missing_playwright_message: str,
    ) -> SessionT:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise error_type(missing_playwright_message) from exc

        playwright = sync_playwright().start()
        try:
            browser = launch_chromium(playwright, headless=headless)
            kwargs: dict[str, Any] = {}
            if storage_path.exists():
                kwargs["storage_state"] = str(storage_path)
            context = browser.new_context(**kwargs)
            page = context.new_page()
        except BrowserLaunchError as exc:
            playwright.stop()
            raise error_type(str(exc)) from exc
        except Exception:
            playwright.stop()
            raise
        return cls(playwright, browser, context, page)

    def save_storage_state(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._context.storage_state(path=str(path))

    def close(self) -> None:
        try:
            self._browser.close()
        finally:
            self._playwright.stop()


def login_challenge_image_bytes(page: Any) -> bytes:
    from form_killer.login import login_challenge_image_bytes as capture_login_challenge_image_bytes

    return capture_login_challenge_image_bytes(page)


def run_browser_login(
    *,
    browser_session_cls: type[BrowserFormSession],
    session_path: Path,
    url: str,
    login_timeout_seconds: int,
    is_login_complete: Callable[[Any], bool],
    get_login_name: Callable[[Any], str | None],
    login_required_type: type[Exception],
    timeout_message: str,
    challenge_image_bytes: Callable[[Any], bytes],
    wait_for_user: Callable[[], None] | None = None,
    login_runner: Callable[[Any, Callable[[], bool], int], None] | None = None,
    event_handler: ServiceEventHandler | None = None,
) -> str | None:
    if event_handler:
        event_handler(ServiceEvent("checking"))
    session = browser_session_cls.open(storage_path=session_path, headless=True)
    try:
        session.page.goto(url, wait_until="networkidle", timeout=60_000)
        if is_login_complete(session.page):
            return _finish_login(session, session_path, get_login_name, event_handler)
        if event_handler:
            return _login_with_events(
                session=session,
                session_path=session_path,
                login_timeout_seconds=login_timeout_seconds,
                is_login_complete=is_login_complete,
                get_login_name=get_login_name,
                challenge_image_bytes=challenge_image_bytes,
                event_handler=event_handler,
                timeout_message=timeout_message,
            )
        if login_runner:
            login_runner(session.page, lambda: is_login_complete(session.page), login_timeout_seconds)
        elif wait_for_user:
            wait_for_user()
        else:
            session.page.wait_for_timeout(login_timeout_seconds * 1000)
        if not is_login_complete(session.page):
            raise login_required_type(url)
        return _finish_login(session, session_path, get_login_name, event_handler)
    finally:
        session.close()


def _login_with_events(
    *,
    session: BrowserFormSession,
    session_path: Path,
    login_timeout_seconds: int,
    is_login_complete: Callable[[Any], bool],
    get_login_name: Callable[[Any], str | None],
    challenge_image_bytes: Callable[[Any], bytes],
    event_handler: ServiceEventHandler,
    timeout_message: str,
) -> str | None:
    event_handler(ServiceEvent("challenge_fetching"))
    image_bytes = challenge_image_bytes(session.page)
    event_handler(ServiceEvent("challenge", image_bytes=image_bytes))
    event_handler(ServiceEvent("waiting"))
    deadline = time.monotonic() + login_timeout_seconds
    while time.monotonic() < deadline:
        if is_login_complete(session.page):
            return _finish_login(session, session_path, get_login_name, event_handler)
        try:
            session.page.wait_for_timeout(2000)
        except Exception:
            time.sleep(2)
    raise LoginTimeoutError(timeout_message)


def _finish_login(
    session: BrowserFormSession,
    session_path: Path,
    get_login_name: Callable[[Any], str | None],
    event_handler: ServiceEventHandler | None,
) -> str | None:
    login_name = get_login_name(session.page)
    session.save_storage_state(session_path)
    if event_handler:
        event_handler(ServiceEvent("success", login_name=login_name))
    return login_name


def page_needs_login(
    page: Any,
    *,
    url_markers: tuple[str, ...],
    strong_login_markers: tuple[str, ...],
    fallback_login_markers: tuple[str, ...],
    has_form_controls: Callable[[Any], bool],
) -> bool:
    try:
        url = str(page.url).lower()
        text = str(page.locator("body").inner_text(timeout=1000)).lower()
    except Exception:
        return True
    if any(marker in url for marker in url_markers):
        return True
    if any(marker in text for marker in strong_login_markers):
        return True
    if has_form_controls(page):
        return False
    return any(marker in text for marker in fallback_login_markers)


def has_form_controls(page: Any, selector: str) -> bool:
    try:
        return page.locator(selector).count() > 0
    except Exception:
        return False


def has_auth_cookie(page: Any, cookie_names: set[str]) -> bool:
    try:
        cookies = page.context.cookies()
    except Exception:
        return False
    for cookie in cookies:
        name = str(cookie.get("name") or "").lower()
        value = str(cookie.get("value") or "")
        if name in cookie_names and len(value) >= 6:
            return True
    return False


def has_authenticated_signal(page: Any, *, script: str, cookie_names: set[str]) -> bool:
    try:
        if bool(page.evaluate(script)):
            return True
    except Exception:
        pass
    return has_auth_cookie(page, cookie_names)


def read_login_name_from_page(page: Any, script: str) -> str | None:
    try:
        value = page.evaluate(script)
    except Exception:
        return None
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def read_login_name_from_account_pages(
    page: Any,
    *,
    account_urls: tuple[str, ...],
    page_needs_login: Callable[[Any], bool],
    login_display_name_script: str,
) -> str | None:
    try:
        context = page.context
        account_page = context.new_page()
    except Exception:
        return None
    try:
        for url in account_urls:
            try:
                account_page.goto(url, wait_until="domcontentloaded", timeout=12_000)
                account_page.wait_for_timeout(1500)
            except Exception:
                pass
            if page_needs_login(account_page):
                continue
            value = read_login_name_from_page(account_page, login_display_name_script)
            if value:
                return value
    finally:
        try:
            account_page.close()
        except Exception:
            pass
    return None


def safe_page_title(page: Any) -> str | None:
    try:
        title = page.title()
    except Exception:
        return None
    return title.strip() or None


def wait_for_form_ready(page: Any, selector: str) -> None:
    try:
        page.wait_for_selector(selector, timeout=15_000)
    except Exception:
        pass


def extract_questions_from_page(page: Any, script: str) -> list[dict[str, Any]]:
    return list(page.evaluate(script))


def normalize_browser_question_kind(raw_kind: str, options: list[dict[str, Any]]) -> str:
    kind = raw_kind.lower()
    if kind in {"textarea", "longtext", "paragraph"}:
        return "text"
    if kind in {"text", "input", "string"}:
        return "string"
    if kind in {"number", "integer"}:
        return "integer"
    if kind in {"radio", "single"}:
        return "enum"
    if kind in {"checkbox", "multiple"}:
        return "checkbox"
    if kind in {"select", "dropdown"}:
        return "dropdown"
    if kind in {"switch", "boolean"}:
        return "boolean"
    if kind in {"scale", "rating"}:
        return "enum"
    if kind in {"file", "upload"}:
        return "unsupported"
    if options:
        return "enum"
    return "unsupported"


def build_browser_form_schema(
    raw_questions: list[dict[str, Any]],
    *,
    source_url: str,
    title: str,
    provider: str,
    raw_questions_key: str,
    default_form_id: str,
    submit_text: str = "鎻愪氦",
) -> FormSchema:
    questions: list[Question] = []
    metadata: list[dict[str, Any]] = []
    bindings: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_questions, 1):
        raw_id = str(raw.get("id") or f"q_{index}")
        kind = normalize_browser_question_kind(str(raw.get("kind") or "unsupported"), raw.get("options") or [])
        options = [
            Option(id=str(option.get("id") or option.get("label") or option_index), label=str(option.get("label") or ""))
            for option_index, option in enumerate(raw.get("options") or [], 1)
            if str(option.get("label") or "").strip()
        ]
        question = Question(
            id=raw_id,
            label=str(raw.get("label") or raw_id),
            kind=kind,
            raw_type=str(raw.get("kind") or "unsupported"),
            required=bool(raw.get("required", False)),
            options=options,
        )
        questions.append(question)
        metadata.append(
            {
                "id": question.id,
                "kind": question.kind,
                "label": question.label,
                "dom_index": int(raw.get("dom_index", index - 1)),
                "selector": raw.get("selector"),
                "options": [option.model_dump() for option in options],
            }
        )
        bindings.append(
            build_control_binding(
                question,
                handle=str(raw.get("handle") or raw.get("id") or question.id),
                dom_index=int(raw.get("dom_index", index - 1)),
                selector=raw.get("selector"),
                options=raw.get("options") or [],
            )
        )

    return FormSchema(
        id=extract_form_id(source_url, default_form_id),
        name=title,
        submit_text=submit_text,
        questions=questions,
        raw={
            "provider": provider,
            "document_type": "form",
            "source_url": source_url,
            raw_questions_key: metadata,
            FORM_CONTROL_BINDINGS_KEY: bindings,
        },
    )


def extract_form_id(url: str, default: str) -> str:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    return parts[-1] if parts else default
