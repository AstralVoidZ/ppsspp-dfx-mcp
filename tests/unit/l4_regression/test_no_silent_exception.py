"""CI 校验：capture.py 中所有 except Exception 块必须包含 logger 调用。

防止静默异常吞没 — 每个 except Exception 都应记录错误信息
（logger.debug/warning/error/info），否则调试时无法定位失败原因。

用 ast 扫描 service/capture.py 源码，找到所有 `except Exception` 块，
检查每个 handler body 内是否包含 `logger.X(...)` 调用。
不递归进入嵌套 ExceptHandler 的 body（避免内层 except 的 logger
误算给外层 except）。

Anchor:
- L4: 每个 except Exception handler 的 body 中至少有一个 logger 调用。
"""

from __future__ import annotations

import ast
import pathlib
from typing import Iterator

import pytest

from ppsspp_dfx_mcp.service import capture as capture_module


def _capture_source_path() -> pathlib.Path:
    """获取 capture.py 源文件路径。"""
    return pathlib.Path(capture_module.__file__)


def _parse_capture_module() -> ast.Module:
    """解析 capture.py 为 AST。"""
    source = _capture_source_path().read_text(encoding="utf-8")
    return ast.parse(source, filename=str(_capture_source_path()))


def _find_except_exception_handlers(
    tree: ast.Module,
) -> list[ast.ExceptHandler]:
    """找到模块中所有 `except Exception` handler。

    匹配 `except Exception` 和 `except Exception as e` 两种形式，
    不匹配 `except` （bare）或 `except OtherException`。
    """
    handlers: list[ast.ExceptHandler] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if node.type is None:
            continue  # bare except — 不检查
        if isinstance(node.type, ast.Name) and node.type.id == "Exception":
            handlers.append(node)
    return handlers


def _walk_skipping_nested_except(node: ast.AST) -> Iterator[ast.AST]:
    """遍历 node 及其所有后代，但不递归进入嵌套 ExceptHandler 的 body。

    这样外层 except handler body 中的 logger 调用会被找到，
    但内层 except handler body 中的 logger 调用不会被误算给外层。
    """
    yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ExceptHandler):
            # yield handler 节点本身（便于检查），但不递归进入其 body
            yield child
            continue
        yield from _walk_skipping_nested_except(child)


def _is_logger_call(node: ast.AST) -> bool:
    """判断 node 是否为 logger.X(...) 调用。"""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    if not isinstance(func.value, ast.Name):
        return False
    return func.value.id == "logger"


def _handler_has_logger_call(handler: ast.ExceptHandler) -> bool:
    """检查 except handler body 是否包含 logger.X() 调用。

    递归搜索 handler body 中的语句，但不进入嵌套 ExceptHandler
    的 body（避免内层 except 的 logger 误算给外层）。
    """
    for stmt in handler.body:
        for node in _walk_skipping_nested_except(stmt):
            if _is_logger_call(node):
                return True
    return False


class TestNoSilentException:
    """capture.py 中所有 except Exception 块必须包含 logger 调用。"""

    def test_at_least_one_except_exception_exists(self):
        """前提：capture.py 中至少有一个 `except Exception` 块。

        如果此断言失败，说明 capture.py 被大幅重构（所有 except
        Exception 被替换为具体异常类型），需人工确认此测试是否仍需保留。
        """
        tree = _parse_capture_module()
        handlers = _find_except_exception_handlers(tree)
        assert len(handlers) >= 1, (
            "capture.py 中应至少有一个 `except Exception` 块 — "
            "如果全部改为具体异常类型，需人工确认此测试是否仍适用。"
        )

    def test_all_except_exception_have_logger_call(self):
        """L4 anchor: 每个 `except Exception` 块都包含 logger 调用。

        遍历 capture.py 中所有 `except Exception` handler，检查
        每个 handler body 中是否至少有一个 logger.debug/warning/
        error/info 调用。缺少 logger 调用的 handler 会静默吞没异常，
        导致调试时无法定位失败原因。
        """
        tree = _parse_capture_module()
        handlers = _find_except_exception_handlers(tree)
        silent_handlers: list[str] = []
        for handler in handlers:
            if not _handler_has_logger_call(handler):
                silent_handlers.append(
                    f"line {handler.lineno}: except Exception "
                    f"{'as ' + handler.name + ' ' if handler.name else ''}"
                    f"缺少 logger 调用"
                )
        assert not silent_handlers, (
            "capture.py 中以下 `except Exception` 块缺少 logger 调用"
            "（静默异常吞没）:\n  "
            + "\n  ".join(silent_handlers)
            + "\n每个 except Exception 块必须包含 logger.debug/warning/"
            "error/info 调用以记录错误信息。"
        )

    def test_no_bare_except(self):
        """L4 anchor: capture.py 中不应有 bare `except:`（无异常类型）。

        bare except 会捕获所有异常（包括 KeyboardInterrupt、
        SystemExit），应使用 `except Exception` 代替。
        """
        tree = _parse_capture_module()
        bare_excepts: list[int] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and node.type is None:
                bare_excepts.append(node.lineno)
        assert not bare_excepts, (
            f"capture.py 中存在 bare `except:`（无异常类型），"
            f"行号: {bare_excepts} — "
            f"应使用 `except Exception` 代替以避免捕获 "
            f"KeyboardExit/SystemExit。"
        )
