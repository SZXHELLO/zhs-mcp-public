"""基于阶段0侦察产物（脱敏、裁剪）的测试。"""
import json
import re
from pathlib import Path

from bs4 import BeautifulSoup

from zhs_exam_mcp.extractor import raw_texts, raw_to_question, parse_header
from zhs_exam_mcp.extractor_api import DomState, merge_api_dom, parse_do_homework
from zhs_exam_mcp.models import RawOption, RawQuestion

FX = Path(__file__).parent / "fixtures"


def _paper():
    return parse_do_homework(json.loads((FX / "doHomework_sample.json").read_text(encoding="utf-8")))


def test_parse_do_homework():
    info, qs = _paper()
    assert info.course == "计算机组成与系统结构" and info.title == "第四章单元测试" and info.chapter == "第四章"
    assert [q.type_text for q in qs] == ["单选题", "单选题", "单选题", "多选题"]
    assert qs[0].options[0].id == "514427385" and qs[0].score == 2.0 and qs[0].eid


def test_stem_with_br_and_roman():
    _, qs = _paper()
    raw = RawQuestion(no=2, type_text="单选题", stem_html=qs[1].stem_html,
                      options=[RawOption(html=o.content) for o in qs[1].options])
    stem, opts, lossy = raw_texts(raw)
    assert "Ⅰ 保护现场" in stem and "\n" in stem and not lossy
    assert opts[0] == "Ⅰ、Ⅴ、Ⅳ、Ⅶ、Ⅲ、Ⅱ"


def test_dom_and_api_give_same_qid():
    """DOM 选项文本（<p>…<br></p>）与接口文本（…<br>）规范化后一致 → 同一 qid。"""
    _, qs = _paper()
    soup = BeautifulSoup((FX / "dom_q01.html").read_text(encoding="utf-8"), "html.parser")
    items = soup.select(".subject_node .nodeLab")
    dom_opts = [RawOption(html=str(it.select_one(".node_detail")), option_id=it.select_one("input")["value"],
                          selected=bool(it.select_one(".onChecked"))) for it in items]
    assert [o.option_id for o in dom_opts] == [o.id for o in qs[0].options]
    header = soup.select_one(".subject_type_describe").get_text()
    assert parse_header(header)[:2] == (1, "单选题")
    assert soup.select_one(".subject_describe").get_text().strip() == ""  # DOM 题干为空
    dom_raw = RawQuestion(no=1, type_text="单选题", stem_html=qs[0].stem_html, options=dom_opts)
    api_raw = merge_api_dom(qs, [])[0][0]
    assert raw_to_question(dom_raw, "k", "s").qid == raw_to_question(api_raw, "k", "s").qid


def test_merge_selected_by_option_ids_even_if_dom_order_differs():
    _, qs = _paper()
    st = [DomState(index=i, option_ids=[o.id for o in q.options], selected_ids=[]) for i, q in enumerate(qs)]
    st.reverse()
    st[0].selected_ids = [qs[3].options[1].id, qs[3].options[2].id]  # 多选题选了两个
    raws, warns = merge_api_dom(qs, st)
    assert [o.selected for o in raws[3].options] == [False, True, True, False]
    assert not warns


def test_server_answers_override_dom():
    from zhs_exam_mcp.extractor_api import parse_server_answers
    _, qs = _paper()
    body = {"rt": {qs[0].eid: {"questionId": qs[0].eid, "answer": qs[0].options[1].id},
                   qs[3].eid: {"questionId": qs[3].eid, "answer": f"{qs[3].options[0].id},{qs[3].options[2].id}"},
                   qs[1].eid: {"questionId": qs[1].eid, "answer": ""}}}
    server = parse_server_answers(body)
    st = [DomState(index=i, option_ids=[o.id for o in q.options], selected_ids=[]) for i, q in enumerate(qs)]
    st[1].selected_ids = [qs[1].options[0].id]  # 页面上有、服务器没有（仅本地）
    raws, warns = merge_api_dom(qs, st, server)
    assert [o.selected for o in raws[0].options] == [False, True, False, False]
    assert [o.selected for o in raws[3].options] == [True, False, True, False]
    assert not any(o.selected for o in raws[1].options)
    assert len(warns) == 3  # 第1、2、4题页面与服务器不一致
