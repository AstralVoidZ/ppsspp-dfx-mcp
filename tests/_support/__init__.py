"""tests/_support — 集中式测试支撑缝包（specs/010 T053 S-4）。

`state.py` 是生产私有共享状态的**唯一直写点**：S-4 守门
（`tests/unit/l2_mcp_contract/test_shared_state_guard.py::
test_tests_never_write_production_shared_state`）显式豁免本目录。
"""
