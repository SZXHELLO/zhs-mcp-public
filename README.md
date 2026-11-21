# zhs-exam-mcp

## 这是什么？
智慧树「在线学堂」章节测试的读题与作答辅助 MCP 服务器（stdio，服务器名 `zhs`），目前仅支持选择题，并且无法通过视觉方式读取图片信息，欢迎各位大神帮忙完善!

工作流程：

读题 → 由模型给出答案、解析和置信度 → 在网页上补填并暂存 → 汇总待确认的题交给你裁决 → 满足提交条件且你明确同意后才提交。也支持「完成 4、6、8 章节测试」这样的批量任务。

> **免责声明**
>
> - 本项目与智慧树（Zhihuishu）官方无任何关系，仅供个人学习使用。
> - 作者极度尊重知识和学习过程，反对任何想通过“捷径”取得成绩的想法和行为，本MCP开发目的仅仅是为了帮助大学生提高学习效率，请不要做任何懈怠学习的行为！
> - 平台页面带有「异常脚本」检测和异常行为处理机制：可能清空作答记录退回重做，甚至记 0 分。是否使用、如何使用，后果由使用者自行承担。
> - 本工具**不做任何规避**：不绕过检测、不处理验证码 / 人脸识别、不自动登录。遇到任何平台弹窗都会立即停止，交给你处理。
> - 模型给出的答案可能出错，请以教材和老师要求为准。

## 环境要求

