# 更新日志

本项目的所有显著变更将记录在此文件。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.1.7] - 2026-10-03

> 本节按批次累积，最新批次在前。条目源自 v4 系统性代码审查报告的 §3.3 路线图，
> 全项已在本节覆盖闭环（各批次的「守卫制度」随每项修复同批落地）。
> 其后追加两批：**工具面缺陷根治**（2026-10-05）与
> **遗留问题收口与发布就绪**（2026-10-04），分别见下。

### Fixed（工具面缺陷根治）

工具面深度测试列出的 16 项，已逐项复现并修复（15 项复现 + 1 项环境），每项独立提交、附可证伪测试：

- **会话与入参契约统一**：8 个会话型工具与 `run_script` 的 0 会话错误码由两种收敛为
  单一 `SESSION_NOT_FOUND`；schema 校验失败不再裸透传 pydantic dump（含内部类名与
  `errors.pydantic.dev` 链接），改由 `tool_error_middleware` 前置拦截并返回
  `[ARGS_INVALID] <field>: <msg>`；`ppsspp_breakpoint` / `ppsspp_replay` 的
  `session_id` 由必填改为可省略，与其余工具一致。
- **输出契约**：`ppsspp_query(action=register)` 补文本与寄存器名回显；
  `read_u32` 文本定宽补零；`read_string` 区分「空串」与「地址当前无效」；
  `ppsspp_memory_map` 的 `mapping.ticket` 移出业务字段以恢复整包幂等。
- **错误领域化与时延**：`func_remove` 失败不再裸透传 `PPSSPP_PROTOCOL_ERROR`；
  `ppsspp_step` 的 resume 由固定 3s 预算改为广播快路径并返回位置快照；
  `ppsspp_dump` 空捕获由 22–32s 降至毫秒级（预算叠加收口）。
- **判据纠偏**：`ppsspp_context` 的 identity 偏移加距离上限；
  `wait_frames` / `press_button` 显式声明并拒绝 0 值语义；
  `ppsspp_gpu_stats` 非渲染态不再误报 `CPU_FREEZE_SUSPECTED`。
- **版本一致守门**：新增 `test_version_consistency_gate.py`，钉住
  `__version__ == pyproject.version == serverInfo.version`（该项为陈旧 editable
  dist-info 的**环境**产物，非代码缺陷，处置方式为安装重同步 + 守门）。

同批修复**真机验收门自身不可运营**的结构性缺陷（`scripts/_wire.build_server_env()` 丢弃全部
`PPSSPP_DFX_*` 导致门无法配置其启动的子进程）、边界探针矩阵的 11 项存量漂移（旧工具名 /
旧契约 / 两处假信号），以及两处「消息教 Agent 使用已退役工具」的产品缺陷
（`memory_trace_wizard` 的 `access=` 参数、`observer_lookup` 的无观察者提示）。
真机验收结果：真机门 `156/7 → 167/2`（余 2 项为 TRAE 沙箱拦截 GPU 缓存的**环境**性抖动，
非代码）；边界探针矩阵 `47/11 → 58/0`。

### Fixed / Added（遗留问题收口与发布就绪）

处置遗留清单登记的全部 15 项（含研究阶段新发现），
并建立发布同步与内容边界契约：

- **测试结论可自证**：全量运行判定器拆分语义——`verdict` → `regression_verdict` +
  `suite_green`，判定消费摘要失败计数并新增 `parse_mismatch`（「摘要报失败但明细缺失」
  必判不通过），记录 schema 升至 `regression-run/2`；新增只读复核脚本
  `inspect_derived_records.py`；补跑落档，使报告头号数字可溯源。
- **发布可安全落地**：修正 `uv.lock` 与 `pyproject.toml` 不一致（`uv lock --check`
  退出码 1 → 0）；`scripts/ppsspp_dfx_mcp_release.py` 新增**只读裁决**子命令（存在未裁决项时
  拒绝同步）；`.gitattributes` 以 `export-ignore` 声明跑批 / 评测产物边界并补忽略规则；
  `.mcp.json` 改为通用运行器 + 包入口名（移除绝对路径与调试默认）；`check_env.py` 增补
  接入目标与锁一致性自检；`README` / `README.en` 补工作目录假设与绝对路径变体。
  完成导出同步与**发布仓本地提交**。
- **架构债收口**：新增叶子模块 `registry.py`（工具层不再反向依赖组合根，并有门禁断言防复发）；
  共享状态重置收敛到唯一入口（生产侧只保留语义化回收 API）；错误翻译单轨化，并以 37 工具
  异常注入的等价判据锁定。
- **真机与量化**：新增 `test_ws_upgrade_no_subprotocol.py` / `test_cancel_storm.py`
  （真机门控，已入跳过白名单）与 `scripts/measure_event_loop_lag.py`（对照法测主循环延迟）。
- **文档口径**：技能目录纳入既有规范守门（含项目侧技能）；时效数字补截止日期。

> **授权边界（沿用 2026-10-03 用户裁决）**：本批仅完成「导出同步 + 发布仓本地提交」，
> **未**打标签、**未**推送远端、**未**对外发布（含制品库）；三项待授权动作见
> 待授权动作清单（打标签 / 推送远端 / 对外发布）。
> 跨平台握手实证当前**仅覆盖 Windows**（POSIX 侧因 `wsl.exe` 被本机安全策略禁用而未验证）。

### Added（v0.1.7 回填批）

- **scan 预算守卫（回填开源仓 v0.1.6 之后内容）**：开源仓实测 24 MiB@4 KiB
  分块前台扫描耗时 53–96s，恒超 MCP 客户端 ~30s 取消线，客户端取消→重试→
  超时循环正是「会话冻结只能 stop 重启」的真实来源。`core/primitives.py`
  新增四常量（`FOREGROUND_SCAN_LIMIT_BYTES=2 MiB`、`SCAN_READ_TIMEOUT_S=10s`、
  `SCAN_MAX_CONSECUTIVE_READ_FAILURES=5`、`SCAN_BG_BUDGET_S=600s`）；
  `tools/scan.py` 的 pattern/strings 模式超限自动转后台作业，逐块读加超时与
  连续失败中止；`service/debug_client.py` 的 `scan_memory` 逐块超时。新增
  `test_scan_budget_guard`（8 例）。
- **src 实现补交**：上一批因 `git add` pathspec 笔误原子回滚，
  只提交了 tests/scripts/SCOPE/views，src 侧 11 个文件滞留工作树，本批补交：
  `core/call_attribution.py` 的 `CAUSE_NO_PRODUCER` 提示纠错、
  探针失效第四态 `stale_address_suspected`（连续零读 streak）、
  `tools/breakpoint.py`/`memory.py` 的修复、`gpu_stats.py` 三维归因接入
  `CPU_FREEZE_SUSPECTED` 路径与 WsTimeout 双前缀修复、三处条件必填声明。

### Fixed（v3 审查修复批）

- **写保护闭环**：`ppsspp_assemble` 原先只在会话外做一次不带模块表的
  检查，窄 extent（`0x08804000..0x08814000`）之外约 97% 的真实 top.prx 代码段
  可无 force 写入；`write_memory` 在 `module_list` 失败时以
  `if session_modules:` 直接跳过权威检查（fail-open）。现
  `memory_protection` 抽出 `resolve_session_modules()`（模块表缺失/类型错/为空
  返回 `None` 并告警），会话内统一接入权威模块表，模块表缺失时退化为保守
  extent 而非跳过；删除从未接线的死代码。l1_contract 增 Step4 十条 + 新增
  `test_s1_s3_write_protection_paths.py`（8 条工具层锁）。
