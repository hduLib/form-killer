from __future__ import annotations

import re

from form_killer.forms.base import Question

PERSONAL_LABEL_RE = re.compile(
    r"\b("
    r"name|full\s*name|surname|first\s*name|last\s*name|"
    r"id|student\s*id|hdu\s*id|email|e-mail|phone|mobile|address|"
    r"passport|ssn|birth|birthday|variant|file|upload|report"
    r")\b|姓名|名字|电话|手机|邮箱|地址|身份证|学号|文件|上传",
    re.IGNORECASE,
)


def is_personal_question(question: Question) -> bool:
    if question.kind == "file":
        return True
    return bool(PERSONAL_LABEL_RE.search(question.label))
