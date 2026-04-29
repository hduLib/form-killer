from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class ServiceEvent:
    kind: str
    provider: str | None = None
    provider_label: str | None = None
    message: str | None = None
    image_bytes: bytes | None = None
    login_name: str | None = None


ServiceEventHandler = Callable[[ServiceEvent], None]


class LoginTimeoutError(RuntimeError):
    pass