- **截图稳定等待加 deadline**：`_wm_command_screenshot` 的内层
  `while stable_samples < 2` 无上界、无 deadline，判据 `0 < size == last_size`
  在文件恒为 0 字节时（写一半被杀/磁盘满/上次残留空文件）永不成立，而该循环
  持会话锁且 CPU 停在 STEPPING → 整个会话不可用。抽出模块级
  `_await_stable_image(path, deadline)`：以 `time.monotonic()` 为上界，到点返回
  `b''`，`read_bytes` 移入 `asyncio.to_thread`；新增 `_WM_SCREENSHOT_WAIT_S=5.0`
  / `_WM_SCREENSHOT_POLL_S=0.05`。实测 0 字节 stuck 文件：修复前 2.00s 仍在
  循环 → 修复后 0.28s 返回。回归锁 `test_s4_wm_screenshot_wait.py`（8 条）。
- **日志镜像 cap 重启越界 + 启动期轮转**：`_cap_marked` 为 handler 实例属性
  且写 marker 后不再复查上限，文件停在 `(cap-128, cap]` 区间时每次重启再追加
  约 104 字节 marker 即越界（10,485,840 > 10,485,760）；读取端 `analyze_log`
  对 `size > MAX_LOG_BYTES` 直接抛 `ArgsInvalid` → 默认路径永久失效且无自愈
  （从 ≤0.1.6 升级即中招）。新增 `_write_cap_marker(size, line_bytes)` 先做
  fit-check（不足则退化 33 字节 compact marker，绝不越界）与
  `_rotate_oversized_mirror()` 启动轮转自愈。回归锁
  `test_s2_log_mirror_restart_cap.py`（7 条）。
- **lint 门禁回绿并钉齐 ruff**：本 HEAD 上 `ruff check .` 报 8 处违规、
  `ruff format --check .` 报 9 个文件待排（与文档「0 错 0 待排」相反，CI lint
  job 必红），现全部修净（6 处自动 + 2 处手工）。本地/CI 版本漂移：`.venv` 与
  `uv.lock` 为 0.16.8，而 pyproject 两处与 ci.yml 钉 0.16.6 → 三处统一 0.16.8。
  顺带修复 `scripts/run_quickstart_checks.py` 的本地绝对路径硬编码（改为从
  `__file__` 推导，兼解除根仓提交隐私扫描的阻断，行为在本仓不变）。
- **启动期取消不再遗留孤儿进程或复活已停会话**：
  `_start_once` 原先只在 sessions.json 持久化成功后才登记
  `_launchers[session_id]`，其间每个 await 都是取消点（客户端 ~30s 取消 vs
  实测未 pin 后端启动 31.9s），取消落在该窗口时进程已 spawn 却无任何内存归属
  → 永久孤儿；现 spawn 成功后立即登记，各失败/中止路径显式回收（新增
  `_abandon_launcher`）。`stop_session` 与「WS 探测成功→写两张表」之间
  存在窗口，无条件写入会让刚被 stop 的会话条目复活并泄漏 transport/observer；
  现建立块在同一把锁内重查 sessions.json 存活性（会话已消失抛
  `SessionNotFound` 中止），新增 `_close_transport_and_observer` 统一关闭。
- **version 握手改用专用队列**：`send_version()` 的 ticketless 回退与
  observer dispatcher 抢同一个 `events` 队列；首次连接无竞争，但自动重连时
  dispatcher 已在排空 `events`，`"version"` 不在 `_SUBSCRIBED_EVENTS` 被静默
  丢弃 → 回退耗尽预算抛 `RuntimeError` 并被包成「reconnect failed」，该会话此后
  每次工具调用都失败且无自愈路径。新增有界专用队列 `_version_queue`
  （max=8，drop-oldest），`_recv_loop` 将 ticketless 的 `version` 路由至此，
  竞争在结构上消失。回归锁 `test_w30_version_handshake_routing.py`（3 条）。
- **events 队列有界 drop-oldest + 自身回送不再充当超时归因证据**：
  `WsTransport._events_queue` 原为无界 `asyncio.Queue()`，唯一普通消费者
  `wait_for_broadcast()` 对不匹配消息只 `_requeue()` 永不丢弃，
  `fire_and_forget()` 的回送无 future 归属 → 长会话队列单调增长；改为有界
  `maxsize=256`，统一入队入口 `_put_event()` 满时丢最旧并计入 `_events_dropped`。
  `_recv_loop` 对每条 unmatched 帧记账 producer 存活，自身 fire-and-forget
  的回送会误盖这枚戳，把「模拟器卡死」误诊成「配对错误」；现将
  `fire_and_forget` 的 ticket 登入有界 `_ff_tickets`（maxlen=64），命中集合的
  回送不记账（纵深防御，当前无可达调用）。回归锁
  `test_w1_w2_event_queue_bound.py`（6 条）。
- **预算算术与 `call()` 默认超时同源**：三处轮询循环
  （`transport.wait_for_state`、`debug_client.replay_wait_complete`、
  `safe_boot.probe_cpu_ready`）声明的 wall-clock 预算对每次轮询无效（轮询沿用
  `call()` 默认 5s，且 deadline 只在成功往返后检查），声明 3000ms 实测 5.52s、
  250ms 实测 5.01s、boot 探针 5.00s；现新增 `DEFAULT_CALL_TIMEOUT_S`，每次轮询
  按剩余预算钳制（只裁剪、绝不跳过）。`replay.timeout_ms` 上界 60000 → 25000
  （对齐客户端 ~25s 取消线）。前台 25s 准入门用估算计费，单步 state_probe
  却可等到工具 30s 硬上限，估算过关的批量仍可长期持锁；`_execute_batch` 新增
  `budget_s` 并在取会话锁前算出 deadline，单步 probe 上限改由准入门同源预算
  推导。新增 `test_w29_w31_budget_clamps.py`（12 条）。
- **会话级模块表统一回收落点，GC 按会话回收**：
  `_REGISTRY_BY_SESSION`/`_SEEDED_BY_SESSION`/`_ZERO_STREAKS` 与
  `_FAKE_TRANSPORTS` 均按会话 id 建键却无删除点，长寿命 server 为每个
  见过的会话永久保留探针/计数表。`gc_idle_sessions` phase 1 把三张表整体
  pop 进局部 dict、phase 2 才逐个关闭，lifespan 取消 GC 任务时未处理的
  transport/observer 随局部 dict 销毁，永久无法重试。现新增
  `_drop_session_side_tables()` 作为唯一回收落点（一次清 cond_filter +
  state_observer + reset_probe_streaks + client_helper），GC phase 1 改快照
  （get 而非 pop）、phase 2 逐会话 kill 成功后加锁 pop。回归锁
  `test_w3_a9_a18_session_state_reclaim.py`（7 条）。

### Security

