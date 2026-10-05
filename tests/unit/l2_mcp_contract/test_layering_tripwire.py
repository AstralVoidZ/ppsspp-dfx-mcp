"""Layering contract tripwire (architecture review v1 §4.1).

Static import-graph assertion: lower layers must never import the tools
layer (and the pure contract layers must stay leaf-ish). This is the
permanent guard for the P2 layering-inversion convergence — the three
inversions it was written against (core/batch_jobs, core/stepping,
service/debug_client importing tools._common / session_manager) must
never regrow.

Run (from the repository root/):
    <venv python> -m pytest tests/unit/l2_mcp_contract/test_layering_tripwire.py -q
"""

from __future__ import annotations

import ast
from pathlib import Path

# parents[3] = the subproject root (tests/unit/l2_mcp_contract/<this file>).
# NOTE: this used to be parents[2], which pointed at tests/src — a missing
# directory, so rglob() yielded nothing and the tripwire was silently
# vacuous. Fixed while adding the session→tools edge (W19).
_SRC = Path(__file__).resolve().parents[3] / "src" / "ppsspp_dfx_mcp"

# forbidden[importing_layer] = {imported_layers}
FORBIDDEN: dict[str, frozenset[str]] = {
    "core": frozenset({"tools"}),
    "service": frozenset({"tools"}),
    # `session` sits above `service`; it used to reach into `tools` for the
    # probe-registry side table (W19). That reverse edge is gone — the
    # registry now lives in `service/probe_observer.py` — and must not regrow.
    "session": frozenset({"tools"}),
    "models": frozenset({"tools", "session", "service"}),
    "views": frozenset({"tools", "session", "service"}),
    # specs/010 US4 / L1: the composition root must not be a library the tool
    # layer reaches into. `server` owns the service instances, the tool count
    # and the dynamic exposure chain; tools importing it is what forced the
    # test suite to patch server attributes (12 patch points in
    # test_lifespan_integration.py alone). The home for those objects moves to
    # a leaf module; until then this edge MUST be empty.
    "tools": frozenset({"server"}),
}