- Windows 10/11（macOS / Linux 需手动启动浏览器，见下文）
- Microsoft Edge 或 Google Chrome
- [uv](https://docs.astral.sh/uv/getting-started/installation/)：会自动准备 Python 3.11+，无需单独安装 Python
- Git
- 一个支持 stdio 的 MCP 客户端（Claude Code、Claude Desktop 等）

## 安装

```bash
git clone https://github.com/<你的用户名>/zhs-exam-mcp.git
cd zhs-exam-mcp
uv sync
```

不需要 `playwright install`：工具只连接你自己打开的浏览器，不会另外下载浏览器。

## 配置（可选）

没有 `config.yaml` 时，程序直接使用仓库自带的 `config.example.yaml`。需要自定义时把它复制为 `config.yaml` 再改；`config.yaml` 不会被提交到 git。也可以用环境变量 `ZHS_CONFIG` 指定任意位置的配置文件。

可配置项：调试端口地址、低置信度阈值、填写延时、页面选择器等，说明见文件内注释。

## 使用前：启动带调试端口的浏览器

工具通过浏览器调试端口（默认 9222）连接一个**独立用户目录**的浏览器窗口，不影响你日常使用的浏览器。

- **Windows**：双击 `scripts\start_edge.bat`。找不到 Edge 时会改用 Chrome。
- **macOS**：
  ```bash
  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge" --remote-debugging-port=9222 --user-data-dir="$HOME/zhs_edge_profile"
  ```
  用 Chrome 时，把路径换成 `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`。
- **Linux**：
  ```bash
  microsoft-edge --remote-debugging-port=9222 --user-data-dir="$HOME/zhs_edge_profile"
  ```
  也可以用 `google-chrome` 代替 `microsoft-edge`，参数相同。

然后在这个窗口里**手动登录**智慧树：

- 做单个测试：打开该章节测试的做题页（网址含 `/dohomework/`）；
- 批量做多个章节：打开课程的「作业考试」列表页（网址含 `#/webExamList`），由 `open_exam` 逐个打开。

整个过程中请保持这个浏览器窗口开着。

## 接入 MCP 客户端

启动命令（stdio，无需密钥）：

```bash
uv run --directory <项目绝对路径> zhs-exam-mcp
```

`<项目绝对路径>` 是你克隆的 `zhs-exam-mcp` 文件夹的完整路径。在该文件夹里运行 `pwd`（Windows PowerShell 用 `(Get-Location).Path`）即可查看。

**Claude Code：**

```bash
claude mcp add zhs -- uv run --directory <项目绝对路径> zhs-exam-mcp
```

**通用 JSON 配置**（Claude Desktop 等）：

```json
{
  "mcpServers": {
    "zhs": {
      "command": "uv",
      "args": ["run", "--directory", "<项目绝对路径>", "zhs-exam-mcp"]
    }
  }
}
```

Windows 路径写进 JSON 时，反斜杠要写成 `\\`，或者直接改用 `/`，例如 `"D:/code/zhs-exam-mcp"`。

**工作流提示词**：[prompts/agent_workflow.md](prompts/agent_workflow.md)，粘进客户端的系统提示或任务开头即可，包括单个测试和批量模式两种流程。

单次工具调用控制在 60 秒内：`fill_answers` 用 `max_items` 分批，`verify` 可用 `nos` 分批。客户端允许的话，可以把工具超时调大。

## 工具

| 工具 | 作用 |
|---|---|
| `connect` | 连接浏览器并定位做题页，返回测试信息 |
| `get_overview` | 每题是否已答、答案来源（自己答 / 工具填 / 未答）、是否有 AI 答案，以及完成率 |
| `extract_questions(nos?, refresh?)` | 读题（只保存在服务器进程内存中），返回 `snapshot_id` 和题目 |
| `save_answers(snapshot_id, items)` | 保存 AI 答案（选项字母按本次快照换算成选项文本） |
| `fill_answers(scope, nos?, dry_run, max_items)` | 在网页上补填，分批进行；不改动你自己作答的题 |
| `verify(reload, nos?)` | 刷新后对照服务器作答记录核对 |
| `save_progress` | 点「暂存作业」（成功后平台会自动关闭做题页） |
| `get_review_list(exam_key?, threshold?)` | 汇总待确认题 |
| `apply_user_decisions(items)` | 写入你的裁决并同步到网页 |
| `submit_exam(confirm)` | 满足提交条件且 `confirm=true` 才点「提交作业」 |
| `list_exams` | 「作业考试」列表页中未提交的章节测试 |
| `start_batch(chapters)` | 开始批量任务，如 `["4","6","8"]` |
| `open_exam(chapter)` | 从列表页打开某章节测试，遇到平台弹窗立即停止 |
| `batch_status` | 批量进度和汇总的待确认题 |
| `end_batch` | 结束批量任务 |

**提交条件**：全部题都有最终答案；`verify(reload=true)` 全部一致，且之后没有再改动；待确认清单为空；批量任务（如有）的所有章节都已暂存；`confirm=true`，只有你在对话中明确同意后才会传入。其余任何情况下，都不会点击含「提交 / 交卷 / submit」字样的按钮。

**批量模式**：所有指定章节都完成并暂存之前，不会提交，也不会中途询问；全部完成后，一次性汇报待确认的题，并问你要提交哪些章节。

## 运行产物

- `runtime/logs/server.log`、`runtime/logs/actions.jsonl`：每次点击和保存都有记录
- `runtime/screenshots/`：出错截图
- `runtime/batch_state.json`：批量任务进度

`runtime/` 不会提交到 git。

## 页面改版后：重新侦察

```bash
uv run python scripts/recon.py
uv run python scripts/recon_list.py
```

第一条侦察做题页，需先打开一个做题页；第二条侦察「作业考试」列表页。

两个脚本都只读，不点选项、不暂存、不提交。产物写入 `runtime/recon/`，据此修改 `config.yaml` 中的选择器。当前的侦察结论见 [docs/FINDINGS.md](docs/FINDINGS.md)。

## 开发与测试

```bash
uv run pytest
uv run python scripts/mcp_smoke.py --list
uv run python scripts/mcp_smoke.py
npx @modelcontextprotocol/inspector uv run --directory <项目绝对路径> zhs-exam-mcp
```

依次为：离线单元测试；只列出工具；连接已打开的做题页做冒烟测试；用 MCP Inspector 手动调用各工具。

## 许可证

[MIT](LICENSE)