- **未 await 协程硬门禁**：审查报告实测 10 条
  `RuntimeWarning: coroutine ... was never awaited` 指向
  `tools/batch_step.py`/`core/stepping.py`/`tools/replay.py` 三处生产行；判定
  三处生产代码均正确，缺陷全在测试替身类型（`AsyncMock()` 的子属性与
  `return_value` 仍是 AsyncMock，`bool(<coroutine>)` 恒真导致分支走错、断言
  空转）。刻意不改生产代码（不做「非 dict 状态一律当 False」的防御性强转，
  那会掩掉本类错误）。修正三处测试替身，并在 pyproject 新增两条
  filterwarnings（`error::RuntimeWarning` +
  `error::pytest.PytestUnraisableExceptionWarning`）使警告升级为测试失败。
  新增 `test_w8_unawaited_coroutine_gate.py`（7 条）。

### Fixed（审查收口批）

- **lifespan 清单加载改宽口径 best-effort**：此前只捕 `ManifestError`，非该类型
  的清单损坏（如非 UTF-8 `scripts.yaml`）会穿透 lifespan 并终止启动，连带 37 个
  静态工具全部不可用；现外层兜底 `except Exception` 并给出可操作告警（动态脚本
  工具禁用、静态工具不受影响）。对应 xfail 转正为常规断言 + 告警断言。
- **动态注册对账返回真实 `restart_required`**：`_unregister_exposed_tool` 仅在
  真实删除时返回 True；`restart_required` 由运行期 SDK 注册表与账面集合的对账结果
  推导（此前是常量 False，docstring 承诺的分支在代码里不存在）。
- **错误提示不再指引已退役工具**：`invalid address` 提示改为纯算术换算
  （`ppsspp_addr = ida_addr + (top_base.ppsspp - top_base.ida)`）并指向
  `ppsspp_list_addresses`；测试断言从 `or` 盲区收紧为「必须含新文案 + 不得含
  `convert_address`」；`verify_real_mcp.py`/`record_fixtures.py` 的 36/30 计数删除
  并纳入 `test_readme_claims.py` 守卫。
- **工具面与注释去内部代号**：清除工具 JSON Schema 中的 `🔴-1:` 前缀与
  `force` 描述的硬编码代码段常量（改为「按本会话实际模块表计算」）；src 内 36 处
  emoji 标记与评审批号类注释清理为「行为 + 原因」表述（保留合法技术形如 MIPS
  寄存器名）。新增结构守卫：遍历已注册工具 schema，禁止 `🔴/🟡/🟢`、`review vN`、
  `F-5`/`R2`/`C2.2`/`U7`/`I15`/`P0-2`/`V023` 等内部代号进入工具面。
- **版本三源对齐 + 发布守卫**：`server.json` 两处版本 0.1.6→0.1.7 与
  `pyproject` 对齐；`pypi-publish` 两个 job 新增「`server.json` 顶层与
  `packages[0].version` 必须等于 pyproject 版本」守卫（实测漂移 exit 1）。
- **MCP 配置部署重构**：`check_env.py` 改为逐份校验检出内每一份 `.mcp.json`
  （相对 command 按配置所在目录解析 + 目标必须真实存在 + args 契约），
  `--bootstrap` 为每份配置旁 provision venv（实测一条命令后两份配置全绿），新增
  `--print-config` 输出绝对路径片段供不按配置目录解析的客户端；新增 9 条守卫测试。

### Refactored（安全边界与架构债批）

- **端口就绪校验监听者归属**：以 `_port_owned_by_pid`（netstat/lsof/ss 三态）
  确认监听端口属于本次 PPSSPP PID；外来 owner 拒绝并告警，探针不可用时降级留痕。
- **未认证 debugger 暴露默认 fail-closed**：检出非回环绑定即抛
  `WsConnectFailed`（显式 `PPSSPP_DFX_ALLOW_REMOTE_DEBUGGER=1`）；探针不可用时不抛，SECURITY.md 如实说明。
  （修正：本条原指引另含 `RemoteDebuggerLocal=True`；实测与上游源码证明该键**不控制绑定地址**，
  无法修复暴露——详见 SECURITY.md「PPSSPP debugger bind」。）
- `iso_path` 拒 UNC/SMB 并支持 `PPSSPP_DFX_ISO_ROOT` 包含校验。
- `PortConflict` 判定在锁内、`launcher.stop()` 移出全局锁并透传原异常；
  `sessions.json` 的 `ws_url` 白名单=回环 ∪ 当前配置主机（加载丢弃 +
  fallback 二次拒绝，`PPSSPP_DFX_WS_HOST` 远程主机功能保留）。
- `RATE_LIMIT`/`WS_PORT` 非法值改告警回退；`_load_yaml` 补捕
  `UnicodeDecodeError`（GBK 配置不再逃逸成裸异常）。
- `output_dir` 去副作用（显式 `ensure_output_dir()`，启动创建 + POSIX 0700）；
  `tools/analyze.py` 允许根惰性求值，坏 `PROJECT_ROOT` 在导入期给 `ConfigInvalid`。
- `request_id` 改 token 复位（嵌套派发不再抹掉外层 id），新增
  `RequestIdFilter` 使每条日志自动携带；FORCE OVERRIDE 告警带 request_id 归属。
- 限流桶键改 `(session_id, tool_name)`（跨会话不再互相误伤）、
  未注册/形状异常 fail-closed、剪枝不再每次重建字典；README 明确限流只覆盖协议分发。
- 删除死 `verbose` 帧转储路径与全局 `setLevel` 副作用；输出文件
  0600（POSIX）；通用错误兜底不回显绝对路径（完整详情留日志）。
- **架构债**：契约编译器下沉
  `spec/output_contract.py`、治理注册表下沉 `spec/tool_surface_policy.py`、陈旧值
  状态机下沉 `core/value_staleness.py`（工具层保留 re-export shim）；错误码注册表
  下沉 `spec/error_codes.py` 并删除 5 个零实例化死码，新增「每个注册码须有 producer
  站点（AST 扫 raise/return）或在 `RESERVED_CODES` 注明理由」规则；`resources`
  复用 `resolve_session_id` 且保留钉住文案；`PpssppDebugClient` 抽出
  `scan_service`/`gpu_service`/`stepping_service`（1848→1499 行）；工具层删除 23 处
  重复 try/except，算法下沉 `service/scan_engine.py`、`service/probe_observer.py`、
  `service/observer_lookup.py`、`service/screenshot_service.py`、`core/value_expr.py`；
  `session→tools` 反向依赖消除并由 tripwire 固化（顺带修复 tripwire 因 `_SRC`
  路径错误而**空断言**的问题）；脚本信封单源 `spec/script_envelope.py`、
  `ScriptEntry` 字段校验单源、manifest 显式 status、脚本缓存改内容哈希版本戳、
  静态工具注册改目录扫描（37 个工具不变）、新增工具函数长度门禁（>120 行须有
  `# LONG-TOOL:` 理由注释，冻结 6 个豁免）；`[tool.mypy]` 起步 + CI 非阻断 mypy
  job（已转硬门禁，见下）；**提交 `uv.lock`** 使 `uv lock --check` 成为可证伪的漂移守门（残余收口）。
- **顺带修复**：`batch_step` 前台预算估算器调用 `_resolve_target_probes` 漏传
  `session_id`（TypeError 被吞 → 恒取 floor），现先 seed 再解析、估算恢复真实；
  `ppsspp_replay` 描述中一个游离 `}` 恢复。

### Fixed（mypy 硬门禁收口）

