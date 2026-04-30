from __future__ import annotations

import pytest

from form_killer.forms.base import FormSchema
from form_killer.forms.services import (
    FormRouteError,
    RoutedFormService,
    _discover_builtin_form_provider_modules,
    form_providers,
    register_form_provider,
    resolve_form_service,
    unregister_form_provider,
)
from form_killer.forms.yandex import YandexFormService


class FakeService:
    provider = "fake"
    document_type = "form"


class FakeProvider:
    name = "fake"
    label = "Fake Forms"

    def resolve(self, url: str):
        if url.startswith("fake://"):
            return RoutedFormService(provider=self.name, document_type="form", service=FakeService())
        return None


def test_resolve_form_service_accepts_explicit_provider_plugins() -> None:
    routed = resolve_form_service("fake://demo", providers=[FakeProvider()])

    assert routed.provider == "fake"
    assert routed.document_type == "form"
    assert isinstance(routed.service, FakeService)


def test_builtin_providers_register_themselves() -> None:
    names = {provider.name for provider in form_providers()}

    assert {"yandex", "tencent", "wps"} <= names


def test_builtin_provider_modules_are_discovered_by_convention() -> None:
    modules = _discover_builtin_form_provider_modules()

    assert "form_killer.forms.yandex" in modules
    assert "form_killer.forms.tencent" in modules
    assert "form_killer.forms.wps" in modules
    assert "form_killer.forms.services" not in modules


def test_resolve_form_service_can_disable_builtin_providers() -> None:
    with pytest.raises(FormRouteError) as exc_info:
        resolve_form_service("https://forms.yandex.ru/u/demo/", providers=[])

    assert "已注册的表单 provider" in str(exc_info.value)


def test_register_form_provider_replaces_provider_by_name() -> None:
    try:
        register_form_provider(FakeProvider())
        register_form_provider(FakeProvider())

        matching = [provider for provider in form_providers() if provider.name == "fake"]

        assert len(matching) == 1
    finally:
        unregister_form_provider("fake")


def test_form_service_emits_generic_events() -> None:
    events = []

    class FakeAdapter:
        def fetch_schema(self, url: str):
            return FormSchema(id="s1", name="Demo", questions=[], raw={})

        def submit(self, schema, values, *, dry_run):
            return {"ok": True}

    service = YandexFormService(FakeAdapter())
    schema = service.fetch_schema("https://forms.yandex.ru/u/s1/", event_handler=events.append)
    service.submit(schema, {}, dry_run=True, event_handler=events.append)

    assert [event.kind for event in events] == ["parse_start", "dry_run_start"]
    assert {event.provider_label for event in events} == {"Yandex Forms"}
