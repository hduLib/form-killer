from __future__ import annotations

from pathlib import Path

import pytest

from form_killer.forms.services import resolve_form_service
from form_killer.forms.wps import (
    WpsFormService,
    WpsLoginRequired,
    WpsUnsupportedDocumentError,
    classify_wps_url,
    is_login_complete,
    parse_wps_questions,
)
from form_killer.forms import wps


def test_route_resolves_wps_form_url() -> None:
    routed = resolve_form_service("https://f.wps.cn/g/Ptd0OY5K/")

    assert routed.provider == "wps"
    assert routed.document_type == "form"


def test_route_marks_wps_non_form_as_unsupported() -> None:
    routed = resolve_form_service("https://www.kdocs.cn/l/csample")

    assert routed.provider == "wps"
    assert routed.document_type == "unsupported"


def test_classify_wps_url() -> None:
    assert classify_wps_url("https://f.wps.cn/g/Ptd0OY5K/") == "form"
    assert classify_wps_url("https://f.wps.cn/ksform/w/write/Ptd0OY5K") == "form"
    assert classify_wps_url("https://www.kdocs.cn/l/csample") == "unsupported"


def test_parse_wps_questions_maps_fields_and_metadata() -> None:
    schema = parse_wps_questions(
        [
            {
                "id": "identity",
                "label": "您的身份是",
                "kind": "radio",
                "required": True,
                "dom_index": 0,
                "options": [{"id": "1", "label": "研究生"}],
            },
            {"id": "comment", "label": "意见", "kind": "textarea", "required": False, "dom_index": 1},
        ],
        source_url="https://f.wps.cn/g/Ptd0OY5K/",
        title="WPS 示例表单",
    )

    assert schema.raw["provider"] == "wps"
    assert schema.raw["document_type"] == "form"
    assert schema.raw["source_url"] == "https://f.wps.cn/g/Ptd0OY5K/"
    assert schema.questions[0].kind == "enum"
    assert schema.questions[0].required is True
    assert schema.questions[0].options[0].label == "研究生"
    assert schema.questions[1].kind == "text"
    assert schema.raw["wps_questions"][0]["dom_index"] == 0


def test_wps_service_rejects_unsupported_document_type(tmp_path: Path) -> None:
    service = WpsFormService(document_type="unsupported", session_path=tmp_path / "wps.json")

    with pytest.raises(WpsUnsupportedDocumentError):
        service.fetch_schema("https://www.kdocs.cn/l/csample")


def test_wps_login_saves_storage_state(tmp_path: Path) -> None:
    calls: list[str] = []

    class FakeLocator:
        def __init__(self, count: int):
            self._count = count

        def count(self):
            return self._count

        def inner_text(self, timeout=None):
            return "form"

    class FakePage:
        url = "https://f.wps.cn/g/private"
        authenticated = False

        def goto(self, url, **kwargs):
            self.url = url
            calls.append(url)

        def locator(self, selector):
            return FakeLocator(1 if self.authenticated else 0)

        def evaluate(self, script):
            if "data-name" in script:
                return "Alice"
            return False

    class FakeSession:
        page = FakePage()

        @classmethod
        def open(cls, *, storage_path, headless):
            assert storage_path == tmp_path / "wps.json"
            assert headless is True
            return cls()

        def save_storage_state(self, path):
            calls.append(str(path))

        def close(self):
            calls.append("closed")

    service = WpsFormService(
        session_path=tmp_path / "wps.json",
        browser_session_cls=FakeSession,
        login_timeout_seconds=1,
    )

    login_name = service.login(
        "https://f.wps.cn/g/private",
        login_runner=lambda page, is_done, timeout: (
            calls.append(f"runner-before:{timeout}:{is_done()}"),
            setattr(page, "authenticated", True),
            calls.append(f"runner-after:{timeout}:{is_done()}"),
        ),
    )

    assert login_name == "Alice"
    assert calls == [
        "https://f.wps.cn/g/private",
        "runner-before:1:False",
        "runner-after:1:True",
        str(tmp_path / "wps.json"),
        "closed",
    ]


def test_wps_login_skips_challenge_when_session_already_authenticated(tmp_path: Path) -> None:
    calls: list[str] = []

    class FakeLocator:
        def count(self):
            return 1

        def inner_text(self, timeout=None):
            return "form"

    class FakePage:
        url = "https://f.wps.cn/g/private"

        def goto(self, url, **kwargs):
            self.url = url
            calls.append(url)

        def locator(self, selector):
            return FakeLocator()

        def evaluate(self, script):
            return "Alice"

    class FakeSession:
        page = FakePage()

        @classmethod
        def open(cls, *, storage_path, headless):
            return cls()

        def save_storage_state(self, path):
            calls.append(str(path))

        def close(self):
            calls.append("closed")

    service = WpsFormService(
        session_path=tmp_path / "wps.json",
        browser_session_cls=FakeSession,
        login_timeout_seconds=1,
    )

    login_name = service.login(
        "https://f.wps.cn/g/private",
        login_runner=lambda page, is_done, timeout: calls.append("runner"),
    )

    assert login_name == "Alice"
    assert calls == ["https://f.wps.cn/g/private", str(tmp_path / "wps.json"), "closed"]


