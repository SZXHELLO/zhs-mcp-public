"""数据模型。答案一律以「选项文本」存储（选项顺序随机，字母不稳定）。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, computed_field

QType = Literal["single", "multi", "judge"]
Origin = Literal["user", "tool", "empty"]


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Snapshot(BaseModel):
    snapshot_id: str
    label_map: dict[str, str]                 # "A" -> 选项文本
    option_ids: dict[str, str] | None = None  # "A" -> 平台选项 id（若有）


class AIAnswer(BaseModel):
    answer: list[str]
    explanation: str = ""
    confidence: float = Field(ge=0.0, le=1.0)
    model: str = ""
    at: str = Field(default_factory=now)


class UserDecision(BaseModel):
    answer: list[str]
    at: str = Field(default_factory=now)


class Question(BaseModel):
    qid: str
    platform_id: str | None = None
    exam_key: str
    no: int
    type: QType
    score: float | None = None
    stem: str
    options: list[str]
    formula_lossy: bool = False
    snapshot: Snapshot | None = None
    page_answer: list[str] = Field(default_factory=list)
    origin: Origin = "empty"
    ai: AIAnswer | None = None
    user_decision: UserDecision | None = None
    correct_answer: list[str] | None = None
    fill_error: str | None = None
    first_seen: str = Field(default_factory=now)
    last_seen: str = Field(default_factory=now)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def final_answer(self) -> list[str] | None:
        if self.user_decision is not None:
            return list(self.user_decision.answer)
        if self.ai is not None:
            return list(self.ai.answer)
        return None


class ExamFile(BaseModel):
    exam_key: str          # "<课程>/<测试标题>"
    course: str
    title: str
    chapter: str | None = None
    deadline: str | None = None
    total: int | None = None
    updated_at: str = Field(default_factory=now)
    questions: list[Question] = Field(default_factory=list)

    def by_qid(self, qid: str) -> Question | None:
        return next((q for q in self.questions if q.qid == qid), None)

    def by_no(self, no: int) -> Question | None:
        return next((q for q in self.questions if q.no == no), None)


# ---------- 页面原始读取结果（适配器 → 业务逻辑） ----------

class RawOption(BaseModel):
    label: str = ""            # 页面显示的字母（可能为空）
    html: str                  # 选项内容 HTML
    selected: bool = False
    option_id: str | None = None


class RawQuestion(BaseModel):
    no: int
    type_text: str             # 如「单选题」
    score: float | None = None
    stem_html: str
    options: list[RawOption]
    platform_id: str | None = None


class CardItem(BaseModel):
    no: int
    answered: bool
    current: bool = False


class ExamInfo(BaseModel):
    course: str
    title: str
    chapter: str | None = None
    deadline: str | None = None
    total: int | None = None
