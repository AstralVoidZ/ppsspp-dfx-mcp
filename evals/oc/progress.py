"""进度输出：与诊断日志分离的第二通道。

对标 `porpoless/progress.py`。动机同源——诊断走 logger（stderr / 可落盘），
面向人的进度走 stdout sink（可被重定向、可被脚本消费），两者互不污染。

`python -m evals.oc` 在 `main()` 里装 stdout sink；单测直接调用时不装，全部静默。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

_sink: Callable[[str], None] | None = None
_level: int = 0  # 0=静默 1=常规 2=详细（事件流逐行）


def install_sink(sink: Callable[[str], None] | None) -> None:
    """安装进度 sink；传 None 恢复静默（单测默认状态）。"""
    global _sink
    _sink = sink


def set_verbosity(level: int) -> None:
    """设置进度详细度：0 静默 / 1 常规 / 2 含事件流逐行。"""
    global _level
    _level = max(0, min(2, int(level)))


def verbosity() -> int:
    return _level


def emit(message: str, *, min_level: int = 1) -> None:
    """输出一条进度消息。`min_level` 过滤详细度，不满足时静默丢弃。"""
    if _sink is not None and _level >= min_level:
        _sink(message)


def install_stdout_sink() -> None:
    """装默认 stdout sink（供 CLI 使用）。"""
    install_sink(lambda msg: print(msg, flush=True))


def uninstall_sink() -> None:
    install_sink(None)


def sink_to(file: Any) -> Callable[[str], None]:
    """返回一个写入指定文本流的 sink（测试 / 报告重定向用）。"""
    return lambda msg: print(msg, file=file, flush=True)
