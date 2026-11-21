"""端到端（假页面）：extract → save_answers → fill → verify → review → decisions → submit。"""
import pytest

from zhs_exam_mcp.config import Config
from zhs_exam_mcp.safe_click import SubmitGuard
from zhs_exam_mcp.service import ActionLog, ExamService

from fake_page import sample_page


async def _nosleep(_):
    return None


@pytest.fixture(params=["api", "dom"])
def env(tmp_path, request):
    cfg = Config(runtime_dir=tmp_path / "rt", extract_mode=request.param)
    guard = SubmitGuard()
    page = sample_page(seed=1, guard=guard)
    svc = ExamService(cfg, adapter=page, guard=guard, action_log=ActionLog(tmp_path / "a.jsonl"), sleep=_nosleep)
    return svc, page


def _letter(res, no, text):
    q = next(x for x in res["questions"] if x["no"] == no)
    return next(k for k, v in q["options"].items() if v == text), q["qid"]


async def _answer_all(svc, conf=0.9):
    res = await svc.extract_questions()
    want = {1: (["静态刷新"], conf), 2: (["SRAM", "DRAM"], conf), 3: (["1KB"], conf), 4: (["提高速度"], conf)}
    items = []
    for no, (texts, c) in want.items():
        letters = [_letter(res, no, t)[0] for t in texts]
        items.append({"qid": _letter(res, no, texts[0])[1], "answer": letters, "explanation": "x", "confidence": c})
    return res, await svc.save_answers(res["snapshot_id"], items)


async def test_full_flow(env):
    svc, page = env
    info = await svc.connect()
    assert info["exam_key"] == "计算机组成与系统结构/第三章单元测试"
    res, saved = await _answer_all(svc)
    assert saved["saved"] == [1, 2, 3, 4]
    assert next(q for q in res["questions"] if q["no"] == 3)["origin"] == "user"

    rep = await svc.fill_answers("unanswered", max_items=10)
    assert sorted(rep["filled"]) == [1, 2, 4] and rep["remaining"] == 0
    assert page.qs[3].saved == {"1MB"}           # 用户答的题没被动
    assert page.qs[2].saved == {"SRAM", "DRAM"}

    v = await svc.verify(reload=True)
    assert v["mismatches"] == [] or [m["no"] for m in v["mismatches"]] == [3]

    review = await svc.get_review_list()
    assert [q["no"] for q in review["groups"]["disagree"]] == [3]

    sub = await svc.submit_exam(confirm=True)
    assert not sub["submitted"] and not page.submitted

    # 用户裁决：第 3 题改为 AI 的答案（按 review 字母）
    item = review["groups"]["disagree"][0]
    d = await svc.apply_user_decisions([{"qid": item["qid"], "answer": item["ai_answer"]}])
    assert d["fill"]["filled"] == [3] and page.qs[3].saved == {"1KB"}

    # verify 之前不允许提交
    assert not (await svc.submit_exam(confirm=True))["submitted"]
    v = await svc.verify(reload=True)
    assert v["all_consistent"]
    assert (await svc.get_review_list())["count"] == 0
    # 未确认不提交
    r = await svc.submit_exam(confirm=False)
    assert not r["submitted"] and any("confirm" in u for u in r["unmet"])
    r = await svc.submit_exam(confirm=True)
    assert r["submitted"] and page.submitted


async def test_no_question_files_written(env, tmp_path):
    svc, page = env
    await svc.connect()
    await _answer_all(svc)
    await svc.fill_answers("unanswered", max_items=10)
    await svc.get_review_list()
    await svc.verify(reload=True)
    review = await svc.get_review_list()
    items = [{"qid": q["qid"], "answer": q["ai_answer"]} for qs in review["groups"].values() for q in qs]
    if items:
        await svc.apply_user_decisions(items)
        await svc.verify(reload=True)
    r = await svc.submit_exam(confirm=True)
    assert r["submitted"]
    rt, alog = tmp_path / "rt", tmp_path / "a.jsonl"
    leftovers = [p for p in tmp_path.rglob("*")
                 if p.is_file() and p.suffix in (".json", ".md")
                 and rt not in p.parents and p != alog]
    assert leftovers == []
    assert {p.name for p in tmp_path.iterdir()} <= {"rt", "a.jsonl"}


async def test_snapshot_expiry_and_validation(env):
    svc, page = env
    await svc.connect()
    res = await svc.extract_questions()
    q1 = next(q for q in res["questions"] if q["no"] == 1)
    await svc.extract_questions()  # 新快照 → 旧的过期
    r = await svc.save_answers(res["snapshot_id"], [{"qid": q1["qid"], "answer": ["A"], "confidence": 0.9}])
    assert "过期" in r["errors"][0]["error"]
    res2 = await svc.extract_questions()
    q1 = next(q for q in res2["questions"] if q["no"] == 1)
    q2 = next(q for q in res2["questions"] if q["no"] == 2)
    r = await svc.save_answers(res2["snapshot_id"], [
        {"qid": q1["qid"], "answer": ["A", "B"], "confidence": 0.9},
        {"qid": q2["qid"], "answer": "C", "confidence": 0.9},
    ])
    assert "恰好 1 个" in r["errors"][0]["error"] and r["warnings"] and r["saved"] == [2]


async def test_low_confidence_filled_but_reviewed(env):
    svc, page = env
    await svc.connect()
    await _answer_all(svc, conf=0.4)
    rep = await svc.fill_answers()
    assert sorted(rep["filled"]) == [1, 2, 4]
    review = await svc.get_review_list()
    assert len(review["groups"]["low_confidence"]) == 4


async def test_dry_run_and_abort_on_dialog(env):
    svc, page = env
    await svc.connect()
    await _answer_all(svc)
    rep = await svc.fill_answers(dry_run=True)
    assert len(rep["planned"]) == 3 and not any(c[0] == "option" for c in page.clicks)
    page.dialog = "网络异常"
    rep = await svc.fill_answers()
    assert "弹窗" in rep["aborted"] and rep["remaining"] == 3


async def test_consecutive_failures_abort(env):
    svc, page = env
    await svc.connect()
    await _answer_all(svc)
    page.fail_click_on = {1, 2, 4}
    rep = await svc.fill_answers()
    assert len(rep["failed"]) == 2 and "连续" in rep["aborted"]


async def test_multi_only_clicks_difference(env):
    svc, page = env
    await svc.connect()
    page.qs[2].ui = page.qs[2].saved = {"SRAM", "ROM"}  # 先清掉再测：设为工具填过的错误状态
    await _answer_all(svc)
    exam = svc.exam
    q2 = exam.by_no(2)
    q2.origin = "tool"
    page.clicks.clear()
    await svc.fill_answers("nos", [2])
    opt_clicks = [c for c in page.clicks if c[0] == "option"]
    assert len(opt_clicks) == 2  # 取消 ROM + 勾选 DRAM
    assert page.qs[2].saved == {"SRAM", "DRAM"}
