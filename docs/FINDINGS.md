# 页面侦察结论

智慧树「在线学堂」章节测试页与「作业考试」列表页的结构、接口和保存机制。
侦察于 2026-11-19 进行（一门课程的第四、五章单元测试）；平台改版后请用 `scripts/recon.py`、`scripts/recon_list.py` 重新侦察，并据此修改 `config.yaml`。

> 侦察脚本只读：不点选项、不点「下一题」、不暂存、不提交；接口数据保存前会脱敏。侦察产物在 `runtime/recon/`，不提交到仓库。

## 1. 做题页（`stuExamWeb.html#/webExamList/dohomework/...`）

### 读题：接口 + DOM 选中状态（`extract_mode: api`）

- **题干在 DOM 中为空**：`.subject_describe.dynamic-fonts` 由防复制组件渲染，`innerText` 为空，无法从 DOM 读取。
- **`doHomework` 响应是明文 JSON**（请求体是加密参数，工具不构造请求，只被动读取页面加载时收到的响应）：
  - `rt.examBase{name, courseName, toChapter, problemNum, totalScore, workExamParts[{questionDtos[...]}]}`
  - 每题 `{eid, name(题干 HTML), questionType{id:1 单选 / 2 多选, name}, questionScore, questionOptions[{id, content}]}`
  - `result` 为 null：**不下发标准答案**。
- 选项 `id` 跨加载稳定，等于 DOM 中隐藏 `input[type=radio|checkbox]` 的 `value`。
- **接口选项顺序 = 页面显示顺序**，每次加载重新随机；按「题干 + 排序后的选项文本」计算的 qid 不受影响。
- 全部题目的题目区**同时存在**于 DOM（`.examPaper_subject`，仅当前题可见），读选中状态不需要逐题切换。

### DOM 选择器

| 用途 | 选择器 / 说明 |
|---|---|
| 题目区 | `.examPaper_subject` |
| 题号题型 | `.subject_type_describe`，如 `1.\n【单选题】 (2分)`；多选题可能无点号：`23【多选题】 (2分)` |
| 选项 | `.subject_node .nodeLab`；字母 `span.mr10`；内容 `.node_detail` |
| 选中态 | 字母 span 带 `onChecked` |
| 答题卡 | `.answerCard_list1 li`：`white` = 未答，`yellow` / `green` = 已答 |
| 按钮 | `上一题` / `下一题`；**最后一题没有「下一题」，是「保存」**；`暂存作业`、`提交作业` |

点答题卡切题只切换显示，不产生网络请求。

### 保存机制（来自页面 JS）

- 「下一题」/ 最后一题「保存」：调用 `saveStudentAnswer` 保存当前题 → 答案已存到服务器。
- 「暂存作业」：确认框「你确定要暂存作业吗」→ 确定 → `temporarySave` → 成功后 **`window.close()` 自动关闭做题页**。
- 「提交作业」：确认框 → `/answer/submit`。
- 页面还会把答案写进 `localStorage`，刷新后与服务器记录合并显示。因此 `verify(reload)` 以 **`getStuAnswerInfoNew`**（服务器作答记录）为准：
  `rt = {eid: {questionId, answer: "选项id" | "id1,id2"（多选逗号分隔）| ""（未答）}}`，仅在已有作答时请求。

### 公式

- 已见过两种：普通文本（含罗马数字、`<br>` 换行）和**无 LaTeX 源的公式图片**。
- 公式图片转为 `[公式#<图片地址哈希>]`，标记 `formula_lossy`，自动进入待确认清单；其余公式形式（MathJax / KaTeX / MathML / `data-latex`）`formula.py` 已支持，但尚未在本平台见到。

## 2. 「作业考试」列表页（`stuExamWeb.html#/webExamList?...`）

- 加载时请求 `/student/getStudentHomework`（明文）：`rt.studentHomeworkList[{examName, cpOrderNumber, chapterRank, problemNum, achieveCount, state, endDate, ...}]`。
  **其中含学生真实姓名等个人信息**：工具只在内存中读取所需字段，不写入任何文件。
- 期末「在线考试」走另一个接口，本工具不处理。
- 卡片 `.examItemWrap`，标题 `.examTit .name`，按钮 `.examChartBox1 span.themeBg`（「开始答题」/「继续答题」）。
- 点击后平台依次检查：作答锁、**异常行为**、定位、人脸识别；全部通过才 `window.open` 新的做题页。做题页关闭后列表页自动刷新。

## 3. 平台检测（重要）

页面 JS 包含：

- `checkoutNotTrustScript`：加载后及每 60 秒检测「异常脚本」，命中则弹框并禁止作答；
- 异常行为处理：可能**清空作答记录退回重做，再次发现直接 0 分**；
- 防复制、屏蔽 `__vue__`、验证码 SDK（网易易盾 / 腾讯）。

本工具**不做任何规避**：任何弹框、验证码或上述提示出现时立即停止，交给用户处理。

## 4. 实测记录（2026-11-19）

| 项目 | 结果 |
|---|---|
| 读题（26 题 / 20 题） | ✔ 题型正确；刷新后选项重排，qid 全部不变 |
| 单选、多选、最后一题「保存」填写 | ✔ 与服务器作答记录一致 |
| 批量填写 26 题（3 批，每批约 25 秒） | ✔ 无失败、无弹窗 |
| 暂存（确认框 → 页面自动关闭） | ✔ |
| 从列表页打开测试（`open_exam`） | ✔ 约 9 秒，首次加载即捕获题目数据 |
| 「提交作业」确认框 | 未实测（需真实提交） |
| 批量模式端到端（多个章节连续） | 未实测 |
