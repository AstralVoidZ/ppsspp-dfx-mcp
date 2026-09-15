# ppsspp-dfx 盲测评估

## 结构

| 文件 | 职责 |
|---|---|
| `scenarios.yaml` | 21 张场景卡（CTL×2 / L1×8 / L2×6 / L3×3 / R1-R2 real 模式 ×2，v1.2），ground truth 全部 fixture 驱动 |
| `gates.py` | 确定性门禁（first_tool / params / sequence / answer_contains / recovery / tool_used / final_call_ok / boot_order / no_tool） |
| `test_gates.py` | 门禁单测（每类正反例） |
| `runner.py` | Runner MVP：fake 模式 server 子进程（每 run 隔离）+ OpenAI 兼容 agent loop + 控频 + JSONL + 断点续跑 |
| `config.yaml` | 被测模型（单模型，v1.1 定版）、控频参数、上限 |
| `judge_prompt.md` | L3 方案题的 judge 子代理模板（位置交换协议） |
| `report.py` | 报告生成器：分层成功率/混淆对/门禁挂点/恢复明细/效率 |
| `rescore.py` | 历史轨迹按当前场景卡重评分 |
| `test_b2_skill.py` | B2 变体（skill_read 伪工具，渐进披露模拟） |
| `runs/` | JSONL 轨迹（gitignore） |

## 运行

```bash
# 门禁单测（阶段 1 验收）
<venv python> -m pytest evals/test_gates.py -q

# 冒烟 / 试点（阶段 2 验收：CTL-01 端到端）
<venv python> -u -m evals.runner --scenarios CTL-01 --runs 1

# 全量核心网格（fake 模式场景 × n=5；控频串行，中断后重跑自动续；场景清单以 scenarios.yaml 为准）
<venv python> -u -m evals.runner

# instructions 消融（B0：Runner 侧剥离，零 server 侵入）
<venv python> -u -m evals.runner --variant B0
```

## 依赖与约定

- 解释器：项目 venv 的 python（httpx 等三方 HTTP 库不在依赖内，runner 用标准库 urllib）。
- 密钥：`evals/llm_api.json`（已 gitignore），形如 `{provider: {options: {baseURL, apiKey}, models}}`；路径可用 `config.yaml` 的 `llm_api_path` 或环境变量 `PPSSPP_DFX_EVALS_LLM_API_PATH` 覆盖。provider+模型在 `config.yaml` 指定。
- 变体：B1=工具面+instructions（默认）；B0=剥离 instructions（消融）；B2=skill 伪工具（已实现，`test_b2_skill.py`，需 `PPSSPP_DFX_SKILL_DIR` 指向 skill 目录）。real 模式（R1-R2 真机场景）用 `--mode real`（需 `PPSSPP_DFX_TEST_EXE_PATH` / `PPSSPP_DFX_TEST_ISO_PATH`）。
- L3 方案题（L3-01/L3-02）关键词门禁只是预筛，正式评分需按 `judge_prompt.md` 派发 subagent（位置交换两次）。
- 每 run 一行 JSONL，含四项 provenance 哈希（git commit / tool_surface / instructions / fixtures），resume 按 (scenario, model, variant, run_idx) 去重。

## Naming conventions

- Modules: snake_case, one purpose per module (`gates.py`, `runner.py`, …).
- Evaluation reports are development-process artifacts and are NOT
  committed to this repository. The runner's interim output
  (`reports/report-<timestamp>.md`, gitignored) is renamed and archived
  wherever the operator keeps such notes.
