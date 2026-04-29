from __future__ import annotations

from pathlib import Path

import pytest

from form_killer.forms.services import FormRouteError, resolve_form_service
from form_killer.forms.tencent import (
    TencentFormService,
    TencentLoginRequired,
    TencentUnsupportedDocumentError,
    classify_tencent_url,
    is_login_complete,
    parse_tencent_questions,
)
from form_killer.forms import tencent


def test_route_resolves_tencent_form_url() -> None:
    routed = resolve_form_service("https://docs.qq.com/form/page/DV2NkZnBGdUVWbEVa")

    assert routed.provider == "tencent"
    assert routed.document_type == "form"


def test_route_marks_tencent_non_form_as_unsupported() -> None:
    routed = resolve_form_service("https://docs.qq.com/sheet/DR3J2eXZ1Wk1rQmhp")

    assert routed.provider == "tencent"
    assert routed.document_type == "unsupported"


def test_route_rejects_unknown_hosts() -> None:
    with pytest.raises(FormRouteError):
        resolve_form_service("https://example.test/form")


def test_classify_tencent_url() -> None:
    assert classify_tencent_url("https://docs.qq.com/form/page/demo") == "form"
    assert classify_tencent_url("https://docs.qq.com/sheet/demo") == "unsupported"


def test_parse_tencent_questions_maps_fields_and_metadata() -> None:
    schema = parse_tencent_questions(
        [
            {"id": "name", "label": "考生姓名", "kind": "textarea", "required": True, "dom_index": 0},
            {
                "id": "degree",
                "label": "报考学位层次",
                "kind": "radio",
                "required": True,
                "dom_index": 1,
                "options": [{"id": "1", "label": "博士"}],
            },
        ],
        source_url="https://docs.qq.com/form/page/demo",
        title="腾讯示例表单",
    )

    assert schema.raw["provider"] == "tencent"
    assert schema.raw["document_type"] == "form"
    assert schema.raw["source_url"] == "https://docs.qq.com/form/page/demo"
    assert schema.questions[0].kind == "text"
    assert schema.questions[0].required is True
    assert schema.questions[1].kind == "enum"
    assert schema.questions[1].options[0].label == "博士"
    assert schema.raw["tencent_questions"][1]["dom_index"] == 1


def test_tencent_service_rejects_unsupported_document_type(tmp_path: Path) -> None:
    service = TencentFormService(document_type="unsupported", session_path=tmp_path / "tencent.json")

    with pytest.raises(TencentUnsupportedDocumentError):
        service.fetch_schema("https://docs.qq.com/sheet/demo")


def test_tencent_login_saves_storage_state(tmp_path: Path) -> None:
    calls: list[str] = []

    class FakeLocator:
        def __init__(self, count: int):
            self._count = count

        def count(self):
            return self._count

        def inner_text(self, timeout=None):
            return "form"

    class FakePage:
        url = "https://docs.qq.com/form/page/demo"
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
            assert storage_path == tmp_path / "tencent.json"
            assert headless is True
            return cls()

        def save_storage_state(self, path):
            calls.append(str(path))

        def close(self):
            calls.append("closed")

    service = TencentFormService(
        session_path=tmp_path / "tencent.json",
        browser_session_cls=FakeSession,
        login_timeout_seconds=1,
    )

    login_name = service.login(
        "https://docs.qq.com/form/page/demo",
        login_runner=lambda page, is_done, timeout: (
            calls.append(f"runner-before:{timeout}:{is_done()}"),
            setattr(page, "authenticated", True),
            calls.append(f"runner-after:{timeout}:{is_done()}"),
        ),
    )

    assert login_name == "Alice"
    assert calls == [
        "https://docs.qq.com/form/page/demo",
        "runner-before:1:False",
        "runner-after:1:True",
        str(tmp_path / "tencent.json"),
        "closed",
    ]