def _package_imports(tree: ast.AST, package: tuple[str, ...] = ()) -> set[str]:
    """All ppsspp_dfx_mcp submodule names imported anywhere in the tree
    (top-level AND function bodies — lazy imports count too).

    ``package`` is the dotted package of the module being scanned, used to
    resolve **relative** imports (``from .. import server``). Without it the
    relative form silently escaped the guard — the same class of hole as the
    ``from ppsspp_dfx_mcp import server`` form.
    """
    found: set[str] = set()

    def _record(dotted: str) -> None:
        parts = dotted.split(".")
        if parts[0] == "ppsspp_dfx_mcp" and len(parts) > 1:
            found.add(parts[1])

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _record(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # Resolve against the importing module's own package.
                base = package[: len(package) - (node.level - 1)] if package else ()
                if node.module:
                    _record(".".join([*base, *node.module.split(".")]))
                for alias in node.names:
                    _record(".".join([*base, alias.name]))
            elif node.module and node.module.startswith("ppsspp_dfx_mcp."):
                _record(node.module)
            elif node.module == "ppsspp_dfx_mcp":
                # `from ppsspp_dfx_mcp import server` — the submodule name lives
                # in the aliases, not in `node.module`. Missing this form left a
                # hole exactly on the edge specs/010 US4 has to forbid
                # (measured: the guard reported 0 violations while 27 tool
                # modules imported the composition root).
                for alias in node.names:
                    _record(f"ppsspp_dfx_mcp.{alias.name}")
            elif node.module and node.module.split(".")[0] in FORBIDDEN:
                # relative-style `from tools import x` at an unusual level
                found.add(node.module.split(".")[0])
    return found


def _scan_layer(layer: str) -> list[str]:
    """该层中违反 FORBIDDEN 的文件列表（每项 `layer/file.py imports [...]`）。"""
    violations: list[str] = []
    layer_dir = _SRC / layer
    if not layer_dir.is_dir():
        return violations
    for py in layer_dir.rglob("*.py"):
        rel = py.relative_to(_SRC)
        # 模块自身所属包（用于解析相对导入）：`ppsspp_dfx_mcp.tools` 下的文件传
        # ("ppsspp_dfx_mcp", "tools")。
        package = ("ppsspp_dfx_mcp", *rel.parts[:-1])
        tree = ast.parse(py.read_text(encoding="utf-8"))
        bad = _package_imports(tree, package) & FORBIDDEN[layer]
        if bad:
            violations.append(f"{rel.as_posix()} imports {sorted(bad)}")
    return violations


def test_lower_layers_never_import_tools():
    violations: list[str] = []
    for layer in FORBIDDEN:
        violations.extend(_scan_layer(layer))
    assert not violations, (
        "layering violation — a layer importing a layer above it: "
        f"{violations}. Move the shared symbol down into core "
        "(see CONTRIBUTING.md — tools may import core, never the reverse)."
    )


def test_tool_layer_never_imports_the_composition_root():
    """specs/010 US4 / L1：工具层 MUST NOT 依赖组合根。

    这是本批次要消掉的那条反向依赖（实测 27 个文件 / 30 处）。单独成条而不是
    并进上面的循环，是为了让失败信息直接指向"工具层 → 组合根"这一条边。
    """
    violations = _scan_layer("tools")
    assert not violations, (
        "tools/ MUST NOT import the composition root (ppsspp_dfx_mcp.server) — "
        f"{len(violations)} 个文件违反：{violations}\n"
        "        服务实例、工具数与动态暴露链应下沉到叶子模块（specs/010 T049）。"
    )


def test_guard_detects_every_import_shape_of_the_new_edge():
    """守卫的守卫：把 `tools → server` 的每种写法都试一遍，MUST 全部被识别。

    为什么要这条：原实现只认 `ppsspp_dfx_mcp.` 前缀与 `import` 形态，
    `from ppsspp_dfx_mcp import server` 与相对形式都能绕过——于是一边"零违规"，
    一边 27 个文件在导入组合根。
    """
    pkg = ("ppsspp_dfx_mcp", "tools")
    cases = {
        "import 形态": "import ppsspp_dfx_mcp.server\n",
        "import 别名形态": "import ppsspp_dfx_mcp.server as s\n",
        "from 包根 import 子模块": "from ppsspp_dfx_mcp import server\n",
        "from 子模块 import 符号": "from ppsspp_dfx_mcp.server import MCPServer\n",
        "相对形式（带模块名）": "from .. import server as s\n",
        "相对形式（from ..server import）": "from ..server import MCPServer\n",
        "函数体内延迟导入": "def f():\n    from ppsspp_dfx_mcp import server\n    return server\n",
        "try 块内导入": "try:\n    from ppsspp_dfx_mcp import server\nexcept ImportError:\n    server = None\n",
    }
    missed = [
        name for name, src in cases.items() if "server" not in _package_imports(ast.parse(src), pkg)
    ]
    assert not missed, f"以下导入形态未被守门识别（可绕过）：{missed}"


def test_guard_does_not_flag_legal_tool_layer_imports():
    """反向自查：守门不能把合法依赖也判成违规（否则会被"放宽判据"消音）。"""
    pkg = ("ppsspp_dfx_mcp", "tools")
    for src in (
        "from ppsspp_dfx_mcp.core import proc\n",
        "from ppsspp_dfx_mcp.service import debug_client\n",
        "from ppsspp_dfx_mcp.spec import error_codes\n",
    ):
        assert not (_package_imports(ast.parse(src), pkg) & FORBIDDEN["tools"]), src


def test_tripwire_catches_a_real_violation():
    """Guard the guard: the scanner must flag a planted violation."""
    tree = ast.parse("from ppsspp_dfx_mcp.tools._common import X")
    assert "tools" in _package_imports(tree)
    tree = ast.parse("from ppsspp_dfx_mcp.core import proc")
    assert "tools" not in _package_imports(tree)
    # Defensive: a lazy import inside a function body must be caught too —
    # that is exactly how the session→tools edge looked before W19.
    tree = ast.parse("def f():\n    from ppsspp_dfx_mcp.tools import state_observer\n")
    assert "tools" in _package_imports(tree)


def test_probe_registry_lives_in_the_service_layer():
    """W19: the probe registry moved out of `tools/` so `session/` need not
    import `tools/` to reclaim it. Lock the new home."""
    from ppsspp_dfx_mcp.service import probe_observer  # noqa: PLC0415 — local: prove importability

    # The module-level containers the tool layer re-exports must be the same
    # objects (no second copy drifting away).
    from ppsspp_dfx_mcp.tools import state_observer as tool_side  # noqa: PLC0415

    assert tool_side._REGISTRY_BY_SESSION is probe_observer._REGISTRY_BY_SESSION
    assert tool_side._SEEDED_BY_SESSION is probe_observer._SEEDED_BY_SESSION
    assert tool_side.drop_session is probe_observer.drop_session


def test_batch_step_calls_the_screenshot_service_not_the_tool():
    """W19 structural lock: the batch screenshot step must delegate to
    `service.screenshot_service.capture_frame`.

    Calling the decorated `tools.screenshot.screenshot` tool would (a) use a
    tool as a library and (b) stack the tool error translator on top of the
    step's own handler. This asserts on the AST, so it fails if the reverse
    edge is reintroduced.
    """
    src = (_SRC / "tools" / "batch_step.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert "ppsspp_dfx_mcp.tools.screenshot" not in imported, (
        "batch_step imports the screenshot TOOL module again — it must call "
        "service.screenshot_service.capture_frame"
    )

    called = {
        n.func.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "screenshot" not in called, "batch_step calls the screenshot tool function"
    assert "capture_frame" in called, "batch_step no longer calls capture_frame"
    assert "ppsspp_dfx_mcp.service.screenshot_service" in imported
