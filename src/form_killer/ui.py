from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from form_killer.forms.base import AnswerPayload, FormSchema, Question


def render_schema(console: Console, schema: FormSchema) -> None:
    table = Table(title=f"{schema.name} ({schema.id})", show_lines=True)
    table.add_column("#", justify="right", style="dim")
    table.add_column("类型", style="cyan")
    table.add_column("必填", justify="center")
    table.add_column("题目")
    table.add_column("选项", overflow="fold")
    for index, question in enumerate(schema.questions, 1):
        options = "\n".join(f"{option.id}: {option.label}" for option in question.options)
        table.add_row(
            str(index),
            question.kind,
            "是" if question.required else "",
            question.label,
            options,
        )
    console.print(table)


def render_output_header(console: Console, title: str, message: str) -> None:
    console.print(Panel(message, title=f"输出｜{title}", border_style="green"))


def render_input_header(console: Console, title: str, message: str) -> None:
    console.print(Panel(message, title=f"输入｜{title}", border_style="cyan"))


def render_user_input_plan(console: Console, schema: FormSchema, question_ids: set[str]) -> None:
    if not question_ids:
        return
    table = Table(title="需要你补充的内容", show_lines=True)
    table.add_column("#", justify="right", style="dim")
    table.add_column("类型", style="cyan")
    table.add_column("必填", justify="center")
    table.add_column("字段")
    for index, question in enumerate([q for q in schema.questions if q.id in question_ids], 1):
        table.add_row(str(index), question.kind, "是" if question.required else "", question.label)
    console.print(table)


def render_preview(console: Console, schema: FormSchema, answers: list[AnswerPayload]) -> None:
    answer_by_id = {answer.question_id: answer for answer in answers}
    table = Table(title="答案预览", show_lines=True)
    table.add_column("题目")
    table.add_column("来源", style="cyan")
    table.add_column("答案", overflow="fold")
    table.add_column("置信度", justify="right")
    table.add_column("说明", overflow="fold")
    for question in schema.questions:
        answer = answer_by_id.get(question.id)
        value = format_answer(question, answer.value) if answer else "[dim]缺失[/dim]"
        table.add_row(
            question.label,
            translate_source(answer.source) if answer else "缺失",
            value,
            "" if not answer or answer.confidence is None else f"{answer.confidence:.2f}",
            "" if not answer else (answer.rationale or ("需要用户补充" if answer.needs_user_input else "")),
        )
    console.print(table)


def render_error(console: Console, message: str) -> None:
    console.print(Panel(message, title="错误", border_style="red"))


def format_answer(question: Question, value: object) -> str:
    if question.is_choice:
        values = value if isinstance(value, list) else [value]
        labels = []
        by_id = {option.id: option.label for option in question.options}
        for option_id in values:
            labels.append(f"{option_id}: {by_id.get(str(option_id), str(option_id))}")
        return "\n".join(labels)
    return str(value)


def translate_source(source: str) -> str:
    return {
        "user": "用户",
        "llm": "AI",
        "file": "文件",
        "skipped": "跳过",
    }.get(source, source)
