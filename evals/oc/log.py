"""evals/oc 日志：诊断与吞咽留痕。

对标 `porpoless/log.py` 的分层约定：

- **诊断 / 吞咽** → logger（默认 WARNING 起 stderr 可见）；
- **用户面向的进度** → `evals.oc.progress.emit`（默认静默，CLI 装 stdout sink）；
- 设 `PPSSPP_DFX_EVALS_LOG_FILE` 可把 DEBUG 全量落盘（长作业排障复核用）。

为什么不在 evals 包根再放一个 log：evals/oc 自身是一个自洽子包，且 porpoless 把
日志放在「层中立基础设施」位置的理由（reverse/ 也需要）在本包不成立——oc 内所有
模块都属同一层。保持单层依赖，避免为不存在的需求造结构。

纪律（继承 porpoless `parse/` 约定）：**采集热路径上的任何输出都不得直写
stdout**——`collect.run_scenario` 每 run 调用一次，直写会污染 CLI 输出并 ×N 重复。
"""

from __future__ import annotations

import logging
import os

_configured = False

_ROOT_LOGGER = "evals.oc"
_FILE_ENV = "PPSSPP_DFX_EVALS_LOG_FILE"


def get_logger(name: str = _ROOT_LOGGER) -> logging.Logger:
    """模块级 logger 工厂：首次调用时配置 stderr handler 与可选文件 handler。"""
    global _configured
    logger = logging.getLogger(name)
    if not _configured:
        logger.setLevel(logging.DEBUG)
        # 无参 StreamHandler：emit 时动态解析 sys.stderr。有参构造会把首调时的
        # 流对象缓存死——pytest 全量跑时先行的测试触发过 emit，capsys 就再也读不到
        # 后续测试的 stderr 输出。
        handler = logging.StreamHandler()
        handler.setLevel(logging.WARNING)
        handler.setFormatter(logging.Formatter("[evals-oc][%(name)s] %(levelname)s %(message)s"))
        logger.addHandler(handler)
        log_file = os.environ.get(_FILE_ENV)
        if log_file:
            fh = logging.FileHandler(log_file, encoding="utf-8")  # 行缓冲：长作业实时可见
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
            logger.addHandler(fh)
        _configured = True
    return logger


def log_swallow(logger: logging.Logger, where: str, exc: BaseException) -> None:
    """静默吞咽点的统一留痕。

    采集线上任何 `except Exception: pass` 都必须走这里——无日志的二分排障成本过高，
    这正是旧 `opencode_collect.py` 排障困难的直接原因。
    """
    logger.debug("%s 吞咽异常: %s: %s", where, type(exc).__name__, exc)
