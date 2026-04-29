from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Literal, Protocol

from pydantic import BaseModel, Field

from form_killer.events import ServiceEventHandler


class FormServiceError(RuntimeError):
    pass


class FormLoginRequired(FormServiceError):
    def __init__(self, url: str, message: str) -> None:
        self.url = url
        super().__init__(message)


QuestionKind = Literal[
    "string",
    "text",
    "enum",
    "checkbox",
    "dropdown",
    "boolean",
    "integer",
    "file",
    "unsupported",
]


class Option(BaseModel):
    id: str
    label: str


class Question(BaseModel):
    id: str
    label: str
    kind: QuestionKind
    raw_type: str
    widget: str | None = None
    required: bool = False
    hidden: bool = False
    multiline: bool = False
    options: list[Option] = Field(default_factory=list)
    validations: list[dict[str, Any]] = Field(default_factory=list)
    max_file_count: int | None = None
    max_file_size_mb: int | None = None

    @property
    def is_choice(self) -> bool:
        return self.kind in {"enum", "checkbox", "dropdown"}


class FormSchema(BaseModel):
    id: str
    name: str
    submit_text: str = "Submit"
    questions: list[Question]
    raw: dict[str, Any] = Field(default_factory=dict)


class AnswerPayload(BaseModel):
    question_id: str
    value: Any
    source: Literal["user", "llm", "file", "skipped"] = "llm"
    confidence: float | None = None
    rationale: str | None = None
    needs_user_input: bool = False


class FormAdapter(Protocol):
    def fetch_schema(self, url: str) -> FormSchema:
        raise NotImplementedError

    def login(
        self,
        url: str,
        wait_for_user: Callable[[], None] | None = None,
        login_runner: Callable[[Any, Callable[[], bool], int], None] | None = None,
        event_handler: ServiceEventHandler | None = None,
    ) -> str | None:
        raise NotImplementedError

    def upload_file(self, schema: FormSchema, question: Question, path: Path) -> Any:
        raise NotImplementedError

    def submit(
        self,
        schema: FormSchema,
        values: dict[str, Any],
        *,
        dry_run: bool,
        event_handler: ServiceEventHandler | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError


class FormService(Protocol):
    provider: str
    label: str
    document_type: str
    supports_login: bool
    login_required_error: type[Exception] | None

    def fetch_schema(self, url: str, event_handler: ServiceEventHandler | None = None) -> FormSchema:
        raise NotImplementedError

    def login(
        self,
        url: str,
        wait_for_user: Callable[[], None] | None = None,
        login_runner: Callable[[Any, Callable[[], bool], int], None] | None = None,
        event_handler: ServiceEventHandler | None = None,
    ) -> str | None:
        raise NotImplementedError

    def upload_file(self, schema: FormSchema, question: Question, path: Path) -> Any:
        raise NotImplementedError

    def submit(
        self,
        schema: FormSchema,
        values: dict[str, Any],
        *,
        dry_run: bool,
        event_handler: ServiceEventHandler | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def verify_submission(self, schema: FormSchema, answer_key: str) -> dict[str, Any]:
        raise NotImplementedError
