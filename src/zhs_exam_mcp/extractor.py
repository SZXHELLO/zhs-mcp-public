"""读题：RawQuestion（页面/接口原始数据）→ Question（规范化文本 + qid + 快照）。

模式 B（DOM）：逐题切换后由适配器读取。
模式 A（接口）：待阶段0侦察确认 doHomework 结构后在 extractor_api 中实现。
"""
from __future__ import annotations

import re
import uuid

from .store import compute_qid
from .formula import html_to_text, strip_option_label
from .models import Question, RawQuestion, Snapshot, now

LETTERS = "ABCDEFGH"
_HEADER_RE = re.compile(r"^\s*(\d+)\s*[.．、]?\s*【([^】]+)】\s*(?:[（(]\s*([\d.]+)\s*分\s*[)）])?")


def parse_header(text: str) -> tuple[int | None, str, float | None]:
    m = _HEADER_RE.search(text or "")
    if not m:
        return None, "", None
    return int(m.group(1)), m.group(2), float(m.group(3)) if m.group(3) else None


def map_type(type_text: str, n_options: int) -> str:
    if "多选" in type_text:
        return "multi"
    if "判断" in type_text:
        return "judge"
    return "single"


def new_snapshot_id() -> str:
    return uuid.uuid4().hex[:10]


def raw_texts(raw: RawQuestion) -> tuple[str, list[str], bool]:
    stem, lossy = html_to_text(raw.stem_html)
    stem = _HEADER_RE.sub("", stem, count=1).strip()
    opts = []
    for o in raw.options:
        t, l2 = html_to_text(o.html)
        opts.append(strip_option_label(t))
        lossy = lossy or l2
    return stem, opts, lossy


def raw_to_question(raw: RawQuestion, exam_key: str, snapshot_id: str) -> Question:
    stem, opts, lossy = raw_texts(raw)
    label_map = {LETTERS[i]: t for i, t in enumerate(opts)}
    option_ids = {LETTERS[i]: o.option_id for i, o in enumerate(raw.options) if o.option_id}
    return Question(
        qid=compute_qid(stem, opts),
        platform_id=raw.platform_id,
        exam_key=exam_key,
        no=raw.no,
        type=map_type(raw.type_text, len(opts)),  # type: ignore[arg-type]
        score=raw.score,
        stem=stem,
        options=opts,
        formula_lossy=lossy,
        snapshot=Snapshot(snapshot_id=snapshot_id, label_map=label_map, option_ids=option_ids or None),
        page_answer=[opts[i] for i, o in enumerate(raw.options) if o.selected],
        last_seen=now(),
    )
