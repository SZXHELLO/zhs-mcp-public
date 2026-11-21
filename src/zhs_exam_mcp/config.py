"""配置加载。

查找顺序：环境变量 ZHS_CONFIG → 项目目录下 config.yaml → 项目目录下 config.example.yaml（仓库自带的默认值）。
配置中的相对路径一律以项目目录（zhs-exam-mcp/）为基准，与配置文件放在哪里无关。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

PROJECT_DIR = Path(__file__).resolve().parents[2]


class ApiConfig(BaseModel):
    paper_url_pattern: str = "doHomework"
    answer_url_pattern: str = "getStuAnswerInfoNew"


class Selectors(BaseModel):
    exam_title: str | None = None
    question_root: str | None = None
    question_header: str | None = None
    question_stem: str | None = None
    option_item: str | None = None
    option_label: str | None = None
    option_content: str | None = None
    option_click_target: str | None = None
    option_selected: str | None = None
    card_item: str | None = None
    card_answered: str | None = None
    card_current: str | None = None
    dialog: str | None = None
    next_button_text: str = "下一题"
    last_save_button_text: str = "保存"
    prev_button_text: str = "上一题"
    save_button_text: str = "暂存作业"


class ListConfig(BaseModel):
    """「作业考试」列表页（侦察结论见 docs/FINDINGS.md）。"""
    url_match: str = "#/webExamList"
    homework_url_pattern: str = "/student/getStudentHomework"
    card: str = ".examItemWrap"
    card_title: str = ".examTit .name"
    start_button: str = ".examChartBox1 span.themeBg"
    start_button_texts: list[str] = Field(default_factory=lambda: ["开始答题", "继续答题"])
    allowed_name_pattern: str = "单元测试|章节测试|章测试"
    open_timeout_s: float = 25.0


class Config(BaseModel):
    cdp_url: str = "http://127.0.0.1:9222"
    page_url_match: str = "onlineexamh5new.zhihuishu.com"
    page_url_prefer: str = "/dohomework/"
    runtime_dir: Path = Path("runtime")
    low_confidence_threshold: float = 0.7
    extract_mode: Literal["dom", "api"] = "api"
    save_action: Literal["next", "none"] = "next"
    last_question_save: Literal["save_progress", "none"] = "none"
    delay_min: float = 0.8
    delay_max: float = 2.0
    max_consecutive_failures: int = 2
    render_timeout_ms: int = 5000
    submit_button_text: str = "提交作业"
    submit_confirm_texts: list[str] = Field(default_factory=lambda: ["确定", "确认", "确认提交"])
    api: ApiConfig = Field(default_factory=ApiConfig)
    exam_list: ListConfig = Field(default_factory=ListConfig)
    selectors: Selectors = Field(default_factory=Selectors)
    config_file: str | None = None          # 实际读取的配置文件（运行时填写）

    @property
    def logs_dir(self) -> Path:
        return self.runtime_dir / "logs"

    @property
    def screenshots_dir(self) -> Path:
        return self.runtime_dir / "screenshots"


def find_config_file(path: str | os.PathLike | None = None) -> Path | None:
    explicit = path or os.environ.get("ZHS_CONFIG")
    if explicit:
        p = Path(explicit).expanduser()
        if not p.exists():
            raise FileNotFoundError(f"找不到配置文件 {p}（来自 ZHS_CONFIG 或参数）")
        return p
    for name in ("config.yaml", "config.example.yaml"):
        p = PROJECT_DIR / name
        if p.exists():
            return p
    return None


def _resolve(p: Path) -> Path:
    p = p.expanduser()
    return p if p.is_absolute() else (PROJECT_DIR / p).resolve()


def load_config(path: str | os.PathLike | None = None) -> Config:
    p = find_config_file(path)
    data = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p else {}
    cfg = Config.model_validate(data)
    cfg.config_file = str(p) if p else None
    cfg.runtime_dir = _resolve(cfg.runtime_dir)
    return cfg

