"""health must not report overall success while the session battery failed.

C1.6 / FR-028 / FR-029: `status` is the field a monitoring caller reads
first. Today the session battery's verdict lands in
`overall_session_status`, while the top-level `status` only reflects the
sessions.json probe -- so a caller that reads `status` sees "ok" on a
session that the very same response says has failed. That is the silent
failure shape this feature set out to remove: the headline says fine while
the detail says broken.

  C1.6a  session battery failing  -> top-level status is NOT "ok"
  C1.6b  session battery passing  -> top-level status unaffected
  C1.6c  the failure reason is summarised at the top level, not only in the
         per-check list (FR-029)
  C1.6d  no session_id (server-only probe) -> status untouched
"""

from __future__ import annotations

from unittest.mock import patch


def _run(session_id: str | None, checks_param: list[dict], overall: str):
    """Run health() with a clean sessions probe and a stubbed battery.

    The sessions-file probe must be neutralised explicitly: it touches the
    real file, and a leftover or corrupt one would degrade status for
    reasons unrelated to what these tests are about.
    """
    import asyncio

    from ppsspp_dfx_mcp.tools.introspect import health

    async def _fake_checks(_sid, checks=None):  # noqa: ARG001
        return list(checks_param), overall

    async def _one_session():
        return [object()]

    async def _clean_file():
        return None

    with (
        patch("ppsspp_dfx_mcp.tools.introspect.session_manager") as sm,
        patch("ppsspp_dfx_mcp.tools.introspect._probe_sessions_file", _clean_file),
        patch("ppsspp_dfx_mcp.tools.smoke.run_smoke_checks", _fake_checks),
    ):
        sm.list_sessions = _one_session
        return asyncio.run(health(session_id=session_id))


class TestTopLevelReflectsBattery:
    def test_failing_battery_is_not_ok(self) -> None:
        out = _run(
            "s",
            [
                {
                    "name": "cpu_running",
                    "passed": False,
                    "detail": "paused=True",
                    "value_status": "ok",
                }
            ],
            "fail",
        )
        assert out["status"] != "ok", (
            f"top-level status stayed 'ok' while the battery failed: {out}"
        )

    def test_failing_battery_reason_is_summarised(self) -> None:
        """FR-029: the reason must be readable without walking the list."""
        out = _run(
            "s",
            [
                {
                    "name": "cpu_running",
                    "passed": False,
                    "detail": "paused=True",
                    "value_status": "ok",
                }
            ],
            "fail",
        )
        blob = " ".join(f"{k}={v}" for k, v in out.items()).lower()
        assert "cpu_running" in blob, f"the failing check is not named at the top level: {out}"

    def test_passing_battery_does_not_degrade(self) -> None:
        out = _run(
            "s",
            [
                {
                    "name": "cpu_running",
                    "passed": True,
                    "detail": "paused=False",
                    "value_status": "ok",
                }
            ],
            "pass",
        )
        assert out["status"] == "ok", f"a passing battery degraded status: {out}"

    def test_server_only_probe_is_untouched(self) -> None:
        """No session_id means the battery never ran; nothing to degrade."""
        out = _run(None, [], "fail")
        assert out["status"] == "ok", f"server-only probe was degraded: {out}"
        assert "session_checks" not in out


class TestOverallFieldStillPresent:
    def test_battery_verdict_is_still_reported(self) -> None:
        """T049 adds a summary; it must not replace the detail."""
        out = _run(
            "s",
            [
                {
                    "name": "ws_connected",
                    "passed": False,
                    "detail": "closed",
                    "value_status": "failed",
                }
            ],
            "fail",
        )
        assert out["overall_session_status"] == "fail"
        assert isinstance(out["session_checks"], list)
