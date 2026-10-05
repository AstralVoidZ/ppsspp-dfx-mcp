# ppsspp-dfx 盲测评估

## 两条采集通道

| | LLM API 通道 | opencode 通道 |
|---|---|---|
| 入口 | `python -m evals.runner` | `python -m evals.oc` |
| 大脑 | 自建 agent loop + 用户 API key | 外部 agent 自带额度（**免 key**） |
| MCP 会话 | Python 侧直连 server | opencode 拉起（`.opencode/mcp.json`） |
| 资源 | 每 run 一个 server 子进程 | 常驻 server，PPSSPP 长驻 |
| variant 消融 | B0 / B1 / B2 均可 | **仅 B1 有意义**（见下） |

两通道**评分面完全共用**（`scenarios.yaml` + `gates.py` + 轨迹 schema），
故 runs 可合并给 `rescore.py` / `report.py` 对账（设计 D3）。

## 结构

| 文件 | 职责 |
|---|---|
| `scenarios.yaml` | 49 张场景卡（CTL×2 / L1×8 / L2×6 / L3×3 / REAL×30，v1.2），ground truth 全部 fixture 驱动 |
| `gates.py` | 确定性门禁（first_tool / params / sequence / answer_contains / recovery / tool_used / final_call_ok / boot_order / no_tool / file_saved / result_field） |
| `test_gates.py` | 门禁单测（每类正反例） |
| `runner.py` | **通道一** Runner MVP：fake 模式 server 子进程（每 run 隔离）+ OpenAI 兼容 agent loop + 控频 + JSONL + 断点续跑 |
| `config.yaml` | 通道一的被测模型（单模型，v1.1 定版）、控频参数、上限 |
| `oc/` | **通道二** opencode 采集线（模块化包，见下） |
| `bridge.py` | 通道一/二的旁路：MCP-over-HTTP bridge，供外部 agent 的子代理用 curl 驱动（第一代方案，保留） |
| `test_bridge.py` | bridge 单测（fake 模式端点 + 加固） |
| `judge_prompt.md` | L3 方案题的 judge 子代理模板（位置交换协议） |
| `report.py` | 报告生成器：分层成功率/混淆对/门禁挂点/恢复明细/效率 |
| `rescore.py` | 历史轨迹按当前场景卡重评分 |
| `test_b2_skill.py` | B2 变体（skill_read 伪工具，渐进披露模拟） |
| `test_oc_collect.py` | 通道二单测（解析/命令构造/超时/轨迹同构/端到端） |
| `runs/` | JSONL 轨迹（gitignore） |

### `oc/` 模块划分

对标 `tools/porpoless/porpoless/parse/` 的模块化理念：

| 模块 | 职责 | porpoless 对标 |
|---|---|---|
| `oc/log.py` | logger 工厂 + 吞咽留痕 | `log.py` |
| `oc/progress.py` | 进度通道（与诊断分离） | `progress.py` |
| `oc/errors.py` | 结构化失败信号 `error_kind` | `parse/runner_base.py` |
| `oc/events.py` | 事件流解析（工具名归一 / usage 聚合） | `parse/opencode_events.py` |
| `oc/attach.py` | `opencode serve` 生命周期 + URL 白名单 | `parse/attach.py` |
| `oc/runner.py` | `opencode run` 驱动（flag 探测 / 超时安全） | `parse/cli_runner.py` |
| `oc/env.py` | server env + XDG 隔离 + 会话预置 | 通道二新增（消除三处重复） |
| `oc/scenario.py` | 场景卡访问层 | `parse/batching.py` |
| `oc/trajectory.py` | 轨迹契约 + JSONL + resume | `parse/corpus_io.py` |
| `oc/collect.py` | 编排 + CLI | `cli.py` |

`opencode_collect.py` 是**已废弃的兼容入口**，仅转发到 `evals.oc.collect.main`。

## 运行

```bash
# 门禁单测（阶段 1 验收）
<venv python> -m pytest evals/test_gates.py -q

# 冒烟 / 试点（阶段 2 验收：CTL-01 端到端）
<venv python> -u -m evals.runner --scenarios CTL-01 --runs 1

# 全量核心网格（fake 模式场景 × n=5；控频串行，中断后重跑自动续）
<venv python> -u -m evals.runner

# instructions 消融（B0：Runner 侧剥离，零 server 侵入）
<venv python> -u -m evals.runner --variant B0

# ── 通道二（opencode，免 LLM API key）──
<venv python> -u -m evals.oc --scenarios CTL-01 --runs 1 -v
<venv python> -u -m evals.oc --scenarios CTL-01 --runs 1 --dump-events -v -v
<venv python> -u -m evals.oc --attach http://127.0.0.1:4911     # 复用常驻 server
<venv python> -u -m evals.oc --scenarios R9-REAL-STEP --runs 1 --real
```