- **CI mypy 转硬门禁**：`src` 存量类型错误 269 → 0（134 个源文件，
  `Success: no issues found`）；`.github/workflows/ci.yml` 移除
  `continue-on-error: true`、job 名改 `mypy (src)`、`ci-ok.needs` 纳入 mypy 并
  增加结果校验——类型回归自此与 ruff/pytest 同级阻断合并。修复覆盖
  ToolAnnotations 字段（152 处）、动态契约当类型注解（39 处）及 arg-type /
  assignment / union-attr / attr-defined 等簇（72 处）；`script.py` 的 `input`
  默认值以 `cast` 保持 inputSchema 不变（`# noqa: B008`），`scan.py` 顺带修复
  `mode='value'` 缺 phase 时误报 `SessionNotFound` 的缺口（附回归测试）。
  对外 wire 全表面（37 个工具的 name/description/inputSchema/outputSchema/
  annotations）与 HEAD 基线逐字节一致。

### Fixed（v4 系统性审查修复批，2026-10-03）

事实源：会话审查报告（v4 编外轮，十警告 / 十九建议），
全部经"可失败测试先行"复现（修复前红）后修复，配套回归锁 152 项新增断言。
全量回归 2407 passed / 42 skipped / 0 failed；ruff/format/mypy(136 文件) 全绿。

- **transport**：子协议协商失败不再残留"已连接但无收包循环"的僵尸 socket
  （`connect()` 先 close 并置 None 再抛，自动重连恢复可达）。
- **context**：`_match_region` 兼容十进制 int wire 形状——region 字段在
  真实会话恒空并附误导 note 的缺陷修复（hex 串形状保留兼容）。
- **watch_value**：短读显式拒绝，不再按声明宽度误解码（u32 观测静默
  退化 u16）。
- **batch cpu_step**：单步超时纳入批次 deadline（对齐 probe 分支），
  消除"预算门放行、单步烧 10s 持锁"的冻结路径。
- **capture**：VRAM 兜底的 557KB 扫描 + 13 万次像素循环 + PNG 编码
  经 `asyncio.to_thread` 卸载，事件循环不再被阻塞约 1s。
- **errors**：路径脱敏提升到 `to_tool_error` 全分支出口（SteppingFailed
  五个 f-string 改用脱敏 msg）；analyze/_common 源头去服务端派生路径
  （allowed roots / output dir 不再出网）；ToolError 直通保留。
- **task_cleanup**：新增 `core/task_cleanup.await_cancelled`——清理循环
  只吞子任务取消、外层取消照常传播（实证排除 `task.cancelled()` 与
  `cancelling()` 两个不可靠判别，最终以 gather 语义落地）。
- **launcher**：强杀后补有界 reap（wait 5s），POSIX 僵尸 / Windows
  ResourceWarning 与端口残留消除。
- **文档**：README.en 补译「性能参考」整节 + 双语章节对称守卫；
  evals README 移除易漂移 per-file 计数；SCOPE.md 工具数钉到基线守卫。
- scan pattern 前台 `start<=0` 校验补齐（四路径
  同一裁决）；func_add size>=1 与 bool 拒绝；replay time_set uint32 边界 +
  version bool 拒绝；scan value 负数/超宽显式报错（initial/narrow 两相）；
  query top_n>=0。
- trace 双 false 显式拒绝；batch step 形状错误统一 STEP_INVALID。
- probe 注册名 strip（僵尸 probe 消除）；diff 混传
  范围拒绝 + span 改名；analyze 开启句柄 fstat 复核上限（TOCTOU）；断点列表
  地址解析鲁棒化（int/0x 串/十进制串）；replay wait 缺 executing 视为未知。
- read_string docstring 对齐实现；
  pause 探针 0.5s；sessions.json 创建即 0600 + tmp 失败清理；GC 杀前复核
  （skip 不入 stopped_ids）；mem_bp 查找器三副本收口 `tools/_memcheck.py`
  + 29 处 Former-docstring 注释清理 + query/replay 豁免；addresses
  mtime 缓存；watch_value 锁语义披露（基线已再生）。
- **工程卫生**：py.typed 入 wheel（PEP 561）；移除声明未用的 pytest-cov；
  .gitignore 补 .venv-test/；pypi-publish build 后记录 sha256 清单留痕；
  CHANGELOG 修复重复 [0.1.6] 标题（09-30 v3 批次并入本节）；README 中英
  环境变量表补录 3 个安全变量。

遗留（架构批次）：组合根反向依赖（tools→server 30 处，registry 下沉
需同步迁移测试 patch 点与 tripwire）、单例 reset_for_tests 缝、
双轨错误翻译残余 5 处内联（行为等同，纯减法待独立批次）、.mcp.json
Windows 专属路径（本地工作配置，不随发布）。

### v3 全仓审查修复批（2026-09-30 批次，随 0.1.7 交付）


#### Added（条件断点求值器「接线」落地）

- **条件断点 MCP 侧求值（接线完成）**：v3 全仓审查实证——本能力此前只有
  模块（`core/cond_filter.py`）、响应字段与本文档的承诺，**生产代码零调用点**：
  `condition` 仍被下发给 IR 模式会静默忽略寄存器条件的 PPSSPP，导致条件断点
  变无条件假命中。本批完成接线：
  - `set / mem_set / update / mem_update` 一律以**无条件**方式布防（不再下发
    `condition`），条件交 `cond_filter` 注册表；`update` 覆盖条件时先回收旧条目，
    `condition=""` 表示显式清空。
  - `action='wait'` 命中时用 `cpu.evaluate` 求值：假 → 自动 `resume` 并计入
    `condition_filtered` / `filtered_hits`，继续等待；真 → 正常返回并携带
    `condition`；**求值失败保守按命中处理**并在 `note` 说明（调试场景不静默丢命中）。
  - **命中风暴熔断**：同一地址 ≥10 次命中且相邻间隔 <1s → 自动撤防（CPU 断点与
    同址 memcheck 双撤）并返回 `storm_break=true` + `note`。
  - 生命周期：`remove / mem_remove / stop_session / idle GC` 均回收过滤器条目。
  - 新增单测 `tests/unit/l4_regression/test_cond_filter_wiring.py`（9 例）与真机
    集成 `tests/integration/test_cond_filter_real.py`（env 门控；断言
    `s1==0x711` 断点在 `s1≠0x711` 期间**不返回命中**）。

#### Fixed

- **错误上下文在真实调用边界失效**：`session_client*` 的 async generator
  此前在 `finally` 中即复位 PID/游戏态 resolver，异常冒泡到工具层
  `to_tool_error` 时已复位 → `CPU_FREEZE_SUSPECTED` 超时判别矩阵恒走保守分支
  （README 承诺的冻结判别在生产路径失效）。改为**仅在正常退出时复位**，失败时
  保留 resolver 至该 task 结束。新增 `test_error_context_scope.py`：with 之外
  可观测断言 + 正常退出防泄漏 + 双会话并发隔离。
- **`ppsspp_watch_value` 预算失控**：`interval_frames` 无上界、内层 sleep 不受
  `duration_frames` 约束（`interval_frames=10**6` 可持会话锁约 4.6 小时）。改为
  拒绝 `interval_frames > duration_frames`，内层按剩余帧收敛。
- **`ppsspp_analyze_log` 截断语义失真**：内部 500 条上限触发时
  `truncated=false`、`total_matches` 失真。`_filter_log_lines` 回传触顶标志 →
  `truncated=true`；描述明确 `total_matches` 的"下限"语义。
