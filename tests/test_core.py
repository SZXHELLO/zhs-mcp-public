from zhs_exam_mcp.store import ExamStore, compute_qid
from zhs_exam_mcp.extractor import parse_header, raw_to_question
from zhs_exam_mcp.formula import html_to_text, strip_option_label
from zhs_exam_mcp.models import AIAnswer, ExamFile, RawOption, RawQuestion, UserDecision
from zhs_exam_mcp.safe_click import SubmitGuard, ForbiddenClick, check_click, is_forbidden_text

import pytest


# ---------- formula ----------
def test_mathjax_script():
    t, lossy = html_to_text('容量 <span class="MathJax">2 10</span><script type="math/tex">2^{10}</script> 字节')
    assert t == "容量 $2^{10}$ 字节" and not lossy


def test_mathjax_display_and_annotation():
    t, _ = html_to_text('<script type="math/tex; mode=display">a+b</script>')
    assert t == "$$a+b$$"
    k = '<span class="katex"><math><semantics><mi>x</mi><annotation encoding="application/x-tex">x^2</annotation></semantics></math><span>x2</span></span>'
    assert html_to_text(k)[0] == "$x^2$"


def test_mathml_and_data_latex():
    m = "<math><mfrac><mn>1</mn><msup><mi>x</mi><mn>2</mn></msup></mfrac></math>"
    assert html_to_text(m)[0] == r"$\frac{1}{{x}^{2}}$"
    assert html_to_text('<span data-latex="\\sqrt{2}">√2</span>')[0] == r"$\sqrt{2}$"


def test_img_without_source_is_lossy():
    t, lossy = html_to_text('值为 <img src="x.png">')
    assert lossy and "[公式#" in t


def test_sup_sub_and_label():
    assert html_to_text("2<sup>10</sup>")[0] == "2$^{10}$"
    assert strip_option_label("C. 异步刷新") == "异步刷新"
    assert strip_option_label("Cache") == "Cache"


def test_parse_header():
    assert parse_header("1.【单选题】（2分）") == (1, "单选题", 2.0)
    assert parse_header("12. 【多选题】 (2.5分)") == (12, "多选题", 2.5)


# ---------- qid / merge ----------
def test_qid_order_independent():
    a = compute_qid("题干", ["A1", "B1", "C1"])
    assert a == compute_qid(" 题干 ", ["C1", "A1", "B1"])
    assert a != compute_qid("题干", ["A1", "B1", "D1"])


def _raw(selected=None, order=(0, 1, 2, 3)):
    opts = ["分散刷新", "集中刷新", "异步刷新", "静态刷新"]
    return RawQuestion(no=1, type_text="单选题", score=2, stem_html="动态RAM刷新方式不包括（ ）",
                       options=[RawOption(html=f"{'ABCD'[i]}. {opts[j]}", selected=opts[j] == selected)
                                for i, j in enumerate(order)])


def test_reshuffle_same_qid_and_letters_change():
    q1 = raw_to_question(_raw(), "c/t", "s1")
    q2 = raw_to_question(_raw(order=(3, 2, 1, 0)), "c/t", "s2")
    assert q1.qid == q2.qid
    assert q1.snapshot.label_map["A"] == "分散刷新" and q2.snapshot.label_map["A"] == "静态刷新"


def test_merge_keeps_ai_and_origin_rules():
    exam = ExamFile(exam_key="c/t", course="c", title="t")
    q = ExamStore.merge(exam, raw_to_question(_raw(), "c/t", "s1"))
    assert q.origin == "empty"
    q.ai = AIAnswer(answer=["静态刷新"], confidence=0.9)
    q.user_decision = UserDecision(answer=["静态刷新"])
    q.correct_answer = ["静态刷新"]
    # 工具填写后
    q.origin, q.page_answer = "tool", ["静态刷新"]
    q2 = ExamStore.merge(exam, raw_to_question(_raw("静态刷新", (2, 0, 3, 1)), "c/t", "s2"))
    assert q2 is q and q.origin == "tool" and q.ai and q.user_decision and q.correct_answer
    assert q.snapshot.snapshot_id == "s2" and len(exam.questions) == 1
    # 用户随后手动改了
    ExamStore.merge(exam, raw_to_question(_raw("异步刷新"), "c/t", "s3"))
    assert q.origin == "user"


def test_first_seen_with_answer_is_user():
    exam = ExamFile(exam_key="c/t", course="c", title="t")
    assert ExamStore.merge(exam, raw_to_question(_raw("集中刷新"), "c/t", "s")).origin == "user"


def test_store_keeps_same_object():
    store = ExamStore()
    assert store.get("计算机组成与系统结构", "第三章单元测试") is None
    exam = store.load_exam("计算机组成与系统结构", "第三章单元测试")
    assert store.load_exam("计算机组成与系统结构", "第三章单元测试") is exam
    assert store.get("计算机组成与系统结构", "第三章单元测试") is exam
    assert store.load_exam_by_key(exam.exam_key) is exam
    assert store.get("计算机组成与系统结构", "第四章单元测试") is None
    other = store.load_exam_by_key("计算机组成与系统结构/第四章单元测试")
    assert other.questions == [] and store.get("计算机组成与系统结构", "第四章单元测试") is None
    q2 = raw_to_question(_raw(), exam.exam_key, "s").model_copy(update={"no": 2, "qid": "q2"})
    q1 = raw_to_question(_raw(), exam.exam_key, "s")
    exam.questions += [q2, q1]
    store.save_exam(exam)
    assert [q.no for q in exam.questions] == [1, 2] and exam.updated_at
    assert store.get("计算机组成与系统结构", "第三章单元测试") is exam


# ---------- safe_click ----------
def test_safe_click_blocks_submit():
    for t in ("提交作业", "交 卷", "Submit", "确认提交"):
        assert is_forbidden_text(t)
        with pytest.raises(ForbiddenClick):
            check_click(t)
    for t in ("暂存作业", "下一题", "上一题", "3"):
        check_click(t)


def test_submit_guard_token():
    g = SubmitGuard(ttl_s=30)
    with pytest.raises(ForbiddenClick):
        check_click("提交作业", g, "wrong")
    tok = g.issue()
    check_click("提交作业", g, tok)
    g.revoke()
    with pytest.raises(ForbiddenClick):
        check_click("提交作业", g, tok)
    g2 = SubmitGuard(ttl_s=-1)
    with pytest.raises(ForbiddenClick):
        check_click("提交作业", g2, g2.issue())  # 已过期


def test_formula_images_get_distinct_stable_tags():
    a, la = html_to_text('<img src="//img.x/a.png?t=1">＝1')
    b, _ = html_to_text('<img src="//img.x/b.png">＝1')
    a2, _ = html_to_text('<img src="//img.x/a.png?t=2">＝1')
    assert la and a != b and a == a2 and a.startswith("[公式#")