## 依赖与约定

- 解释器：项目 venv 的 python（httpx 等三方 HTTP 库不在依赖内，两条通道的 HTTP
  都用标准库 urllib）。
- 密钥：`evals/llm_api.json`（已 gitignore），形如 `{provider: {options: {baseURL, apiKey}, models}}`；
  路径可用 `config.yaml` 的 `llm_api_path` 或环境变量 `PPSSPP_DFX_EVALS_LLM_API_PATH` 覆盖。
  **通道二不需要此文件**（额度由外部 agent 提供）。
- 变体：B1=工具面+instructions（默认）；B0=剥离 instructions（消融）；B2=skill 伪工具
  （需 `PPSSPP_DFX_SKILL_DIR`）。**通道二只有 B1 有意义**：opencode 通道的 prompt 由
  `.opencode/agents/ppsspp-dfx-collector.md` 静态定义，采集器无法注入或剥离——
  传 `--variant B0/B2` 会打警告，其结果与通道一同名变体**不可比**。
- real 模式（R1-R2 真机场景）由场景卡的 `mode: real` 字段驱动（需
  `PPSSPP_DFX_TEST_EXE_PATH` / `PPSSPP_DFX_TEST_ISO_PATH` 环境变量，未设则对应场景自动 skip）。
  通道二的 real 模式**不做会话预置**（预置出的会话其 PPSSPP 进程已随预置 server 退出），
  改由 agent 自行 `ppsspp_session start`。
- L3 方案题（L3-01/L3-02）关键词门禁只是预筛，正式评分需按 `judge_prompt.md` 派发 subagent（位置交换两次）。
- 真实游戏标识不入库：场景卡里凡涉及真实光盘序号/游戏名的门禁值，一律写成
  `{{ENV_VAR}}` 令牌（如 `{{PPSSPP_DFX_EVAL_GAME_CODE}}`），由评分时从环境变量解析。
  变量未设或为空白时该值**跳过**（不计入 pass/fail），既避免真实标识进入版本库，
  也避免用空串匹配而虚假通过。
  常用令牌：`PPSSPP_DFX_EVAL_GAME_PRODUCT_CODE` / `..._GAME_CODE` /
  `..._GAME_TITLE` / `..._GAME_TITLE_JP`。
- 每 run 一行 JSONL，含四项 provenance 哈希（git commit / tool_surface / instructions / fixtures），
  resume 按 (scenario, model, variant, run_idx) 去重。通道二的 `instructions_sha256`
  取的是 **agent 定义文件**的指纹（同键不同源，见 `oc/collect.py:build_provenance`）。

## 通道二注意事项

以下均为**本机 opencode v2.0.19 实测**结论（porpoless 标定的是 v1.18.x，两版不兼容处已逐条标注）。

- **MCP 注册位置（最容易踩）**：opencode v2 读 `<项目根>/opencode.json` 的
  `mcp.servers`，**不读** `.opencode/mcp.json`（后者是 Claude-Code / MCP-Inspector
  风格）。未注册时 `opencode mcp list` 回 `No MCP servers configured`，agent 拿不到
  任何 ppsspp 工具，只能退化成用 `execute`/`shell`，轨迹里 `tool_calls=0`——
  读起来像「模型不会用工具」，实际是接线缺失。注册一次：

  ```bash
  opencode mcp add ppsspp-dfx -- <repo>/.venv/ppsspp-dfx-mcp/Scripts/python.exe -m ppsspp_dfx_mcp
  ```

  生成的 `<项目根>/opencode.json` 含本机绝对路径，已列入根 `.gitignore`。
  采集器开跑前会做预检（`oc/collect.py:check_mcp_registered`），未注册即 3 秒内
  失败并给出上面这条命令，而不是产出整场误导性 runs。

- **v2 把 MCP 调用包在 `execute` 沙箱里（最关键的一条）**：opencode v2 不会把 MCP
  工具作为独立 part 暴露。模型实际发出的形态是：

  ```json
  {"type":"tool_use","part":{"tool":"execute","state":{
    "input":{"code":"const r = await tools[\"ppsspp-dfx\"].ppsspp_health({});\nreturn r;"},
    "output":{"status":"ok", ...}}}}}
  ```

  工具路径形如 `tools["<serverKey>"].<toolName>`，可用 `search({query})` 查全量。
  **按 part.tool 过滤 `ppsspp-*` 在 v2 上必然 0 命中**——`execute` 事件是唯一能看到
  ppsspp 工具调用的地方。`oc/events.expand_execute_calls` 负责把 code 展开成
  ToolCall（括号配平取实参、JS 字面量归一、解析失败落 `_raw` 留痕）。

  推论：**`execute` 绝不能 deny**——它是 MCP 的运输层，deny 掉等于禁用 MCP。
  collector agent 只 deny `shell`。

