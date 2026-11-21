# zhs-exam-mcp 瘦身计划：移除本地题库与 Markdown 导出

> 交给 Claude Code 执行。项目根目录：`E:\githubrepository\mcp\zhs-mcp`，包目录：`src/zhs_exam_mcp/`。
> 请先新建分支（如 `slim/no-local-bank`）再动手，每完成一个阶段跑一次 `uv run pytest`。

## 0. 目标与范围

**要删的**：所有把题目写到磁盘的逻辑。
- `Qbank/<课程>/<测试>.json`、`Qbank/index.json`（题库持久化 + 跨测试索引）
- `<测试>.md`（复习文件）、`<测试>_待确认.md`（待确认清单文件）
- 跨测试复用答案（`find_elsewhere`）
- 题库目录配置：`bank_dir`、环境变量 `ZHS_BANK_DIR`、`ensure_bank_dir()`
- MCP 工具 `export_markdown`

**必须保留的**（做题流程依赖它们，不能删）：
- 题目规范化：`extractor.py`（`raw_to_question` / `raw_texts`）、`formula.py`（HTML→文本/LaTeX）、`extractor_api.py`、`models.py`。
  原因：模型作答要读规范化后的题干；`filler._fill_one` 填写前用 `compute_qid(stem, opts)` 校验题目没变；
  快照 `label_map` 负责把字母换算成选项文本（选项顺序每次随机）。
- `Bank.merge()` 的合并逻辑（`origin` 推断：user / tool / empty），`select_targets`、`build_review`、提交条件都依赖它。
- `review.py` 的 `build_review` / `review_item` / `to_letters`：待确认清单和提交条件要用，只是不再写成文件。
- `runtime/` 下的日志、截图、`batch_state.json`：这些不是题目，保持不变。

**做法**：把 `Bank`（磁盘题库）换成进程内的 `ExamStore`（内存字典，按 `exam_key` 存 `ExamFile`），调用点保持原样。

**不要动**：`browser.py`、`adapter.py`、`exam_list.py`、`safe_click.py`、`filler.py` 的点选/保存逻辑、`submit.py` 的条件判断逻辑、`docs/FINDINGS.md`、`runtime/`。
**不要删除**用户磁盘上已有的 `Qbank` 文件夹（由用户自己决定）。**不要**对真实页面调用 `submit_exam`。

---

## 1. 新建 `src/zhs_exam_mcp/store.py`，删除 `bank.py`

`store.py` 内容：

1. 从 `bank.py` 原样搬过来：`norm`、`compute_qid`、`same_answer`、`make_exam_key`。
2. 新类 `ExamStore`（纯内存，无任何文件 I/O）：
   ```python
   class ExamStore:
       def __init__(self) -> None:
           self._exams: dict[str, ExamFile] = {}

       def load_exam(self, course: str, title: str) -> ExamFile:
           """取已有的 ExamFile；没有就新建并登记（connect 用）。返回的是同一个对象，修改即生效。"""
           key = make_exam_key(course, title)
           if key not in self._exams:
               self._exams[key] = ExamFile(exam_key=key, course=course, title=title)
           return self._exams[key]

       def get(self, course: str, title: str) -> ExamFile | None:
           """只查不建（batch_status 用，避免给未打开的章节登记空记录）。"""
           return self._exams.get(make_exam_key(course, title))

       def load_exam_by_key(self, exam_key: str) -> ExamFile:
           """有就返回；没有返回一个空 ExamFile（不登记），保持原 Bank 的语义。"""
           course, _, title = exam_key.partition("/")
           return self._exams.get(exam_key) or ExamFile(exam_key=exam_key, course=course, title=title)

       def save_exam(self, exam: ExamFile) -> None:
           """保留这个方法让 service 的调用点不用改：排序 + 更新时间 + 登记，不落盘。"""
           exam.questions.sort(key=lambda q: q.no)
           exam.updated_at = now()
           self._exams[exam.exam_key] = exam

       @staticmethod
       def merge(exam: ExamFile, fresh: Question) -> Question:
           ...  # 原样复制 Bank.merge 的实现，一行不改
   ```
