"""结构化失败信号：让「为什么没采到」可判定，而不是靠文本嗅探。

对标 `porpoless/parse/runner_base.py` 的 `LLMResponse.status_code / error_kind`
与 `porpoless/parse/runtime.py` 的错误分类。

设计要点：**失败以数据表达，不以异常表达**。采集循环据此计数、续跑、写报告；
只有「编排层无法继续」的错误才抛 `CollectorError`。
"""

from __future__ import annotations


class ErrorKind:
    """`error_kind` 取值常量（字符串字面量，便于 JSONL 落盘与前端展示）。"""

    #: opencode 内部状态库锁竞争（实测形态：`database is locked`）
    LOCKED = "locked"
    #: attach 的 server 失联——client 能启动、自己报连接错后非零码退出
    SERVER_GONE = "server_gone"
    #: 子进程跑了但 stdout 与 stderr 都没有可解析内容
    EMPTY_OUTPUT = "empty_output"
    #: 撞 `timeout_s` 上限被强杀
    TIMEOUT = "timeout"
    #: 无法拉起子进程（bin 不存在 / 不可执行）
    SPAWN_FAILED = "spawn_failed"
    #: agent 定义文件找不到，fail fast
    AGENT_MISSING = "agent_missing"
    #: attach URL 非本机地址（安全闸门，拒绝把场景内容发往外部主机）
    BAD_URL = "bad_url"
    #: 事件流解析出内容但没有工具调用，且没有任何工具调用可供诊断
    NO_TOOL_CALLS = "no_tool_calls"
    #: opencode 事件流里的结构化 error（`{"type":"error","error":{...}}`）：
    #: 认证/额度（provider.auth 403）、限流、模型不存在等。**必须**独立于
    #: server_gone 暴露——两者的处置完全不同（前者要换模型/等额度，后者要重启
    #: server），混为一谈会把「额度用完」显示成「server 失联」。
    PROVIDER_AUTH = "provider_auth"
    RATE_LIMITED = "rate_limited"
    #: 账号额度/余额耗尽（实测 `provider.quota` + 402 "Insufficient account funds"）。
    #: 与 RATE_LIMITED 分开：前者要充值/换账号，后者等窗口即可——处置完全不同。
    PROVIDER_QUOTA = "provider_quota"
    MODEL_UNAVAILABLE = "model_unavailable"
    #: 事件流 error 的兜底类
    STREAM_ERROR = "stream_error"

    ALL = frozenset(
        {
            LOCKED,
            SERVER_GONE,
            EMPTY_OUTPUT,
            TIMEOUT,
            SPAWN_FAILED,
            AGENT_MISSING,
            BAD_URL,
            NO_TOOL_CALLS,
            PROVIDER_AUTH,
            RATE_LIMITED,
            PROVIDER_QUOTA,
            MODEL_UNAVAILABLE,
            STREAM_ERROR,
        }
    )


class CollectorError(RuntimeError):
    """编排层致命错误：无法继续（区别于「单个 run 采失败」）。

    Attributes:
        kind: `ErrorKind` 中的取值，供 CLI 决定退出码与提示。
    """

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.kind = kind
