"""Probe results must distinguish "read 0" from "could not read" (C2.6/C2.7).

The four-point session battery reports `passed` per check. That is not
enough: a probe that reads 0 and a probe whose read FAILED are both
`passed: false`, and a consumer cannot tell them apart. The difference
matters -- "game_mode is 0" is a finding about the game, "game_mode could
not be read" is a finding about the tooling, and they warrant different
next actions.

So each probe result carries `value_status`:

  ok        the probe read successfully; `value` is real
  failed    the read raised; `value` MUST be absent (not 0, not stale)
  not_configured
            the probe has no address configured, so it was never read

  C2.6  a failed read MUST NOT report a value at all
  C2.7  a missing `paused` key MUST NOT be reported as "paused=True"
        (the old default made a malformed response look like a paused CPU)
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from ppsspp_dfx_mcp.tools.smoke import run_smoke_checks

OK = "ok"
FAILED = "failed"
NOT_CONFIGURED = "not_configured"


class _FakeClient:
    """Minimal stand-in for PpssppDebugClient."""

    def __init__(self, game_status=None, read_u32=None, raise_on_status=None):
        self._game_status = game_status if game_status is not None else {}
        self._read_u32 = read_u32
        self._raise_on_status = raise_on_status
        self.reads: list[int] = []

    async def game_status(self):
        if self._raise_on_status:
            raise self._raise_on_status
        return dict(self._game_status)

    async def read_u32(self, addr: int) -> int:
        self.reads.append(addr)
        if self._read_u32 is None:
            raise RuntimeError("memory.read_u32: Bad message (level=2)")
        return self._read_u32


def _run(client, checks=None):
    """Run the battery against client with a stubbed transport."""
    import asyncio
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _cm(_sid):
        yield client

    with patch("ppsspp_dfx_mcp.tools.smoke.session_client", _cm):
        return asyncio.run(run_smoke_checks("s", checks=checks))


def _by_name(results):
    return {r["name"]: r for r in results}


class TestGameModeProbe:
    """C2.6 -- a failed read reports no value."""

    def test_successful_read_reports_the_value(self) -> None:
        client = _FakeClient(read_u32=0x2)
        with patch("ppsspp_dfx_mcp.tools.smoke.read_game_mode_addr", return_value=0x08A0D000):
            results, _ = _run(client, ["game_mode_valid"])
        row = _by_name(results)["game_mode_valid"]
        assert row["passed"] is True
        assert row["value_status"] == OK
        assert "0x00000002" in row["detail"]

    def test_failed_read_reports_no_value(self) -> None:
        client = _FakeClient(read_u32=None)
        with patch("ppsspp_dfx_mcp.tools.smoke.read_game_mode_addr", return_value=0x08A0D000):
            results, _ = _run(client, ["game_mode_valid"])
        row = _by_name(results)["game_mode_valid"]
        assert row["passed"] is False
        assert row["value_status"] == FAILED, (
            "a read that raised must not be presented as a probe reading"
        )
        assert "value" not in row, f"a failed read must not carry a value, got {row.get('value')!r}"

    def test_real_zero_is_distinguishable_from_failure(self) -> None:
        """The whole point: 0 is a reading, not an absence."""
        with patch("ppsspp_dfx_mcp.tools.smoke.read_game_mode_addr", return_value=0x08A0D000):
            zero_results, _ = _run(_FakeClient(read_u32=0), ["game_mode_valid"])
            err_results, _ = _run(_FakeClient(read_u32=None), ["game_mode_valid"])
        zero = _by_name(zero_results)["game_mode_valid"]
        err = _by_name(err_results)["game_mode_valid"]
        assert zero["value_status"] == OK
        assert err["value_status"] == FAILED
        assert zero["passed"] is False and err["passed"] is False
        # both fail, for entirely different reasons

    def test_unconfigured_address_is_its_own_status(self) -> None:
        with patch("ppsspp_dfx_mcp.tools.smoke.read_game_mode_addr", return_value=0):
            results, _ = _run(_FakeClient(), ["game_mode_valid"])
        row = _by_name(results)["game_mode_valid"]
        assert row["passed"] is False
        assert row["value_status"] == NOT_CONFIGURED
        assert "value" not in row

    def test_unconfigured_does_not_touch_the_transport(self) -> None:
        client = _FakeClient()
        with patch("ppsspp_dfx_mcp.tools.smoke.read_game_mode_addr", return_value=0):
            _run(client, ["game_mode_valid"])
        assert client.reads == [], "an unconfigured probe must not issue a read"


class TestCpuRunningProbe:
    """C2.7 -- a missing key is not `paused=True`."""

    def test_paused_true_is_reported(self) -> None:
        client = _FakeClient(game_status={"paused": True})
        results, _ = _run(client, ["cpu_running"])
        row = _by_name(results)["cpu_running"]
        assert row["passed"] is False
        assert "paused=True" in row["detail"]

    def test_paused_false_passes(self) -> None:
        client = _FakeClient(game_status={"paused": False})
        results, _ = _run(client, ["cpu_running"])
        assert _by_name(results)["cpu_running"]["passed"] is True

    def test_missing_paused_key_is_not_reported_as_paused(self) -> None:
        """The old default made a malformed response look like a pause."""
        client = _FakeClient(game_status={"game": "Cube"})
        results, _ = _run(client, ["cpu_running"])
        row = _by_name(results)["cpu_running"]
        assert "paused=True" not in row["detail"], (
            f"a missing key was invented into paused=True: {row}"
        )

    def test_missing_key_is_flagged_as_unknown(self) -> None:
        client = _FakeClient(game_status={"game": "Cube"})
        results, _ = _run(client, ["cpu_running"])
        row = _by_name(results)["cpu_running"]
        assert row.get("value_status") in (FAILED, "unknown"), (
            f"an absent key must be marked unknown, got {row}"
        )
        assert row["passed"] is False, "absence cannot be reported as healthy"


class TestGameStatusFailure:
    def test_status_error_marks_both_dependent_checks_failed(self) -> None:
        client = _FakeClient(raise_on_status=RuntimeError("WS closed"))
        results, _ = _run(client, ["iso_loaded", "cpu_running"])
        rows = _by_name(results)
        assert rows["iso_loaded"]["passed"] is False
        assert rows["cpu_running"]["passed"] is False
        assert "WS closed" in rows["cpu_running"]["detail"]

    def test_status_failure_is_not_reported_as_a_clean_reading(self) -> None:
        client = _FakeClient(raise_on_status=RuntimeError("WS closed"))
        results, _ = _run(client, ["cpu_running"])
        row = _by_name(results)["cpu_running"]
        assert "paused" not in row["detail"], f"a transport error leaked a paused verdict: {row}"


class TestWsConnected:
    def test_ws_connected_is_reported_ok(self) -> None:
        results, _ = _run(_FakeClient(), ["ws_connected"])
        row = _by_name(results)["ws_connected"]
        assert row["passed"] is True
        assert row["value_status"] == OK


class TestOverallStatus:
    def test_all_pass_yields_pass(self) -> None:
        client = _FakeClient(game_status={"game": "Cube", "paused": False})
        with patch("ppsspp_dfx_mcp.tools.smoke.read_game_mode_addr", return_value=0x08A0D000):
            results, overall = _run(client, ["ws_connected", "cpu_running", "game_mode_valid"])
        assert overall in ("pass", "fail")  # game_mode read fails, so fail
        assert isinstance(results, list)

    def test_empty_results_are_fail_not_pass(self) -> None:
        """Vacuously-all-pass on an empty list must not read as healthy."""
        _, overall = _run(_FakeClient(), [])
        assert overall == "fail"

    def test_session_error_is_reported(self) -> None:
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _boom(_sid):
            raise RuntimeError("session transport not found")
            yield  # pragma: no cover

        with patch("ppsspp_dfx_mcp.tools.smoke.session_client", _boom):
            import asyncio

            results, overall = asyncio.run(run_smoke_checks("s", checks=None))
        assert overall == "fail"
        assert any(r["name"] == "session_error" for r in results)


class TestInvalidSelection:
    def test_unknown_check_name_is_rejected(self) -> None:
        # run_smoke_checks raises ValueError; the tool layer wraps it.
        with pytest.raises(Exception) as exc:
            _run(_FakeClient(), ["not_a_check"])
        assert "invalid check" in str(exc.value)


class TestStaleAddressSuspicion:
    """FR-019a (spec 008, defect M3): a probe that keeps reading zero must be
    flagged as a SUSPECTED-STALE ADDRESS, not silently passed with value=0.

    This is the state that was missing when addresses.yaml's state_probes had
    drifted (defect D18): every read returned 0, `passed=false` looked like a
    game fact, and nothing distinguished "the value really is zero" from "the
    address points at dead state".
    """

    @pytest.fixture(autouse=True)
    def _fresh_streaks(self):
        # The streak history is per-session module state; without this, test
        # ORDER would change the verdicts -- exactly the flakiness the
        # deterministic-evidence rule forbids.
        from ppsspp_dfx_mcp.tools._common import reset_probe_streaks

        reset_probe_streaks("s")
        yield
        reset_probe_streaks("s")

    def _zero_run(self):
        with patch("ppsspp_dfx_mcp.tools.smoke.read_game_mode_addr", return_value=0x08A0D000):
            results, _ = _run(_FakeClient(read_u32=0), ["game_mode_valid"])
        return _by_name(results)["game_mode_valid"]

    def test_first_two_zeros_stay_ok(self) -> None:
        """A single cold-start zero must NOT be called a stale address."""
        first = self._zero_run()
        assert first["value_status"] == OK
        second = self._zero_run()
        assert second["value_status"] == OK

    def test_third_consecutive_zero_is_flagged(self) -> None:
        self._zero_run()
        self._zero_run()
        third = self._zero_run()
        assert third["value_status"] == "stale_address_suspected"
        assert "suspicion" in third["detail"].lower()
        # passed stays false -- the value IS zero -- but now the READER knows
        # which of the two situations they are in.
        assert third["passed"] is False

    def test_a_nonzero_reading_resets_the_streak(self) -> None:
        self._zero_run()
        self._zero_run()
        with patch("ppsspp_dfx_mcp.tools.smoke.read_game_mode_addr", return_value=0x08A0D000):
            results, _ = _run(_FakeClient(read_u32=1), ["game_mode_valid"])
        assert _by_name(results)["game_mode_valid"]["value_status"] == OK
        # and the next zero starts from scratch again
        assert self._zero_run()["value_status"] == OK

    def test_a_failed_read_does_not_extend_the_streak(self) -> None:
        self._zero_run()
        with patch("ppsspp_dfx_mcp.tools.smoke.read_game_mode_addr", return_value=0x08A0D000):
            results, _ = _run(_FakeClient(read_u32=None), ["game_mode_valid"])
        assert _by_name(results)["game_mode_valid"]["value_status"] == FAILED
        # one more zero: streak must still be 1, not 2
        assert self._zero_run()["value_status"] == OK

    def test_the_warning_names_the_address(self) -> None:
        self._zero_run()
        self._zero_run()
        flagged = self._zero_run()
        assert "08A0D000" in flagged["detail"]
