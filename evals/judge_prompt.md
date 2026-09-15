# LLM-as-Judge 子代理评分模板（位置交换协议）

> 用法：本方案（v1.1 §5.3）的 judge 是 **ZCode subagent**，不新增外部模型。
> 对每个待评轨迹对，用下面模板派发两次子代理：第一次 A 在前 B 在后，第二次
> 交换顺序。两次分差 >1 记 inconclusive。judge 与被测模型应保持家族分离
> （被测模型以 `evals/config.yaml` 当前配置为准；
> 更换被测模型时须复核本前提仍然成立）。

---

## 派发 prompt（占位符：{{TRAJECTORY_A}} / {{TRAJECTORY_B}} = JSONL 轨迹 JSON；{{TASK}} = 场景 prompt）

你是 MCP 工具使用质量的独立评审员。下面是两个 Agent 在同一任务上的完整
交互轨迹（工具调用序列 + 每次结果 + 最终回答）。请按统一 Rubric 打分。

任务：{{TASK}}

【轨迹 A】
{{TRAJECTORY_A}}

【轨迹 B】
{{TRAJECTORY_B}}

Rubric（每项严格按定义打分，总分 0–5）：
1. 正确性（0–2）：最终回答的事实是否正确、是否完成任务目标。
2. 证据引用（0–1）：是否基于轨迹中的工具结果作答。**你必须注明所依据的
   工具调用 seq 号；没有给出 seq 依据的评分无效。**
3. 可执行性（0–1）：给出的建议/结论能否直接被执行。
4. 简洁（0–1）：无冗余、无编造。

输出格式（严格 JSON，不要其他文字）：
{"A": {"correctness": 0, "evidence_seq": [1], "actionable": 0, "concise": 0,
       "total": 0, "reason": "一句话依据"},
 "B": {...同结构...}}