3. **不要**搬：`_INVALID_FN`、`safe_filename`、`_atomic_write`、`exam_dir`、`exam_path`、`index_path`、`load_index`、`_update_index`、`find_elsewhere`。
4. 删除 `bank.py`。
5. 改 import（全部改为 `from .store import ...`）：
   - `extractor.py`：`compute_qid`
   - `filler.py`：顶部 `compute_qid, norm`；`select_targets` 函数内的局部 `from .bank import same_answer` 一并改掉（可以直接挪到文件顶部）
   - `review.py`：`same_answer`
   - `submit.py`：`same_answer`
   - `service.py`：`ExamStore, make_exam_key, norm, same_answer`（`make_exam_key` 若改后未使用就去掉）
   - `formula.py` 第 158 行注释里的 `bank.norm` → `store.norm`

## 2. 删除 `export.py`

整个文件删除。确认 `render_review_md` / `render_pending_md` 只被 `service.py` 引用。

## 3. 修改 `service.py`

- import：去掉 `from .export import render_pending_md, render_review_md`；`Bank` 换成 `ExamStore`。
- `__init__`：`self.bank = Bank(cfg.bank_dir)` → `self.store = ExamStore()`；文件里所有 `self.bank.` 改为 `self.store.`，`Bank.merge` 改为 `ExamStore.merge`。
- `connect()`：返回值删掉 `"bank_dir": ...`；`"questions_in_bank"` 改名为 `"questions_cached"`。
- `extract_questions()`：删除"跨测试复用"这一段（原第 144–147 行）：
  ```python
  if q.ai is None:  # 跨测试复用
      other = self.bank.find_elsewhere(q.qid, exam.exam_key)
      if other:
          q.ai, q.correct_answer = other.ai, q.correct_answer or other.correct_answer
  ```
  返回值中 `"has_ai"` 等字段保持不变。
- `get_review_list()`：删除写文件的三行（`path = ...`、`path.parent.mkdir(...)`、`path.write_text(...)`），返回值去掉 `"file"`；`note` 改为
  `"字母按首次读题时的选项顺序 A–H，apply_user_decisions 使用同一套字母"`。
- `export_markdown()`：整个方法删除。
- `batch_status()`：`exam = self.bank.load_exam(course, it.name) if course else None` → `exam = self.store.get(course, it.name) if course else None`。
- 面向用户的错误文案里的"题库"可改为"已读题目"（如"题库中无此 qid" → "已读题目中无此 qid"），可选；改了就同步改测试里的断言。

## 4. 修改 `server.py`

- 去掉 `ensure_bank_dir` 的 import、`_bank_error` 变量及其日志分支（原第 24–28 行），以及 `_run()` 里 `if _bank_error:` 的提前返回。
- 删除 `export_markdown` 工具函数（`@mcp.tool()` + 函数体）。
- 更新工具 docstring：`extract_questions` 的"读题并写入题库" → "读题（结果只保存在本次服务器进程的内存中）"；`connect`、`get_review_list` 的 docstring 里如果提到题库目录或 `_待确认.md` 文件，一并删掉。
- `uv run python scripts/mcp_smoke.py --list` 应只剩 15 个工具（原 16 个减去 `export_markdown`）。

## 5. 修改 `config.py` 与配置文件

- `config.py`：删除 `bank_dir` 字段、`load_config()` 中 `ZHS_BANK_DIR` 的处理和 `cfg.bank_dir = _resolve(...)`、整个 `ensure_bank_dir()`、模块 docstring 中关于题库目录优先级的那一行。`runtime_dir` 的相对路径解析逻辑保持不变。
- `config.example.yaml`：删除"题库目录"注释和 `bank_dir: ../Qbank` 这一行。
- 用户本地的 `config.yaml`（不在 git 里）：同样删掉 `bank_dir` 这一行。（Config 没有设置 `extra="forbid"`，残留也不会报错，但删掉更干净；执行前先确认这一点。）

## 6. 测试

- `tests/test_core.py`：import 改为 `from zhs_exam_mcp.store import ExamStore, compute_qid`；`Bank.merge` → `ExamStore.merge`；删除 `test_bank_roundtrip_and_index`。
  新增 `test_store_keeps_same_object`：`load_exam` 两次拿到同一对象；`get` 对未登记的测试返回 `None`；`save_exam` 后按题号排序。