- **`ppsspp_query(func_add)` verified 误判**：name-only 调用（协议允许）按
  `address==0` 校验。改为 addr 缺省时按 name 匹配；反例断言防"假通过"。
- **`ppsspp_scan` narrow 短读崩溃**：短 payload 触发未捕获 `struct.error` →
  整批 narrow 退化为 `[INTERNAL]`。合并读与逐点读两分支补长度守卫。
- **调试器暴露告警快路径遗漏**：`_wait_for_port` phase-1 命中即返回，跳过
  `warn_if_debugger_exposed_externally`；现两分支公共出口均告警。
- **`ppsspp_scan` value 初扫热循环**：逐元素 `struct.unpack` 阻塞事件循环
  （实测 1 MiB u16 ≈ 0.168 s）。eq 改走 `bytes.find`、其余用预编译
  `unpack_from`；实测 1 MiB eq **213 ms → 0.51 ms**，全 op 逐元素等价（属性测试）。
- **`ppsspp_scan` 整段读内存**：`_read_segments` 聚合全区间（strings 前台
  峰值可达 256 MiB）→ 改流式 `_iter_segments`，峰值降到单块级。
- **`ppsspp_state_observer` N+1 往返**：每 probe 每 sample 一次单点读
  （50×1400 ≈ 7 万次）→ 同 sample 多探针合并块读 + 失败回退逐点读
  （3 探针×2 sample：6 次 → 2 次）。
- **lint 门禁红（HEAD 实测 5 错 + 8 文件待格式化）**：修 F401/I001/SIM108；
  全仓 `ruff format`（钉版 0.16.6）后 0 错 0 待排；dev 依赖与 CI 同步钉
  `ruff==0.16.6`（消除本地/CI 版本漂移）。
- **`core/error_codes.py` 孤儿模块**：业务异常注册表全仓无人消费 → 改为从
  `errors.py` 的 `ToolError` 子类自动派生（33 类）+ 双向一致性守卫测试。
- **计数与退役名守卫盲区**：`test_readme_claims.py` 清单纳入
  `CONTRIBUTING.md` 与 `evals/README.md`（36→37 工具、21→49 场景卡）；清
  `README.en.md` 的退役工具名并新增 8 个退役名的显式不得出现断言。
- **evals B2 默认路径指向仓外**：`evals/runner.py` 的 monorepo 残留
  `_REPO_ROOT.parents[1]`（fresh clone 必 `RuntimeError`，本机因工作区巧合通过）→
  路径仓内化（`skills/ppsspp-dfx`）+ 存在性与"无父目录引用"守卫测试。
- **bool 当 int**：新增 `require_int_not_bool`，覆盖
  `batch_step.count` / `input.duration+x` / `memory.size` /
  `scan.max_results+chunk_size` 五处。
- `views/_contract.py` 修正指向失效符号的引用（防漂移文档自身漂移）；
  `sessions.json` 写入后 `chmod 0o600`（含 iso 路径/pid/ws_url）。
- **CI 红修复（跨平台，发布前发现）**：`tests/unit/core/test_launcher.py` 的暴露
  告警测试隐含 Windows 专属假设（patch 的是 Windows 绑址探针），在 ubuntu/macos
  上必失败——CI 自 v0.1.6-dev 提交起即为红（本项与前述 lint 门禁红是同一次 CI
  失败的两个原因）。已按平台拆分：3 条 Windows-only 标记 + 3 条 POSIX 对应断言
  （探针不可用→保守告警 / 通配绑定→告警 / 回环→静默），并以 `sys.platform`
  强制探针在本地复核 POSIX 分支 4/4 断言为真。

### Security（v3 审查修复批）

- **evals HTTP bridge 加固**（评估基建，不随 wheel 分发）：此前无鉴权、无
  请求体上限、不校验 Content-Type/Origin（本机任意进程等价获得 MCP 全权；恶意
  网页可用 `text/plain` 简单请求盲触发副作用）。补：body ≤1 MiB（413）、
  `Content-Type: application/json`（415）、loopback `Origin`（403）、
  Bearer token（401，启动打印，`compare_digest` 比较）。
- **manifest 绝对路径逃生口收敛**：绝对 `path` 现在必须显式
  `PPSSPP_DFX_ALLOW_ABS_SCRIPT=1` 才放行（相对路径 containment 不变）；
  `SECURITY.md` 新增「配置可信边界」段；示例清单注释同步。
- **撤除 `IR_ENCODING_DETECTED` / `VERIFY_MISMATCH` 宣称**：两码全仓零 raise
  点，但 README×2 / `skills/**`×3 / 工具描述共六处宣称"读代码段会返回该码"。
  裁决为**撤宣称**（无实机取证的判别式不启用启发式实现）；`errors.py` 保留类并
  注明"待实机取证后再启用"。

### Changed

- **工具描述**（同笔重生成 `tests/unit/l2_mcp_contract/tool_surface_baseline.json`，
  37 工具，`total_description_chars` 25047→25587）：`ppsspp_breakpoint` 条件语义
  改为"MCP 侧求值"并补 wait 返回字段文档；`ppsspp_read_memory` 撤 IR 措辞；
  `ppsspp_analyze_log` 明确 truncated/total_matches 语义。
- **文档**：README 中英「独立部署快速开始」区分**源码检出**（`cp examples/…`）与
  **PyPI 安装**（wheel 不含 `examples/`，给 raw.githubusercontent 链接）；
  配置模板链接同步。

### 回归（v3 审查修复批）

- `pytest tests -q`：**1669 passed / 37 skipped / 1 xfailed / 0 failed**（修复前
  基线 1593/36/1；新增 +76 用例）。
- `ruff check .` + `ruff format --check .`（钉版 0.16.6）：**0 错 / 全量已格式化**
  （排除他人工作流未跟踪文件 `evals/opencode_collect.py`）。
- 真机（PPSSPP v1.20.4 dev 构建 + `cn.iso`）：条件断点过滤验收 —— 见
  `tests/integration/test_cond_filter_real.py`。

### Fixed（文档计数漂移 + 计数守卫）

- **静态工具数漂移**：README.md / README.en.md（feature 条目、协议面表格）与
  `skills/ppsspp-dfx/references/architecture.md`（L3 架构图）仍写 36，实际 37
  （`ppsspp_watch_value` 加入后未刷新）。四处全部对齐实测。
- **场景卡数漂移**：README 两份仍写 21 张，实际 49 张（real tier 扩至 30 后
  未刷新）。两份全部对齐。
- **新增计数守卫** `tests/unit/l2_mcp_contract/test_readme_claims.py`（7 例）：
  - 工具数 → 锚定 `tool_surface_baseline.json["tool_count"]`，覆盖三份文档；
  - 场景卡数 → 锚定 `evals/scenarios.yaml`；
  - 测试套件规模 → 以 AST 统计 `test_*` 函数数为下界（与文档「不含参数化
    展开」口径一致），既抓陈旧也不因新增用例而误报。
  覆盖范围写成**显式文件列表**而非 glob——v0.1.6 漂移清扫漏掉 `skills/`
  子树正是因为按「顶层 .md」的目录直觉扫描。

### Security（发布脱敏：真实游戏标识出库 + 两处空匹配缺陷修复）