- **工具名归一（v1/v2 分隔符不同）**：v1 是 `ppsspp-dfx_ppsspp_health`（下划线），
  v2 独立 part 形态是 `ppsspp-dfx.ppsspp_health`（点号）。两种都由
  `oc/events.normalize_tool_name` 处理。

- **v2 事件 schema**：工具事件包成
  `{"type":"tool_use","part":{"type":"tool","tool":<name>,"state":{"input":<args>,"output":<result>}}}}`
  —— **参数在 `state.input` 里**，不在节点顶层；`state.metadata` 里是整份工具定义
  清单（几十万字符），解析层必须剪掉，否则 `final_answer` 被淹没。

- **模型侧纪律**：collector agent 的 `steps` 必须给足（实测 12 会让模型在
  拿到工具结果后继续漫游、耗尽预算、交出空答案 → `aborted: Step interrupted`）。
  现在设 30，并在 prompt 里明确「直接调工具、拿到结果立刻作答」。这是**模型质量**
  维度的问题，与解析层无关——管线能采到调用，门禁照样如实判失败。

- **flag 能力探测**：`--attach`（v1）/ `--dir` / `--pure` 在 v2 **均不存在**，
  取而代之的是 `--server`。`oc/runner.py` 运行时探测 `opencode run --help`
  （合并 stdout+stderr——实测正文走 stderr）后只拼装支持的 flag。新增 flag 需先确认两版本行为。

- **XDG 隔离**：`oc/env.py` 把四个 XDG 变量重定向到**每 run 唯一**目录（旧的固定
  `%TEMP%/oc-xdg` 会让并发 run 共享 opencode 状态库，即 `database is locked`），
  同时从真实 profile **拷入** opencode 配置/数据——隔离的是写入，不是认证。
  源目录按平台候选并集解析（opencode v2 在 Windows 上仍用 `~/.local/share/opencode`）。
  设 `PPSSPP_DFX_EVALS_OC_XDG_ROOT` 可改根目录。

- **结构化 error 事件**：provider 认证/额度失败只出现在
  `{"type":"error","error":{"type":"provider.auth",...,"status":403}}` 事件里，
  进程同时非零退出。`oc/events.py` 单独解析并分类为 `provider_auth` /
  `provider_quota` / `rate_limited` / `model_unavailable`，**优先于**按 returncode
  的推断——否则「额度用完」会被显示成「server 失联」，处置方向完全错。

- **熔断（`--circuit-threshold`，默认 3）**：provider 级失败
  （`provider_quota` / `provider_auth` / `rate_limited` / `model_unavailable`）
  **不会自愈**。连续 N 次即熔断剩余网格并给出处置提示；`0` 关闭。
  实测教训：49 卡全量跑到第 8 张撞上额度耗尽，若无熔断，剩下 39 张会逐条产出
  `calls=0` 的同质噪音，把真结果淹没在 JSONL 里还白烧几小时机时。
  **门禁判失败（`no_tool_calls` / `empty_output` / 各 gate 不通过）不触发熔断**——
  那是评测结果，不是环境故障。

- **诊断日志**：`PPSSPP_DFX_EVALS_LOG_FILE=<path>` 让 DEBUG 全量落盘（长作业排障）。
  采集热路径的输出走 progress（`-v`/`-vv`），不直写 stdout。

- **attach URL 白名单**：仅接受 `http://127.0.0.1:*` / `http://localhost:*`——opencode
  会把完整 prompt（含场景卡）发给 server，外部 host 均可接收。

- **agent 工具面**：`.opencode/agents/ppsspp-dfx-collector.md` 只 deny `shell`
  （`execute` 必须留着，它是 MCP 的运输层）。禁用 `shell` 的目的是防止模型退化成
  「用命令行自己模拟调试」。

- **自定义 provider（绕开 opencode 账号额度）**：opencode 官方额度不可用时
  （免费档 CLI 403 / 付费档 402 `Insufficient account funds`），可在
  `<项目根>/opencode.json` 挂 OpenAI 兼容自定义 provider 复用项目既有凭证：

  ```json
  { "provider": { "evals": {
      "npm": "@ai-sdk/openai-compatible",
      "options": { "baseURL": "...", "apiKey": "..." },
      "models": { "<上游真实 model id>": { "name": "..." } } } } }
  ```

  注意 **model key 必须是上游真实 id**——opencode 把 key 原样传给 provider，
  用别名会被拒（`Invalid model id`）。该文件含密钥，已 gitignore。

## Naming conventions

- Modules: snake_case, one purpose per module (`gates.py`, `runner.py`, `oc/events.py`, …).
- Evaluation reports are development-process artifacts and are NOT
  committed to this repository. The runner's interim output
  (`reports/report-<timestamp>.md`, gitignored) is renamed and archived
  wherever the operator keeps such notes.
