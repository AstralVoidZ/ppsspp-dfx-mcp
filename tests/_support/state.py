"""specs/010 T053 S-4 — 生产共享状态的集中式测试支撑缝（唯一直写点）。

守门 `test_shared_state_guard.py::test_tests_never_write_production_shared_state`
扫描 tests/ 下对登记容器（REGISTRY）的直写，并**显式豁免本目录**：对生产
私有容器的写入 MUST 收敛在本文件内，其余测试经本缝的具名函数或生产语义化
回收 API 操作容器。

为什么是具名函数而不是泛型 `container(mod, name)` 穿墙取引用：泛型会把
直写重新散布回各测试文件（`container(...)[sid] = ...`），S-4 的审计面等于
没收敛。缝内每一处直写都可 grep、可审计、可对照 REGISTRY 登记。

历史注：`tests/unit/probe_isolation.py` 是更早的测试支撑先例（T005），
其 `_reset()` 经 getattr 别名写容器——属 S-4 检测盲区而非豁免对象；
本轮（T053）不搬动它，其收敛留待后续任务。

新增容器时的接线顺序：
  1. 在 test_shared_state_guard.REGISTRY 登记（S-1）；
  2. 生产侧确认/补语义化回收 API（S-2/S-3）；
  3. 测试需要种子/全清时，在本文件补具名函数（缝内直写），其余测试不得绕缝。

注意：本缝 import 的是**源模块**（service.probe_observer 等），不是
tools.state_observer 的 re-export——写路径永远作用同一容器对象，但源模块
才是容器的定义点，绑定到源可避免 re-export 面变化时的静默错绑。
"""

from __future__ import annotations

from typing import Any

from ppsspp_dfx_mcp.core import cond_filter
from ppsspp_dfx_mcp.service import probe_observer
from ppsspp_dfx_mcp.session import client_helper
from ppsspp_dfx_mcp.tools import _common

# ── 种子：service/probe_observer ─────────────────────────────────────────


def seed_probe_registry(session_id: str, entry: dict[str, Any]) -> None:
    """向 `_REGISTRY_BY_SESSION` 种入一个会话条目。"""
    probe_observer._REGISTRY_BY_SESSION[session_id] = entry  # noqa: SLF001 — S-4 缝内直写


def seed_probe_seeded(session_id: str) -> None:
    """点亮 `_SEEDED_BY_SESSION` 的惰性播种闩。"""
    probe_observer._SEEDED_BY_SESSION.add(session_id)  # noqa: SLF001 — S-4 缝内直写


# ── 种子：其余会话侧表（test_w3_a9_a18 的组合种子基元）────────────────────


def seed_zero_streaks(session_id: str, streaks: dict[int, int]) -> None:
    """向 `_ZERO_STREAKS`（tools/_common）种入一个会话条目。"""
    _common._ZERO_STREAKS[session_id] = streaks  # noqa: SLF001 — S-4 缝内直写


def seed_fake_transport(session_id: str, transport: Any) -> None:
    """向 `_FAKE_TRANSPORTS`（session/client_helper）种入一个会话条目。"""
    client_helper._FAKE_TRANSPORTS[session_id] = transport  # noqa: SLF001 — S-4 缝内直写


def seed_cond_filter(key: tuple[str, int], value: dict[str, Any]) -> None:
    """向 `_filters`（core/cond_filter）种入一个 (session, addr) 条目。"""
    cond_filter._filters[key] = value  # noqa: SLF001 — S-4 缝内直写


def seed_all_side_tables(session_id: str) -> None:
    """向每个模块级会话侧表各种入一条 session_id 条目（W3/A9 判据用）。"""
    seed_probe_registry(session_id, {"0x1000": object()})
    seed_probe_seeded(session_id)
    seed_zero_streaks(session_id, {0x1000: 2})
    seed_fake_transport(session_id, object())
    seed_cond_filter((session_id, 0x1000), {"condition": "== 1", "hits": 0})


# ── 全清：生产无语义化 clear-all API 的容器 ───────────────────────────────


def clear_probe_sessions() -> None:
    """清空 probe 注册表与播种闩（两个容器，进程级）。"""
    probe_observer._REGISTRY_BY_SESSION.clear()  # noqa: SLF001 — S-4 缝内直写
    probe_observer._SEEDED_BY_SESSION.clear()  # noqa: SLF001 — S-4 缝内直写


def clear_probe_registry() -> None:
    """仅清空 probe 注册表（保留播种闩——A-5 僵尸清空语义依赖该差别）。"""
    probe_observer._REGISTRY_BY_SESSION.clear()  # noqa: SLF001 — S-4 缝内直写


def clear_cond_filters() -> None:
    """清空条件过滤器表（进程级，测试隔离用）。"""
    cond_filter._filters.clear()  # noqa: SLF001 — S-4 缝内直写


# ── 快照/恢复：进程全局侧表的保存-测试-还原（test_w3 fixture）─────────────


def snapshot_side_tables() -> dict[str, Any]:
    """浅拷贝全部模块级会话侧表，供 `restore_side_tables` 还原。"""
    return {
        "registry": dict(probe_observer._REGISTRY_BY_SESSION),  # noqa: SLF001 — S-4 缝
        "seeded": set(probe_observer._SEEDED_BY_SESSION),  # noqa: SLF001 — S-4 缝
        "streaks": dict(_common._ZERO_STREAKS),  # noqa: SLF001 — S-4 缝
        "fakes": dict(client_helper._FAKE_TRANSPORTS),  # noqa: SLF001 — S-4 缝
        "filters": dict(cond_filter._filters),  # noqa: SLF001 — S-4 缝
    }


def restore_side_tables(snap: dict[str, Any]) -> None:
    """把 `snapshot_side_tables` 的快照还原回容器（clear + update）。"""
    probe_observer._REGISTRY_BY_SESSION.clear()  # noqa: SLF001 — S-4 缝内直写
    probe_observer._REGISTRY_BY_SESSION.update(snap["registry"])  # noqa: SLF001 — S-4 缝内直写
    probe_observer._SEEDED_BY_SESSION.clear()  # noqa: SLF001 — S-4 缝内直写
    probe_observer._SEEDED_BY_SESSION.update(snap["seeded"])  # noqa: SLF001 — S-4 缝内直写
    _common._ZERO_STREAKS.clear()  # noqa: SLF001 — S-4 缝内直写
    _common._ZERO_STREAKS.update(snap["streaks"])  # noqa: SLF001 — S-4 缝内直写
    client_helper._FAKE_TRANSPORTS.clear()  # noqa: SLF001 — S-4 缝内直写
    client_helper._FAKE_TRANSPORTS.update(snap["fakes"])  # noqa: SLF001 — S-4 缝内直写
    cond_filter._filters.clear()  # noqa: SLF001 — S-4 缝内直写
    cond_filter._filters.update(snap["filters"])  # noqa: SLF001 — S-4 缝内直写