def test_wps_login_emits_events_until_success(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []
    events = []

    class FakeLocator:
        def __init__(self, count: int):
            self._count = count

        def count(self):
            return self._count

        def inner_text(self, timeout=None):
            return "form"

    class FakePage:
        url = "https://f.wps.cn/g/private"
        authenticated = False

        def goto(self, url, **kwargs):
            self.url = url
            calls.append(url)

        def locator(self, selector):
            return FakeLocator(1 if self.authenticated else 0)

        def evaluate(self, script):
            if "data-name" in script:
                return "Alice"
            return False

        def wait_for_timeout(self, timeout):
            self.authenticated = True

    class FakeSession:
        page = FakePage()

        @classmethod
        def open(cls, *, storage_path, headless):
            return cls()

        def save_storage_state(self, path):
            calls.append(str(path))

        def close(self):
            calls.append("closed")

    monkeypatch.setattr(wps, "login_challenge_image_bytes", lambda page: b"png")
    service = WpsFormService(
        session_path=tmp_path / "wps.json",
        browser_session_cls=FakeSession,
        login_timeout_seconds=5,
    )

    login_name = service.login("https://f.wps.cn/g/private", event_handler=events.append)

    assert login_name == "Alice"
    assert [event.kind for event in events] == ["checking", "challenge_fetching", "challenge", "waiting", "success"]
    assert events[2].image_bytes == b"png"
    assert events[-1].login_name == "Alice"
    assert calls == ["https://f.wps.cn/g/private", str(tmp_path / "wps.json"), "closed"]


def test_wps_login_complete_requires_positive_evidence() -> None:
    class FakeLocator:
        def __init__(self, text: str = "", count: int = 0):
            self._text = text
            self._count = count

        def count(self):
            return self._count

        def inner_text(self, timeout=None):
            return self._text

    class FakePage:
        url = "https://account.wps.cn/login"

        def locator(self, selector):
            if selector == "body":
                return FakeLocator("扫码登录 金山账号")
            return FakeLocator(count=0)

    assert is_login_complete(FakePage()) is False


def test_wps_login_complete_rejects_login_page_with_checkbox() -> None:
    class FakeLocator:
        def __init__(self, text: str = "", count: int = 0):
            self._text = text
            self._count = count

        def count(self):
            return self._count

        def inner_text(self, timeout=None):
            return self._text

    class FakePage:
        url = "https://account.wps.cn/?qrcode=kdocs"

        def locator(self, selector):
            if selector == "body":
                return FakeLocator("账号密码登录")
            if "input[type='checkbox']" in selector:
                return FakeLocator(count=1)
            return FakeLocator(count=0)

    assert is_login_complete(FakePage()) is False


def test_wps_login_complete_accepts_strong_cookie_on_login_page() -> None:
    class FakeLocator:
        def __init__(self, text: str = "", count: int = 0):
            self._text = text
            self._count = count

        def count(self):
            return self._count

        def inner_text(self, timeout=None):
            return self._text

    class FakeContext:
        def cookies(self):
            return [{"name": "wps_sid", "value": "signed-in-session"}]

    class FakePage:
        url = "https://account.wps.cn/?qrcode=kdocs"
        context = FakeContext()

        def locator(self, selector):
            if selector == "body":
                return FakeLocator("账号密码登录")
            return FakeLocator(count=0)

    assert is_login_complete(FakePage()) is True


def test_wps_login_complete_rejects_weak_cookie_on_login_page() -> None:
    class FakeLocator:
        def __init__(self, text: str = "", count: int = 0):
            self._text = text
            self._count = count

        def count(self):
            return self._count

        def inner_text(self, timeout=None):
            return self._text

    class FakeContext:
        def cookies(self):
            return [{"name": "wpsua", "value": "tracking-only-cookie"}]

    class FakePage:
        url = "https://account.wps.cn/?qrcode=kdocs"
        context = FakeContext()

        def locator(self, selector):
            if selector == "body":
                return FakeLocator("账号密码登录")
            return FakeLocator(count=0)

    assert is_login_complete(FakePage()) is False


def test_wps_fetch_schema_raises_login_when_page_has_no_questions(tmp_path: Path) -> None:
    class FakeLocator:
        def __init__(self, text: str = "", count: int = 0):
            self._text = text
            self._count = count

        def count(self):
            return self._count

        def inner_text(self, timeout=None):
            return self._text

    class FakePage:
        url = "https://account.wps.cn/login"

        def goto(self, url, **kwargs):
            self.url = "https://account.wps.cn/login"

        def wait_for_selector(self, *args, **kwargs):
            raise TimeoutError

        def locator(self, selector):
            if selector == "body":
                return FakeLocator("扫码登录 金山账号")
            return FakeLocator(count=0)

        def title(self):
            return "登录"

        def evaluate(self, script):
            return []

    class FakeSession:
        page = FakePage()

        @classmethod
        def open(cls, *, storage_path, headless):
            return cls()

        def close(self):
            pass

    service = WpsFormService(session_path=tmp_path / "wps.json", browser_session_cls=FakeSession)

    with pytest.raises(WpsLoginRequired):
        service.fetch_schema("https://f.wps.cn/g/private")