- **真实游戏标识移出仓库**：R1-REAL-BOOT 的身份门禁值原为真实光盘序号
  与游戏名，改为 `{{PPSSPP_DFX_EVAL_GAME_*}}` 环境变量令牌，由
  `gates.resolve_value` 在评分时解析；未设/空白的令牌**跳过**而非空串匹配。
- **修复空匹配虚假通过（既有缺陷）**：`_match_in_answer` 对归一化后为空
  的期望值（纯标点或日文假名，如纯日文假名组成的期望值）判定为
  `'' in <任意答案>` 恒真——该门禁此前对**任何**回答都通过。现归一化后
  为空直接判否。
- **修复 answer_contains 空集虚假通过**：`mode=any` 且所有值均不可解析时，
  `any([])` 返回 False 但仍可能被上层误读；现显式返回失败并标注
  `no resolvable values`，与「无门禁不得算通过」的语义对齐。

### Added（方案 B——代码审查后追加）

- **`evals/bridge.py`（子代理采集 HTTP bridge）**: stdlib `http.server` 长驻
  mcp stdio `ClientSession`，暴露 `GET /tools` / `POST /call` / `POST /seed`
  为本地 REST（127.0.0.1 only），供外部 agent 子代理用 curl 驱动采集，
  绕开 LLM API 依赖。配套 `test_bridge.py` 5 用例。
- **`ppsspp_watch_value`（新工具，36→37）**: 值变化轮询观察——纯读、
  零暂停；热读地址"谁/何时改了值"需求的观察点替代（命中风暴的
  结构性消除）；变化记录含 frame/相对时间/old/new，上限 64 条。
- **条件求值器（方案 B）**: `breakpoint set/update` 带
  condition 时不再下发给 PPSSPP（IR 模式寄存器条件被静默忽略——实测
  s1==0x711 恒假仍触发），改由 MCP 侧命中时用 `cpu.evaluate` 求值：
  假 → 自动 resume 并计入 `filtered_hits`；真 → 保持暂停并在响应携带
  `condition`/`condition_filtered`。
- **wait 风暴熔断**: ≥10 次命中间隔 <1s 自动撤除断点并返回
  `storm_break=true` + note（命中风暴的保护性响应）。

### Fixed（实机盲测回归修复 + 接口契约面）

- **`first_tool` 门禁扩大前导豁免**：`_PREAMBLE_TOOLS` 增补
  `ppsspp_query`/`ppsspp_memory_map`/`ppsspp_context`/`ppsspp_gpu_stats`
  （场景相关前导探查），修复 R9/R10/R24 因模型先查寄存器/内存布局/GPU
  状态再调 expected 工具被误判 fail（runs-20260929 pass 78.3%→83.7%）。
- **会话锁同任务可重入**：`batch_step` 全程持锁期间内嵌 `screenshot`
  步骤的嵌套获取不再自死锁（此前 100% SESSION_BUSY）。跨任务互斥语义
  不变（仍一条工具调用独占会话，等锁超 5s 报 SESSION_BUSY）。
- **伴生项**：修复 `batch_step` screenshot 分支对 `screenshot()`
  返回值的过期解包——按 `structured_content` 元数据记账（图像已由
  工具自动落盘 `file_path`）。
- **`restored` 字段误标修复**：本进程新建会话在版本指纹回写时
  不再被 `_load_sessions` 误标为 `restored=1`（冷启动语义恢复）。
- **MCP 防御**：`query(func_add)` 暴露 `size` 参数（默认显式 4，
  规避 PPSSPP ≤ v1.20.4-1845 省略 size 下溢为 0 的上游缺陷），ack 后
  回读符号表校验并返回 `verified` 字段。
- **接口契约面**：9 项描述-实现漂移按实测修正（`breakpoint`
  mem_remove 按地址匹配、`write_register` r5→a1 归一与超界拒绝、
  `step` 单步不可用声明、`query` threads/modules 免暂停、`session`
  restored 字段入契约、`replay` flush/save 消费缓冲、`input` duration
  上限 18000）；`server.py` 移除无注册效果的 "smoke" 模块条目；
  manifest 补 `device_walkthrough` load 阶段副作用说明。
  `tool_surface_baseline.json` 按规程再生成。

回归：unit 1298 passed（含新增锁语义 5 测试）/ evals gates 19 passed /
真机活体验证（批内嵌 screenshot 3/3 成功、新会话 restored=0、
func_add verified=true）。

<!-- merged the earlier batch (below) into the single Unreleased section -->

### Added

- **cpu_step 执行体**：`batch_step` 的 `cpu_step` 步骤类型从
  「校验通过但无执行分支的假成功」变为真实单步——`with_stepping` 自动
  暂停/恢复，mode 映射 `step_into/over/out`，count 循环逐步并带陈旧广播
  过滤；停滞时步骤失败并上报 `confirmed N/M` 部分进度。真机活体：暂停态
  单步 3 次 PC 真实推进。

### Changed

- **BREAKING（嵌套地址格式统一）**：以下 4 个接口响应中嵌套列表的
  `address` 字段类型从 `int`（十进制）变为 `str`（十六进制
  `0x{VALUE:08X}` 格式），与顶层 `address` 字段格式统一。消费方（LLM
  Agent）需更新解析逻辑，按字符串而非整数处理：
  - `ppsspp_disassemble` 响应 `instructions[].address`
  - `ppsspp_search_disasm` 响应 `results[].address`
  - `ppsspp_scan`（pattern 模式）响应 `value[].address`
  - `ppsspp_search_memory_info` 响应 `regions[].address`
- **analyze_log**：新增 `filter_mode`（`any`=旧 OR 语义
  默认；`all`=严重级别 AND filter 收窄）与 `limit`（响应新增
  `total_matches`/`truncated`，长日志不再整包返回）；log_path 白名单
  描述修正为实际口径（整个 .ppsspp-dfx 树）。
- **query(func_scan)**：客户端按请求窗口 [address,
  address+64KB) 过滤 PPSSPP 返回的全表，响应附 `filtered_to` /
  `total_before_filter`。
- **health**：带 session_id 时 structuredContent 现包含
  `session_checks` / `overall_session_status`（此前仅 text 通道携带）。
- **list_scripts**：非法 category 由静默空列表改为
  ARGS_INVALID 并列出合法值（对齐 list_addresses 策略）。
- **scan**：pattern 模式响应同步填充 `count`（此前恒 0，误读为
  无命中）。
- **disassemble**：count=0 回退文档默认 10（原样返回空被读作
  「未映射内存」）；全占位 `-` 结果附 note 说明地址疑似未映射。
- **run_script**：input 未知字段由静默忽略改为
  SCRIPT_CONTRACT_ERROR（列出未知键与合法键）。
- **evals 场景卡**：CTL-01/L1-02 弃用已退役 `ppsspp_get_pc`
  的 prompt/白名单（等价工具 + health/session 纳入白名单）；
  R1-REAL-BOOT 的 'Game' 字面量 oracle 改宽口径 any-of 游戏身份 token；
  L3-03 first_tool 白名单放宽。实跑验证：CTL-01 / L3-03 / R1 全部转 PASS。

回归：unit 1302 passed / evals gates 19 passed。

### Fixed（2026-09-20 遗留清理批：扩卡）

