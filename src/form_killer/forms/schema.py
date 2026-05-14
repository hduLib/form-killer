from __future__ import annotations

from typing import Any

from form_killer.forms.base import Option, Question


FORM_CONTROL_BINDINGS_KEY = "form_controls"


def build_control_binding(
    question: Question,
    *,
    handle: str | None = None,
    dom_index: int | None = None,
    selector: str | None = None,
    options: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    binding: dict[str, Any] = {
        "id": question.id,
        "label": question.label,
        "kind": question.kind,
        "handle": handle or question.id,
        "options": build_option_bindings(question.options, options or []),
    }
    if dom_index is not None:
        binding["dom_index"] = dom_index
    if selector:
        binding["selector"] = selector
    return binding


def build_option_bindings(options: list[Option], raw_options: list[dict[str, Any]]) -> list[dict[str, str]]:
    bindings: list[dict[str, str]] = []
    for index, option in enumerate(options):
        raw = raw_options[index] if index < len(raw_options) else {}
        bindings.append(
            {
                "id": option.id,
                "label": option.label,
                "handle": str(raw.get("handle") or option.id),
            }
        )
    return bindings


def control_bindings_by_question_id(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    bindings = raw.get(FORM_CONTROL_BINDINGS_KEY) or []
    return {str(binding.get("id")): binding for binding in bindings if binding.get("id")}


def option_handle_for_value(binding: dict[str, Any], value: Any) -> str | None:
    text = str(value)
    by_id: dict[str, str] = {}
    by_label: dict[str, str] = {}
    for option in binding.get("options") or []:
        handle = str(option.get("handle") or option.get("id") or "")
        if not handle:
            continue
        by_id[str(option.get("id") or "")] = handle
        by_label[str(option.get("label") or "").strip().lower()] = handle
    return by_id.get(text) or by_label.get(text.strip().lower())
