"""MCP 服务器（stdio，服务器名 zhs）。stdout 仅供 MCP 协议，日志写文件/stderr。"""
from __future__ import annotations

import logging
import sys
from typing import Any

from mcp.server.mcpserver import MCPServer

from .browser import BrowserSession, DomAdapter, DomListAdapter
from .config import load_config
from .safe_click import SubmitGuard
from .service import ActionLog, ExamService, ToolError

cfg = load_config()
cfg.logs_dir.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.FileHandler(cfg.logs_dir / "server.log", encoding="utf-8"),
              logging.StreamHandler(sys.stderr)],
)
log = logging.getLogger("zhs")
log.info("配置：%s", cfg.config_file or "内置默认值")

mcp = MCPServer("zhs")
_session = BrowserSession(cfg)
_guard = SubmitGuard()
_actions = ActionLog(cfg.logs_dir / "actions.jsonl")
_svc = ExamService(cfg, guard=_guard, action_log=_actions)
_svc.adapter = DomAdapter(_session, _guard, _actions)
_svc.list_adapter = DomListAdapter(_session, _actions)


async def _run(name: str, coro) -> Any:
    try:
        return await coro
    except ToolError as e:
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001
        log.exception("tool %s failed", name)
        return {"error": f"{type(e).__name__}: {e}"}


@mcp.tool()
async def connect(cdp_url: str | None = None) -> dict:
    """连接已登录的 Edge（CDP）并定位章节测试页；返回课程、标题、章节、题目数、截止时间。"""
    async def go():
        await _session.connect(cdp_url)
        return await _svc.connect()
    return await _run("connect", go())


@mcp.tool()
async def get_overview() -> dict:
    """每题：题号、页面是否已答、origin、是否已有 AI 答案；以及完成率。"""
    return await _run("get_overview", _svc.get_overview())


@mcp.tool()
async def extract_questions(nos: list[int] | None = None, refresh: bool = False) -> dict:
    """读题（结果只保存在本次服务器进程的内存中）。返回 snapshot_id 和每题 {qid,no,type,score,stem,options(字母→文本),page_answer(字母),has_ai}。
    字母只在本次 snapshot 内有效（选项顺序随机）。首次调用会刷新页面以捕获题目数据；refresh=true 强制刷新。"""
    return await _run("extract_questions", _svc.extract_questions(nos, refresh))


@mcp.tool()
async def save_answers(snapshot_id: str, items: list[dict], model: str = "") -> dict:
    """保存 AI 答案。items: [{qid, answer: ["B","D"], explanation, confidence(0~1)}]，字母按 snapshot_id 对应的快照。"""
    return await _run("save_answers", _svc.save_answers(snapshot_id, items, model))


@mcp.tool()
async def fill_answers(scope: str = "unanswered", nos: list[int] | None = None,
                       dry_run: bool = False, max_items: int = 10) -> dict:
    """在网页上填写 final_answer（不修改用户自己作答的题）。scope: "unanswered" | "nos"。
    每次最多 max_items 题，返回 remaining，循环调用直到 remaining=0。"""
    return await _run("fill_answers", _svc.fill_answers(scope, nos, dry_run, max_items))


@mcp.tool()
async def save_progress() -> dict:
    """点击「暂存作业」并检查是否有报错弹窗。"""
    return await _run("save_progress", _svc.save_progress())


@mcp.tool()
async def verify(reload: bool = True, nos: list[int] | None = None) -> dict:
    """reload 后重读，比较页面选中项与 final_answer，列出不一致（确认已保存到服务器）。"""
    return await _run("verify", _svc.verify(reload, nos))


@mcp.tool()
async def get_review_list(exam_key: str | None = None, threshold: float | None = None) -> dict:
    """汇总待确认题（低置信度 / 我的答案与 AI 不一致 / 公式有损或填写失败），结果在返回值中（不写文件）。"""
    return await _run("get_review_list", _svc.get_review_list(exam_key, threshold))


@mcp.tool()
async def apply_user_decisions(items: list[dict]) -> dict:
    """写入用户裁决并同步到网页。items: [{qid, answer: 字母(按 get_review_list 的字母) 或 选项文本}]。"""
    return await _run("apply_user_decisions", _svc.apply_user_decisions(items))


@mcp.tool()
async def submit_exam(confirm: bool = False) -> dict:
    """点击「提交作业」。仅当提交条件全部满足才执行：全部有最终答案、verify(reload) 全部一致、
    待确认清单为空、且 confirm=true（只有用户在对话中明确同意提交后才可传 true）。否则返回未满足项。"""
    return await _run("submit_exam", _svc.submit_exam(confirm))


@mcp.tool()
async def list_exams(refresh: bool = True) -> dict:
    """读取「作业考试」列表页中「未提交」的章节测试（章节号、名称、题数、平台上已答题数、截止时间）。
    期末在线考试不在其中，也永远不会被处理。"""
    return await _run("list_exams", _svc.list_exams(refresh))


@mcp.tool()
async def start_batch(chapters: list[str]) -> dict:
    """开始批量任务，如 chapters=["4","6","8"] 或 ["第四章"]。之后逐个 open_exam 完成并暂存。
    批量任务未全部暂存前，submit_exam 一律拒绝；也不要中途询问用户是否提交。"""
    return await _run("start_batch", _svc.start_batch(chapters))


@mcp.tool()
async def open_exam(chapter: str) -> dict:
    """在「作业考试」列表页点击该章节测试的「开始答题/继续答题」，切换到新打开的做题页并 connect。
    平台弹出任何提示（人脸/定位/异常行为/锁定）都会停止并要求用户处理。"""
    return await _run("open_exam", _svc.open_exam(chapter))


@mcp.tool()
async def batch_status() -> dict:
    """批量任务进度：各章节状态、作答数、待确认题（pending_reviews）、下一项。"""
    return await _run("batch_status", _svc.batch_status())


@mcp.tool()
async def end_batch() -> dict:
    """结束（清除）批量任务。"""
    return await _run("end_batch", _svc.end_batch())


def main() -> None:
    mcp.run()  # stdio


if __name__ == "__main__":
    main()
