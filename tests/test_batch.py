"""作业考试列表 + 批量任务：解析、章节解析、批量规则（全部暂存前不能提交）。"""
import json
from pathlib import Path

import pytest

from zhs_exam_mcp.adapter import ListAdapter, OpenBlocked
from zhs_exam_mcp.config import Config
from zhs_exam_mcp.exam_list import cn_to_int, parse_homework_list, resolve_targets
from zhs_exam_mcp.safe_click import SubmitGuard
from zhs_exam_mcp.service import ActionLog, ExamService, ToolError

from fake_page import sample_page

FX = Path(__file__).parent / "fixtures"
PAT = Config().exam_list.allowed_name_pattern


def _items():
    return parse_homework_list(json.loads((FX / "homework_list_sample.json").read_text(encoding="utf-8")), PAT)


def test_cn_to_int():
    assert [cn_to_int(x) for x in ("第四章", "第十章", "第十二章", "第二十章", "第23章", "无")] == [4, 10, 12, 20, 23, None]


def test_parse_list_filters_non_chapter_tests():
    items = _items()
    assert [i.chapter for i in items] == [4, 5, 6, 7, 8, 9, 10]
    assert all("单元测试" in i.name for i in items)          # 「期中大作业」被排除
    assert items[0].answered == 26 and items[0].status_text == "已全部作答（未提交）"
    assert items[1].status_text == "未作答"
    assert items[0].course == "计算机组成与系统结构"


def test_resolve_targets():
    found, missing = resolve_targets(_items(), ["4", 6, "第八章", "第三章", "4"])
    assert [f.name for f in found] == ["第四章单元测试", "第六章单元测试", "第八章单元测试"]
    assert len(missing) == 1 and "第三章" in missing[0]


# ---------------- 批量流程（假列表页 + 假做题页） ----------------

class FakeList(ListAdapter):
    def __init__(self, svc, guard, block=None):
        self.svc, self.guard, self.block = svc, guard, block or set()
        self.opened = []
        self.pages = {}

    async def items(self, refresh=True):
        return _items()

    async def open(self, name):
        if name in self.block:
            raise OpenBlocked("请进行人脸识别验证")
        self.opened.append(name)
        page = self.pages.setdefault(name, sample_page(seed=len(self.pages), guard=self.guard, title=name))
        self.svc.adapter = page


async def _nosleep(_):
    return None


@pytest.fixture
def env(tmp_path):
    cfg = Config(runtime_dir=tmp_path / "rt")
    guard = SubmitGuard()
    svc = ExamService(cfg, guard=guard, action_log=ActionLog(tmp_path / "a.jsonl"), sleep=_nosleep)
    svc.list_adapter = FakeList(svc, guard)
    return svc


async def _do_one(svc):
    """模拟 agent 完成当前章节：读题→按真实答案作答→填写→verify→暂存。"""
    want = {1: ["静态刷新"], 2: ["SRAM", "DRAM"], 3: ["1MB"], 4: ["提高速度"]}  # 第3题与用户原答案一致
    ex = await svc.extract_questions()
    items = []
    for q in ex["questions"]:
        inv = {v: k for k, v in q["options"].items()}
        items.append({"qid": q["qid"], "answer": [inv[t] for t in want[q["no"]]], "confidence": 0.9})
    await svc.save_answers(ex["snapshot_id"], items)
    rep = await svc.fill_answers()
    assert rep["remaining"] == 0
    assert (await svc.verify(reload=True))["all_consistent"]
    return await svc.save_progress()


async def test_batch_flow_no_submit_until_all_saved(env):
    svc = env
    b = await svc.start_batch(["4", "6"])
    assert [i["name"] for i in b["batch"]] == ["第四章单元测试", "第六章单元测试"]

    r = await svc.open_exam("4")
    assert r["opened"] == "第四章单元测试"
    # 第一个章节已全部作答且 verify 通过，但批量未完成 → 拒绝提交
    want = await _do_one(svc)
    assert want["batch_next"] == "第六章单元测试"
    # 模拟页面关闭后仍在本章（adapter 仍指向它）
    sub = await svc.submit_exam(confirm=True)
    assert not sub["submitted"] and "批量任务未完成" in sub["unmet"][0]
    assert not svc.list_adapter.pages["第四章单元测试"].submitted

    st = await svc.batch_status()
    assert not st["all_saved"] and st["next"] == "第六章单元测试"

    await svc.open_exam("6")
    done = await _do_one(svc)
    assert done["batch_next"] is None and "全部暂存完成" in done["note"]
    st = await svc.batch_status()
    assert st["all_saved"] and st["pending_reviews"] == []
    assert [i["status"] for i in st["items"]] == ["saved", "saved"]

    # 用户确认后：重新打开第四章 → verify → 提交
    await svc.open_exam("4")
    assert (await svc.verify(reload=True))["all_consistent"]
    assert (await svc.submit_exam(confirm=False))["submitted"] is False
    sub = await svc.submit_exam(confirm=True)
    assert sub["submitted"] and svc.list_adapter.pages["第四章单元测试"].submitted
    assert [i["status"] for i in (await svc.batch_status())["items"]] == ["submitted", "saved"]


async def test_open_blocked_stops_batch(env):
    svc = env
    svc.list_adapter.block = {"第六章单元测试"}
    await svc.start_batch([6, 8])
    with pytest.raises(ToolError, match="人脸识别"):
        await svc.open_exam("6")
    st = await svc.batch_status()
    assert st["stopped"] and st["items"][0]["status"] == "failed"
    with pytest.raises(ToolError, match="已中止"):
        await svc.open_exam("8")   # 中止后不再继续
    await svc.end_batch()
    assert (await svc.batch_status())["batch"] is None


async def test_start_batch_rejects_unknown_and_running(env):
    svc = env
    with pytest.raises(ToolError):
        await svc.start_batch(["第三章"])
    await svc.start_batch(["5"])
    with pytest.raises(ToolError, match="未完成的批量任务"):
        await svc.start_batch(["7"])