- 补 `cpu.getReg.json` fixture（v0.1.6 后 `query(register pc)` 走 `cpu.getReg` 事件，原 `cpu.status.json` fixture 未跟上事件路由变更），恢复 CTL-01/L1-02 两处 `answer_contains` 门禁（`from_fixture` + `transform: hex` 动态解析 PC 值，不硬编码漂移）。
- 确认已修（docstring 诚实化路线——`session.wait_ready` elapsed_s 语义、`breakpoint.stats` fixed ~30s window 声明），CHANGELOG 前批未单列。
- **is_error 双通道归属**：定论为非缺陷——SDK 错误时单通道（text only，by design，见 `errors.py:43-48`），成功时双通道由 `_contract.py` 派生机制保证结构一致。
- **real 卡扩充第一批**：新增 R3-R12 共 10 张 real 卡（GETPC/REGS/MEMAP/MEMREAD/DISASM/BPSET/STEP/SCAN/SCRIPT/HEALTH），real 卡 2→12，目标 ≥30 待续。

### Fixed（2026-09-22 采集修复：evals first_tool 门禁）

- **real 卡扩充第二批**：新增 R13-R30 共 18 张 real 卡（BPWAIT/BPSTATS/BPTRACE/WATCH/STATEOBS/FRAME/DUMP/GPUSTATS/PRESS/HOLD/ANALOG/WAITFRAMES/LISTSCRIPTS/ANALYZE/LISTADDR/MEMDIFF/MEMINFO/EVALUATE），real 卡 12→30，达成 ≥30 目标。
- **补 `expected_first_tools`**：该批 real 卡声明了 `first_tool` 门禁却未定义 `expected_first_tools`，门禁比对空列表恒判 FAIL——28 张 real 卡从设计上不可能通过。按各卡 prompt 意图补齐期望工具。
- **`_gate_first_tool` 前置调用豁免**：模型先探活（`ppsspp_health`）或先取 session id（`ppsspp_session`）再执行任务属合理行为，不再计为"首个任务工具"；仅当该工具本身是声明的首工具时（L1-06 health / R1 session）保留首工具语义。
- **R7/R8 允许 `ppsspp_query` 前置**：`ppsspp_disassemble` / `breakpoint set` 的 address 为必填，"反汇编当前 PC"/"在当前 PC 设断点"是隐含两阶段任务，模型须先 `query(register pc)` 取当前 PC。
- 回归：evals gates 24 passed；对既有 runs-20260922.jsonl（187 格）按新门禁重新评分，通过 112→163（59.9%→87.2%），剩余 fail 均为能力/真机/参数类。

## [0.1.6] - 2026-09-18

### Changed（表面重构 — Glama AI 可用性评审整改：工具数 41 → 36）

净变化：8 项合并/移除 + 3 项新工具（`scan` / `diff_memory` / `context`），
41 → 36。全部能力有等价替代路径，无删减。

| v0.1.5 及之前 | v0.1.6 | 说明 |
|---|---|---|
| `ppsspp_session_list` | `ppsspp_session(action="list")` | 会话清单并入会话分发器；返回 `{sessions, count}` 不变 |
| `ppsspp_batch_list` | `ppsspp_batch_status(batch_id 省略)` | 盘点模式并入状态查询；返回 `{jobs, retention_jobs}` 不变 |
| `ppsspp_dump_texture` / `ppsspp_dump_clut` | `ppsspp_dump(kind="texture"/"clut", level?)` | 二合一；`kind="clut"` 时 `level` 必须为 0；元数据统一含 `level` 字段（clut 恒为 0） |
| `ppsspp_convert_address` | （非工具化） | 纯算术：`ppsspp_addr = ida_addr + (top_base.ppsspp - top_base.ida)`；公式移入 `ppsspp_list_addresses` 描述与技能书 `scripts/addr_convert.py` |
| `ppsspp_memory_info_search` | `ppsspp_search_memory_info` | 改名（verb_noun 约定），参数与返回不变 |
| `ppsspp_wait_breakpoint` | `ppsspp_breakpoint(action="wait")` | 严格等待（lock-free，断点保持布防），经观察者专用 step 确认通道 |
| `ppsspp_trace_memory_access` | `ppsspp_breakpoint(action="trace")` | 命中-快照-放行（必清+恢复）；仅编排内存断点（默认读访问），执行断点用 `set` + `wait` 组合 |
| `ppsspp_smoke_test` | `ppsspp_health(session_id=...)` | 四点会话电池并入健康探针：追加 `session_checks` 与 `overall_session_status`；无参形态保持零接触服务器探针语义 |
| `ppsspp_get_pc` | `ppsspp_query(action="register", name="pc", safe=?)` | `safe=true` 走 with_stepping 暂停一致性读取（≡ 原 get_pc，trust=high）；`safe=false` 裸读（trust=low，零开销热路径轮询） |

- `ppsspp_read_memory` 瘦身：`action="scan"` 迁出至新工具 `ppsspp_scan`，
  `read_memory` 回归纯读语义。
- `ppsspp_step` 瘦身：`into/over/out` 移入 `ppsspp_batch_step` 的 `cpu_step`
  步骤类型（count 1..1000 指令级批量推进）；`step` 保留
  `pause/resume/reset/run_until/next_hle` 运行控制语义。
- `diff_memory` / `scan` 的会话解析统一为**先解析会话、后校验参数**的优先序
  （两工具在"参数错 + 无会话"并发场景抛出一致的会话类错误）。
- 命名成文约定落盘 CONTRIBUTING（原子工具 verb_noun / 分发器 noun(action=) /
  族内自洽）。
- 重叠簇交叉引用：`query`/`frame_snapshot`/`state_observer`/`breakpoint`/
  `step`/`batch_step` 等描述新增 ROUTING 节（何时用我、何时改用哪个相邻工具）。
- 真机验证结论（ISO 实测）：MCP 全链路 WS 往返中位 16.7ms（n=50，经
  Inspector 客户端→服务器→PPSSPP）；WS 客户端层轻量往返 p50 0.21ms（n=60，
  localhost，见 README 性能参考）；MCP 通道回传 64KB 字节列表 ~204ms/块
  （全频段扫描必须后台化且工具内联匹配）；cpu.stepping 广播不含断点标识
  （命中归因客户端 pc↔布防表）；PPSSPP 原生 condition/log 断点字段可用。

### Added

- `ppsspp_scan`：三模式统一扫描器——`mode="pattern"`（字节模式搜索，从
  read_memory 迁入）/ `mode="value"`（Cheat-Engine 式值扫描+窄化会话，
  initial→narrow→list→drop，width u8/u16/u32，op eq/ne/lt/gt）/
  `mode="strings"`（charset 感知字符串采集：shift_jis/utf8/ascii +
  min_len + CJK 占比质量过滤）。`background=true` 提交 detached 后台作业
  （复用 batch_jobs 注册表），value initial 上限抬升至 32 MiB。
- `ppsspp_diff_memory`：内存快照差分——snapshot（64KB 分块读，单快照
  8 MiB，注册表 4 FIFO）→ compare（变更字节清单，内联 256 + truncated）→
  drop/list；纯客户端编排零新 WS 事件；不可读区段式跳过。注册表为进程级
  共享：并行会话共用同一容量与 FIFO 序。
- `ppsspp_context`：崩溃归因上下文包——known_functions IDA 偏移换算身份 +
  反汇编窗 + 可选回溯（with_stepping）。
- `ppsspp_batch_step` 新增 `cpu_step` 步骤类型：
  `{type:"cpu_step", mode:"into"|"over"|"out", count:1..1000}`。