def test_tencent_login_skips_challenge_when_session_already_authenticated(tmp_path: Path) -> None:
    calls: list[str] = []

    class FakeLocator:
        def count(self):
            return 1

        def inner_text(self, timeout=None):
            return "form"

    class FakePage:
        url = "https://docs.qq.com/form/page/demo"

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

    service = TencentFormService(
        session_path=tmp_path / "tencent.json",
        browser_session_cls=FakeSession,
        login_timeout_seconds=1,
    )

    login_name = service.login(
        "https://docs.qq.com/form/page/demo",
        login_runner=lambda page, is_done, timeout: calls.append("runner"),
    )

    assert login_name == "Alice"
    assert calls == ["https://docs.qq.com/form/page/demo", str(tmp_path / "tencent.json"), "closed"]


def test_tencent_login_emits_events_until_success(monkeypatch, tmp_path: Path) -> None:
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
        url = "https://docs.qq.com/form/page/demo"
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

    monkeypatch.setattr(tencent, "login_challenge_image_bytes", lambda page: b"png")
    service = TencentFormService(
        session_path=tmp_path / "tencent.json",
        browser_session_cls=FakeSession,
        login_timeout_seconds=5,
    )

    login_name = service.login("https://docs.qq.com/form/page/demo", event_handler=events.append)

    assert login_name == "Alice"
    assert [event.kind for event in events] == ["checking", "challenge_fetching", "challenge", "waiting", "success"]
    assert events[2].image_bytes == b"png"
    assert events[-1].login_name == "Alice"
    assert calls == ["https://docs.qq.com/form/page/demo", str(tmp_path / "tencent.json"), "closed"]


def test_tencent_login_complete_requires_positive_evidence() -> None:
    class FakeLocator:
        def __init__(self, text: str = "", count: int = 0):
            self._text = text
            self._count = count

        def count(self):
            return self._count

        def inner_text(self, timeout=None):
            return self._text

    class FakePage:
        url = "https://docs.qq.com/login"

        def locator(self, selector):
            if selector == "body":
                return FakeLocator("扫码登录")
            return FakeLocator(count=0)

    assert is_login_complete(FakePage()) is False


def test_tencent_login_complete_rejects_login_page_with_controls() -> None:
    class FakeLocator:
        def __init__(self, text: str = "", count: int = 0):
            self._text = text
            self._count = count

        def count(self):
            return self._count

        def inner_text(self, timeout=None):
            return self._text

    class FakePage:
        url = "https://docs.qq.com/login"

        def locator(self, selector):
            if selector == "body":
                return FakeLocator("扫码登录")
            if "textarea" in selector:
                return FakeLocator(count=1)
            return FakeLocator(count=0)

    assert is_login_complete(FakePage()) is False


def test_tencent_login_complete_accepts_strong_cookie_on_login_page() -> None:
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
            return [{"name": "uid_key", "value": "signed-in-session"}]

    class FakePage:
        url = "https://docs.qq.com/login"
        context = FakeContext()

        def locator(self, selector):
            if selector == "body":
                return FakeLocator("扫码登录")
            return FakeLocator(count=0)

    assert is_login_complete(FakePage()) is True


def test_tencent_login_complete_rejects_weak_cookie_on_login_page() -> None:
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
            return [{"name": "uid", "value": "tracking-only-cookie"}]

    class FakePage:
        url = "https://docs.qq.com/login"
        context = FakeContext()

        def locator(self, selector):
            if selector == "body":
                return FakeLocator("扫码登录")
            return FakeLocator(count=0)

    assert is_login_complete(FakePage()) is False


def test_tencent_fetch_schema_raises_login_when_page_has_no_questions(tmp_path: Path) -> None:
    class FakeLocator:
        def __init__(self, text: str = "", count: int = 0):
            self._text = text
            self._count = count

        def count(self):
            return self._count

        def inner_text(self, timeout=None):
            return self._text

    class FakePage:
        url = "https://docs.qq.com/login"

        def goto(self, url, **kwargs):
            self.url = "https://docs.qq.com/login"

        def wait_for_selector(self, *args, **kwargs):
            raise TimeoutError

        def locator(self, selector):
            if selector == "body":
                return FakeLocator("扫码登录")
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

    service = TencentFormService(session_path=tmp_path / "tencent.json", browser_session_cls=FakeSession)

    with pytest.raises(TencentLoginRequired):
        service.fetch_schema("https://docs.qq.com/form/page/demo")
