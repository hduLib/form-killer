from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

from form_killer.forms.base import FormService


Provider = str
DocumentType = str
BUILTIN_FORM_PROVIDER_PACKAGE = "form_killer.forms"
_FORM_PROVIDER_INFRASTRUCTURE_MODULES = frozenset({"base", "browser_common", "services"})
_FORM_PROVIDER_PLUGINS_LOADED = False


class FormRouteError(RuntimeError):
    pass


@dataclass(frozen=True)
class RoutedFormService:
    provider: Provider
    document_type: DocumentType
    service: FormService


class FormProvider(Protocol):
    name: str
    label: str

    def resolve(self, url: str) -> RoutedFormService | None:
        raise NotImplementedError


@dataclass(frozen=True)
class HostFormProvider:
    name: str
    label: str
    service_factory: Callable[[DocumentType], FormService]
    classify_url: Callable[[str], DocumentType]
    hosts: tuple[str, ...] = ()
    host_suffixes: tuple[str, ...] = ()

    def resolve(self, url: str) -> RoutedFormService | None:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if not self._matches_host(host):
            return None
        document_type = self.classify_url(url)
        return RoutedFormService(
            provider=self.name,
            document_type=document_type,
            service=self.service_factory(document_type),
        )

    def _matches_host(self, host: str) -> bool:
        return host in self.hosts or any(host.endswith(suffix) for suffix in self.host_suffixes)


_FORM_PROVIDER_REGISTRY: list[FormProvider] = []


def load_form_provider_plugins(*, force: bool = False) -> None:
    global _FORM_PROVIDER_PLUGINS_LOADED
    if _FORM_PROVIDER_PLUGINS_LOADED and not force:
        return
    for module_name in _discover_builtin_form_provider_modules():
        importlib.import_module(module_name)
    _FORM_PROVIDER_PLUGINS_LOADED = True


def _discover_builtin_form_provider_modules() -> tuple[str, ...]:
    package = importlib.import_module(BUILTIN_FORM_PROVIDER_PACKAGE)
    package_paths = getattr(package, "__path__", None)
    if package_paths is None:
        return ()

    modules = []
    for module_info in pkgutil.iter_modules(package_paths, prefix=f"{BUILTIN_FORM_PROVIDER_PACKAGE}."):
        short_name = module_info.name.rsplit(".", maxsplit=1)[-1]
        if short_name.startswith("_") or short_name in _FORM_PROVIDER_INFRASTRUCTURE_MODULES:
            continue
        modules.append(module_info.name)
    return tuple(sorted(modules))


def register_form_provider(provider: FormProvider) -> None:
    for index, registered in enumerate(_FORM_PROVIDER_REGISTRY):
        if registered.name == provider.name:
            _FORM_PROVIDER_REGISTRY[index] = provider
            return
    _FORM_PROVIDER_REGISTRY.append(provider)


def unregister_form_provider(name: str) -> None:
    _FORM_PROVIDER_REGISTRY[:] = [provider for provider in _FORM_PROVIDER_REGISTRY if provider.name != name]


def form_providers() -> tuple[FormProvider, ...]:
    load_form_provider_plugins()
    return tuple(_FORM_PROVIDER_REGISTRY)


def resolve_form_service(url: str, *, providers: Iterable[FormProvider] | None = None) -> RoutedFormService:
    active_providers = form_providers() if providers is None else tuple(providers)
    for provider in active_providers:
        routed = provider.resolve(url)
        if routed is not None:
            return routed

    raise FormRouteError(f"暂不支持该 URL。当前支持 {supported_provider_labels(active_providers)}。")


def supported_provider_labels(providers: Iterable[FormProvider] | None = None) -> str:
    active_providers = form_providers() if providers is None else tuple(providers)
    labels = [provider.label for provider in active_providers]
    return "、".join(labels) if labels else "已注册的表单 provider"