- `tests/test_config.py`：删除 `test_default_bank_is_sibling_of_project`、`test_env_overrides_bank_dir`、`test_ensure_bank_dir_creates_and_reports`；`test_relative_paths_resolve_against_project_not_config` 改用 `runtime_dir` 验证；第 47 行的断言去掉 `bank_dir` 部分；模块 docstring 同步修改。
- `tests/test_service.py`、`tests/test_batch.py`：`Config(bank_dir=..., ...)` 去掉 `bank_dir` 参数；删除 `assert (await svc.export_markdown())["questions"] == 4`。
- 新增 `test_no_question_files_written`（放在 `test_service.py`）：跑完整流程（connect → extract → save_answers → fill → get_review_list → verify → submit）后，断言 `tmp_path` 下除了 runtime 目录和 action log 之外，没有任何 `.json` / `.md` 文件。
- `test_batch.py` 的 `test_batch_flow_no_submit_until_all_saved` 里"重新打开第四章 → verify → 提交"这一段必须继续通过：它验证的正是内存 store 能跨 `connect` 保留 AI 答案。

## 7. 文档

- `README.md`：
  - 开头简介去掉"本地题库""生成复习用 Markdown"等表述；
  - 删除整个"### 题库位置"小节；
  - "配置"小节可配置项里去掉"题库目录"；
  - 工具表删除 `export_markdown` 行，`connect` 的说明改为"返回测试信息"，`get_review_list` 的说明去掉"并写出 `*_待确认.md`"；
  - 新增一句说明：题目和 AI 答案只保存在 MCP 服务器进程内存中，服务器重启后需要重新 `extract_questions` 并作答。
- `prompts/agent_workflow.md`：
  - 单个测试流程：删除第 3 步 `export_markdown`（后续步骤重新编号），第 6 步末尾的 `→ export_markdown` 删除；
  - 批量模式：第 2.5 步改为只调用 `get_review_list`（只生成清单，不询问）；第 3 步汇报内容去掉"复习文件路径"。
- `pyproject.toml`：`description` 改为 `"智慧树章节测试：读题与作答辅助 MCP（stdio）"` 之类，去掉"题目整理"。
- `.gitignore` 的 `Qbank/` 行保留（无害，防止旧文件被误提交）。

## 8. 验收标准

1. `uv run pytest` 全部通过。
2. 以下命令无输出（`.gitignore` 除外）：
   ```bash
   grep -rnE "bank_dir|ZHS_BANK_DIR|Qbank|export_markdown|ensure_bank_dir|from \.bank|render_review_md|render_pending_md|find_elsewhere|_待确认\.md" src tests scripts prompts README.md config.example.yaml pyproject.toml
   ```
3. `uv run python scripts/mcp_smoke.py --list` 列出 15 个工具，不含 `export_markdown`。
4. 代码里除 `runtime/` 下的日志、截图、`batch_state.json` 外，不再有任何写文件的操作：
   `grep -rnE "write_text|open\(.*['\"]w|os\.replace|mkdir" src/zhs_exam_mcp` 只剩 runtime / 日志 / 截图 / batch_state 相关的命中。
5. 手动冒烟（需要用户打开做题页，由用户执行或在用户同意下执行，**不提交**）：`connect` → `extract_questions` → `save_answers` → `fill_answers(dry_run=true)` → `verify`，全程不报错，`Qbank` 目录无新增或修改的文件。

## 9. 行为变化（执行完向用户说明）

- **重启即丢失**：MCP 服务器进程重启（包括客户端重连）后，已读题目、AI 答案、用户裁决全部清空，需要重新 `extract_questions` 并作答。批量任务中途重启时，`batch_state.json` 还在但题目数据没了，此时 `submit_exam` 会因"题数不一致 / 没有最终答案"拒绝提交（安全的失败方式），需要对相应章节重新读题作答。
- **不再跨测试复用**：同一道题在别的测试里出现，不会自动带出之前的 AI 答案。
- **不再生成任何 Markdown**：待确认清单只通过 `get_review_list` / `batch_status` 的返回值出现在对话里。