- `ppsspp_breakpoint` 新增 `action="stats"`：窗口内命中频率统计（按 pc
  聚合）+ 探针值变化采样。
- 工具面基线锁定（`tool_surface_baseline.json` + L2 契约测试）：36 工具的
  描述与 schema 逐字节锁定，`scripts/dump_tool_surface.py` 再生成。
- README「性能参考（本机实测）」与 `docs/ppsspp-build.md`「行为契约的
  验证基线」（PPSSPP v1.20.4-605 实测口径）。
- pytest 进入 `[dependency-groups]` dev（uv 默认安装），杜绝
  `uv run pytest` 静默回落系统 PATH 旧版 pytest 的假失败。

### Fixed

- `ppsspp_scan` 后台提交返回键统一为 `batch_id`（此前 `job_id` 与
  `ppsspp_batch_status(batch_id=...)` 的参数名互相矛盾）。
- `ppsspp_breakpoint(action='trace')` 描述更正：仅编排内存断点
  （此前声称 "exec via address only"，实现中并无 exec 路径）。
- `ppsspp_health(session_id=...)` 的会话电池自包含化：`run_smoke_checks`
  恒返回元组，坏会话降级为 failed 项而非 INTERNAL 崩溃。
- e2e/inspector 测试同步当前工具面（移除对已删 `ppsspp_smoke_test` /
  `ppsspp_session_list` 的调用；期望集合改为从基线 JSON 派生，杜绝再次
  漂移）。

## [0.1.5] - 2026-09-18

### Added

- **Python 3.13 支持**：全仓 17 处 PEP 758 无括号多异常捕获
  （`except A, B:` → `except (A, B):`，零行为差异）替换为括号形式，
  `requires-python` 放宽为 `>=3.13`，CI 测试矩阵扩展为
  3.13 + 3.14 × 三平台。mcp SDK 官方支持 3.10+，放宽采用门槛。

## [0.1.4] - 2026-09-18

### Added

- 英文 README（`README.md` 转为英文主门面，中文迁至 `README.zh-CN.md`，
  双语切换器）——面向 MCP 全球受众。
  （勘误：v0.1.5 起实际布局为 `README.md`（中文主门面）+
  `README.en.md`（英文辅文档）；本条所述文件名与现状不符。）
- MCP Registry 元数据：`server.json`（官方 Registry 发布格式）与 README
  的 `mcp-name` 所有权标记。

## [0.1.3] - 2026-09-18

### Changed

- README 增加徽章行（PyPI / CI / Python / License）。
- 全库 ruff 清债至零并纳入 CI 执法：safe-fix 482 处（导入排序/类型现代化/
  未用导入）、`ruff format` 全库 214 文件、残量 SIM105/SIM117/F841 等手工
  清扫；`.github/workflows/ci.yml` 新增 lint job（`ruff check` +
  `ruff format --check`）。
- 工具描述空白规范化（协议面唯一变化）：`ruff format` 剥离 docstring
  空行尾随空白，仅 `ppsspp_assemble` 描述受影响（490→478 字符，纯空白级），
  基线经 `scripts/dump_tool_surface.py` 同步再生成；41 工具 inputSchema/
  outputSchema 逐字节不变。
- 错误码分类学补全：输入参数校验统一为 `ARGS_INVALID`（全仓 83 处从
  `INTERNAL` 迁移；`.ppr` 写盘失败等真实内部错误保留 `INTERNAL`）。
  技能文档 `error-codes.md` 同步新增 `ARGS_INVALID`/`STEP_INVALID` 条目，
  `INTERNAL` 行改写为仅限服务器自身故障。
- `memory_protection` 的 top.prx 受保护段基址改从 `addresses.yaml` 的
  `top_base.ppsspp` 读取（缺失时回退本项目默认值），落实"无硬编码项目
  地址"原则；内核段保持 PSP 通用常量。
- `MAX_WAIT_FRAMES` / `MAX_PRESS_DURATION_FRAMES` 下移至
  `core/primitives.py`（单一真相源），`models/batch_step.py` 的字段描述
  改为插值引用，不再手抄数值。
- `ppsspp_batch_step` 的 `PressStep.button` 在 inputSchema 中以 25 项枚举
  下发（此前为裸 string + 文字描述，白名单约束仅存在于运行时）；按钮词汇表
  规范定义上收至 `models/input.py`（`PPSSPP_ALL_BUTTONS`），input 工具与
  批量步骤共用单一真相源。

### Fixed

- `batch_step` 的 wait step 补上帧上限执行（此前仅独立 `wait_frames`
  工具执行该 cap，批量路径的文档承诺未兑现）。
- `_validate_step` 的 step 结构校验错误码由 `INTERNAL` 更正为
  `STEP_INVALID`（输入校验失败不是服务器内部错误；新错误类继承
  `ToolError`，既有客户端分类不受影响）。

## [0.1.2] - 2026-09-17

### Fixed

- MCP 握手的 serverInfo 版本改为从包元数据读取（`importlib.metadata`），
  消除 `__init__.py` 中与 pyproject 脱节的硬编码副本——0.1.1 曾在协议层
  自报 0.1.0。
- README：Windows 的 venv 创建命令更正为 `py -3.14`
  （`python3.14` 别名在 Windows 上通常不存在）。

## [0.1.1] - 2026-09-17

### Added

- **配置模板三件套**（`examples/`）：`project.yaml` / `addresses.yaml` /
  `scripts.manifest.yaml` 可拷贝模板，PLACEHOLDER 标注 + 逐字段语义注释，
  独立部署时 `mkdir -p .ppsspp-dfx/config && cp examples/*.yaml
  .ppsspp-dfx/config/` 即可起步。
- **预置 `.mcp.json`**：独立 checkout 的开箱即用 MCP 客户端注册。
- `check_env.py --check` 对 `scripts.manifest.yaml` 缺失显式告警（非阻断）
  并给出 examples 修复路径——此前缺失会导致 `ppsspp_script_*` 工具静默消失。
- **README 新增「故障排查速查表」与「已知限制」**：错误码体系的症状级
  索引 + 协议面边界的诚实汇总。
- **`docs/SCOPE.md`**：PPSSPP WS 事件 → 工具映射的人读版 + 刻意未工具化
  事件清单（机器可读真相源仍为 `ws_contract.py`）。
- 随包 `.github/workflows/`：三平台测试矩阵（CI）与两段式发布工作流——
  push tag `v*` 构建并试发布（内部验证），确认后发布 GitHub Release 正式
  上架 PyPI；两阶段均为 trusted publishing（OIDC），内置 tag/版本一致性 Guard。

### Changed

- README 定位为中文社区发布；代码注释与工具 docstring 保持英文
  （协议面被工具面基线锁定）。
- README 参考 deepseek-harness 的官方 README 结构重构为发布版形态：
  PyPI 安装为主路径（含安装后的 `.mcp.json` 与命令示例），新增项目状态、
  致谢与引用节。

## [0.1.0] - 2026-09-16

### Added

- 首个开源发布版本：41 个静态 MCP 工具（全结构化 inputSchema/outputSchema）、
  动态诊断脚本工具（manifest 驱动）、多会话管理与楔死自愈、后台批处理、
  原生 replay 录制回放、GPU 缓冲/纹素/CLUT 转储、HLE 内省、盲测评估体系
  （21 场景卡 + 确定性门禁）。
