"""HTML → 纯文本，公式统一转成 $...$ / $$...$$ LaTeX。

优先级：原始 LaTeX 源（MathJax script / annotation / data-latex）→ MathML 转写 →
保留可见文本并标记 lossy。
"""
from __future__ import annotations

import hashlib
import re

from bs4 import BeautifulSoup, NavigableString, Tag

LOSSY_PLACEHOLDER = "[公式]"


def _wrap(tex: str, display: bool) -> str:
    tex = tex.strip()
    return f"$${tex}$$" if display else f"${tex}$"


# ---------------- MathML → LaTeX（覆盖常见元素） ----------------

_MO_MAP = {
    "×": r"\times", "÷": r"\div", "≤": r"\le", "≥": r"\ge", "≠": r"\ne", "±": r"\pm",
    "→": r"\to", "∞": r"\infty", "·": r"\cdot", "⋅": r"\cdot", "∑": r"\sum", "∏": r"\prod",
    "∫": r"\int", "√": r"\sqrt{}", "≈": r"\approx", "∈": r"\in", "∧": r"\wedge", "∨": r"\vee",
    "¬": r"\neg", "⊕": r"\oplus", "−": "-",
}


def mathml_to_latex(node: Tag) -> str:
    def conv(n) -> str:
        if isinstance(n, NavigableString):
            return str(n).strip()
        name = n.name.split(":")[-1] if n.name else ""
        kids = [c for c in n.children if not (isinstance(c, NavigableString) and not str(c).strip())]
        k = [conv(c) for c in kids]
        if name in ("math", "mrow", "mstyle", "semantics", "mpadded", "mphantom"):
            if name == "semantics":
                return k[0] if k else ""
            return "".join(k)
        if name in ("annotation", "annotation-xml"):
            return ""
        if name == "mi" or name == "mn":
            return n.get_text()
        if name == "mo":
            t = n.get_text().strip()
            return _MO_MAP.get(t, t)
        if name == "mtext":
            return r"\text{" + n.get_text() + "}"
        if name == "msup" and len(k) >= 2:
            return f"{{{k[0]}}}^{{{k[1]}}}"
        if name == "msub" and len(k) >= 2:
            return f"{{{k[0]}}}_{{{k[1]}}}"
        if name == "msubsup" and len(k) >= 3:
            return f"{{{k[0]}}}_{{{k[1]}}}^{{{k[2]}}}"
        if name == "mfrac" and len(k) >= 2:
            return rf"\frac{{{k[0]}}}{{{k[1]}}}"
        if name == "msqrt":
            return rf"\sqrt{{{''.join(k)}}}"
        if name == "mroot" and len(k) >= 2:
            return rf"\sqrt[{k[1]}]{{{k[0]}}}"
        if name == "mover" and len(k) >= 2:
            return rf"\overline{{{k[0]}}}" if k[1] in ("¯", "‾", "-", "―") else rf"\overset{{{k[1]}}}{{{k[0]}}}"
        if name == "munder" and len(k) >= 2:
            return rf"\underset{{{k[1]}}}{{{k[0]}}}"
        if name == "mfenced":
            o, c = n.get("open", "("), n.get("close", ")")
            return o + ",".join(k) + c
        if name == "mtable":
            return r"\begin{matrix}" + r"\\".join(k) + r"\end{matrix}"
        if name == "mtr":
            return "&".join(k)
        return "".join(k)

    return conv(node)


# ---------------- 主转换 ----------------

def html_to_text(html: str) -> tuple[str, bool]:
    """返回 (文本, formula_lossy)。"""
    soup = BeautifulSoup(html or "", "html.parser")
    lossy = False

    for s in soup.find_all(["style", "noscript"]):
        s.decompose()

    # MathJax v2：<script type="math/tex[; mode=display]">；去掉渲染副本
    for el in soup.select(".MathJax_Preview, .MathJax, .MathJax_Display, .MathJax_SVG, .MathJax_CHTML"):
        el.decompose()
    for sc in soup.find_all("script"):
        typ = (sc.get("type") or "").lower()
        if typ.startswith("math/tex"):
            sc.replace_with(_wrap(sc.get_text(), "mode=display" in typ))
        elif typ.startswith("math/mml"):
            m = BeautifulSoup(sc.get_text(), "html.parser").find("math")
            sc.replace_with(_wrap(mathml_to_latex(m), False) if m else "")
        else:
            sc.decompose()

    # data-latex
    for el in soup.select("[data-latex]"):
        el.replace_with(_wrap(el["data-latex"], el.name == "div"))

    # KaTeX：.katex 内有 annotation
    for el in soup.select(".katex-display, .katex"):
        if el.parent is None:
            continue
        ann = el.find("annotation", attrs={"encoding": "application/x-tex"})
        if ann:
            el.replace_with(_wrap(ann.get_text(), "katex-display" in (el.get("class") or [])))

    # MathJax v3：mjx-container（可能带 assistive MathML）
    for el in soup.find_all("mjx-container"):
        disp = el.get("display") == "true"
        ann = el.find("annotation", attrs={"encoding": "application/x-tex"})
        m = el.find("math")
        if ann:
            el.replace_with(_wrap(ann.get_text(), disp))
        elif m:
            el.replace_with(_wrap(mathml_to_latex(m), disp))
        else:
            lossy = True
            el.replace_with(LOSSY_PLACEHOLDER)

    # 裸 MathML
    for m in soup.find_all("math"):
        if m.parent is None:
            continue
        ann = m.find("annotation", attrs={"encoding": "application/x-tex"})
        disp = m.get("display") == "block"
        m.replace_with(_wrap(ann.get_text() if ann else mathml_to_latex(m), disp))

    # 上下标（简单富文本公式）
    for t in soup.find_all(["sup", "sub"]):
        inner = t.get_text().strip()
        t.replace_with(f"$^{{{inner}}}$" if t.name == "sup" else f"$_{{{inner}}}$")

    # 图片：有 alt/latex 用之，否则占位
    for img in soup.find_all("img"):
        alt = (img.get("data-latex") or img.get("alt") or "").strip()
        if alt:
            img.replace_with(_wrap(alt, False))
        else:
            # 无 LaTeX 源的公式图片：用图片地址的短哈希区分（同一选项跨加载稳定；不同图片不会变成同一文本）
            lossy = True
            src = (img.get("src") or img.get("data-src") or "").split("?")[0]
            tag = hashlib.sha1(src.encode("utf-8")).hexdigest()[:6] if src else ""
            img.replace_with(f"[公式#{tag}]" if tag else LOSSY_PLACEHOLDER)

    for br in soup.find_all("br"):
        br.replace_with("\n")
    for blk in soup.find_all(["p", "div", "li"]):
        blk.insert_after("\n")

    text = soup.get_text()
    # 不做 NFKC（会把 Ⅰ→I、全角标点→半角，改变显示文本）；NFKC 只用于 qid/匹配（store.norm）
    text = text.replace("\xa0", " ").replace("　", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{2,}", "\n", text).strip()
    if "[公式" in text:
        lossy = True
    return text, lossy


_LABEL_RE = re.compile(r"^\s*[A-Ha-h]\s*[.．、:：]\s*")


def strip_option_label(text: str) -> str:
    """去掉选项开头的「A.」——字母随机，不入库。"""
    return _LABEL_RE.sub("", text, count=1).strip()
