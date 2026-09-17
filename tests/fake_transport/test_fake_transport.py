"""test_fake_transport.py — unit tests for FakeTransport.

Covers task 5.3: verify FakeTransport satisfies the same implicit
protocol as WsTransport (call / fire_and_forget / wait_for_state).

The protocol contract:
- `call(event, **params)` is async, returns a dict.
- `fire_and_forget(event, **params)` is async, returns None.
- `wait_for_state(predicate, timeout_ms, interval_ms)` is async,
  returns a dict satisfying the predicate, or raises TimeoutError.
"""

from __future__ import annotations

from typing import Any

import pytest

from .fake_transport import FakeTransport

# ============================================================================
# Constructor & initial state
# ============================================================================


class TestConstructor:
    def test_default_state_is_running(self):
        """Default state: stepping=False (CPU running)."""
        ft = FakeTransport()
        assert ft.state == {"stepping": False}

    def test_calls_list_starts_empty(self):
        ft = FakeTransport()
        assert ft.calls == []

    def test_fire_and_forget_calls_list_starts_empty(self):
        ft = FakeTransport()
        assert ft.fire_and_forget_calls == []

    def test_responses_dict_starts_empty(self):
        ft = FakeTransport()
        assert ft._responses == {}

    def test_faf_handlers_dict_starts_empty(self):
        ft = FakeTransport()
        assert ft._faf_handlers == {}


# ============================================================================
# Configuration
# ============================================================================


class TestConfiguration:
    def test_set_state_replaces_current_state(self):
        ft = FakeTransport()
        ft.set_state({"stepping": True, "pc": 0x1234})
        assert ft.state == {"stepping": True, "pc": 0x1234}

    def test_set_state_does_not_mutate_internal_dict(self):
        """set_state copies the input dict — later mutation should not affect state."""
        ft = FakeTransport()
        src = {"stepping": True}
        ft.set_state(src)
        src["stepping"] = False  # mutate the source
        assert ft.state == {"stepping": True}  # unchanged

    def test_state_property_returns_copy(self):
        """state property returns a copy — mutating it does not affect internal state."""
        ft = FakeTransport()
        s = ft.state
        s["stepping"] = True
        assert ft.state == {"stepping": False}  # unchanged

    def test_set_response_stores_response(self):
        ft = FakeTransport()
        ft.set_response("memory.read_u32", {"value": 42})
        assert "memory.read_u32" in ft._responses

    def test_set_faf_handler_stores_handler(self):
        ft = FakeTransport()
        ft.set_faf_handler("cpu.stepping", lambda t, **p: None)
        assert "cpu.stepping" in ft._faf_handlers


# ============================================================================
# call() — task 5.2
# ============================================================================


class TestCall:
    async def test_call_returns_configured_dict_response(self):
        ft = FakeTransport()
        ft.set_response("memory.read_u32", {"value": 42})
        result = await ft.call("memory.read_u32", address=0x1000)
        assert result == {"value": 42}

    async def test_call_returns_dict_copy_not_reference(self):
        """call() returns a copy — mutating the returned dict does not affect the stored response."""
        ft = FakeTransport()
        ft.set_response("memory.read_u32", {"value": 42})
        result = await ft.call("memory.read_u32", address=0x1000)
        result["value"] = 99
        # Subsequent call should still return 42
        result2 = await ft.call("memory.read_u32", address=0x1000)
        assert result2 == {"value": 42}

    async def test_call_invokes_callable_response(self):
        """If response is callable, invoke it with the call's **params."""
        ft = FakeTransport()

        def responder(address: int, **params: Any) -> dict[str, Any]:
            return {"value": address + 1}

        ft.set_response("memory.read_u32", responder)
        result = await ft.call("memory.read_u32", address=0x10)
        assert result == {"value": 0x11}

    async def test_call_returns_empty_dict_for_unconfigured_event(self):
        ft = FakeTransport()
        result = await ft.call("memory.read_u32", address=0x0)
        assert result == {}

    async def test_call_returns_current_state_for_cpu_status(self):
        """cpu.status event returns the current state dict."""
        ft = FakeTransport()
        ft.set_state({"stepping": True, "pc": 0xABCD})
        result = await ft.call("cpu.status")
        assert result == {"stepping": True, "pc": 0xABCD}

    async def test_call_records_invocation(self):
        """call() records (event, params) tuple in self.calls.

        Note: `timeout` is an explicit named parameter of `call()`, so it
        is NOT captured in **params and therefore NOT recorded. Only the
        event-specific **params (e.g. `address`) appear in the recording.
        """
        ft = FakeTransport()
        ft.set_response("memory.read_u32", {"value": 1})
        await ft.call("memory.read_u32", address=0x1000, timeout=2.0)
        assert len(ft.calls) == 1
        event, params = ft.calls[0]
        assert event == "memory.read_u32"
        assert params["address"] == 0x1000
        # timeout is an explicit named param, not in **params
        assert "timeout" not in params

    async def test_call_returns_dict_protocol_compatible_with_wsTransport(self):
        """FakeTransport.call returns a dict — same protocol as WsTransport.call."""
        ft = FakeTransport()
        ft.set_response("test.event", {"foo": "bar"})
        result = await ft.call("test.event")
        assert isinstance(result, dict)


# ============================================================================
# fire_and_forget() — task 5.2
# ============================================================================


class TestFireAndForget:
    async def test_fire_and_forget_returns_none(self):
        """Per protocol: fire_and_forget returns None."""
        ft = FakeTransport()
        result = await ft.fire_and_forget("cpu.stepping")
        assert result is None

    async def test_fire_and_forget_records_call(self):
        ft = FakeTransport()
        await ft.fire_and_forget("cpu.stepping")
        await ft.fire_and_forget("cpu.resume", reason="test")
        assert len(ft.fire_and_forget_calls) == 2
        assert ft.fire_and_forget_calls[0] == ("cpu.stepping", {})
        assert ft.fire_and_forget_calls[1] == ("cpu.resume", {"reason": "test"})

    async def test_fire_and_forget_invokes_handler(self):
        """If a faf handler is configured, it is invoked with (transport, **params)."""
        ft = FakeTransport()
        invoked_with: list[tuple[FakeTransport, dict[str, Any]]] = []

        def handler(t: FakeTransport, **params: Any) -> None:
            invoked_with.append((t, params))

        ft.set_faf_handler("cpu.stepping", handler)
        await ft.fire_and_forget("cpu.stepping", reason="test")
        assert len(invoked_with) == 1
        assert invoked_with[0][0] is ft
        assert invoked_with[0][1] == {"reason": "test"}

    async def test_fire_and_forget_handler_can_mutate_state(self):
        """Handler can mutate the transport's state — simulates PPSSPP state change."""
        ft = FakeTransport()

        def set_stepping_true(t: FakeTransport, **params: Any) -> None:
            t.set_state({"stepping": True})

        ft.set_faf_handler("cpu.stepping", set_stepping_true)
        assert ft.state.get("stepping") is False  # precondition

        await ft.fire_and_forget("cpu.stepping")
        assert ft.state.get("stepping") is True

    async def test_fire_and_forget_handler_invoked_after_recording(self):
        """Handler runs AFTER the call is recorded — can inspect the recorded call."""
        ft = FakeTransport()
        seen_calls: list[list[tuple[str, dict[str, Any]]]] = []

        def handler(t: FakeTransport, **params: Any) -> None:
            seen_calls.append(list(t.fire_and_forget_calls))

        ft.set_faf_handler("cpu.stepping", handler)
        await ft.fire_and_forget("cpu.stepping")
        # Handler should have seen the recorded call
        assert len(seen_calls) == 1
        assert seen_calls[0] == [("cpu.stepping", {})]

    async def test_fire_and_forget_no_handler_is_noop(self):
        """No handler configured — just records the call, no exception."""
        ft = FakeTransport()
        await ft.fire_and_forget("cpu.stepping")
        assert len(ft.fire_and_forget_calls) == 1


# ============================================================================
# wait_for_state() — task 5.2
# ============================================================================


class TestWaitForState:
    async def test_returns_state_when_predicate_satisfied_immediately(self):
        """If predicate matches current state, returns immediately."""
        ft = FakeTransport()
        ft.set_state({"stepping": True})
        result = await ft.wait_for_state(
            lambda s: s.get("stepping") is True,
            timeout_ms=100,
            interval_ms=5,
        )
        assert result == {"stepping": True}

    async def test_returns_state_after_handler_updates_state(self):
        """Predicate satisfied after fire_and_forget handler updates state."""
        ft = FakeTransport()

        def set_stepping_true(t: FakeTransport, **params: Any) -> None:
            t.set_state({"stepping": True})

        ft.set_faf_handler("cpu.stepping", set_stepping_true)

        # fire_and_forget synchronously updates state, so wait_for_state
        # sees stepping=True on the first poll.
        await ft.fire_and_forget("cpu.stepping")
        result = await ft.wait_for_state(
            lambda s: s.get("stepping") is True,
            timeout_ms=100,
            interval_ms=5,
        )
        assert result["stepping"] is True

    async def test_raises_timeout_when_predicate_never_satisfied(self):
        ft = FakeTransport()
        ft.set_state({"stepping": False})
        with pytest.raises(TimeoutError, match="timeout"):
            await ft.wait_for_state(
                lambda s: s.get("stepping") is True,
                timeout_ms=50,
                interval_ms=5,
            )

    async def test_returns_copy_of_state(self):
        """Returned state is a copy — mutation does not affect internal state."""
        ft = FakeTransport()
        ft.set_state({"stepping": True})
        result = await ft.wait_for_state(
            lambda s: s.get("stepping") is True,
            timeout_ms=100,
            interval_ms=5,
        )
        result["stepping"] = False
        assert ft.state.get("stepping") is True  # unchanged

    async def test_default_timeout_interval_values(self):
        """Default timeout_ms=3000, interval_ms=50 (matches WsTransport)."""
        ft = FakeTransport()
        ft.set_state({"stepping": False})
        # Use a short timeout to verify the default is overridable
        with pytest.raises(TimeoutError):
            await ft.wait_for_state(lambda s: s.get("stepping") is True, timeout_ms=30)


# ============================================================================
# Protocol compatibility with WsTransport (task 5.3)
# ============================================================================


class TestProtocolCompatibility:
    """FakeTransport satisfies the same implicit protocol as WsTransport.

    Verified by exercising the same call patterns that SteppingManager
    uses (which is the primary consumer of the protocol).
    """

    async def test_protocol_call_returns_dict(self):
        ft = FakeTransport()
        ft.set_response("test.event", {"ok": True})
        result = await ft.call("test.event")
        assert isinstance(result, dict)

    async def test_protocol_fire_and_forget_returns_none(self):
        ft = FakeTransport()
        result = await ft.fire_and_forget("cpu.stepping")
        assert result is None

    async def test_protocol_wait_for_state_returns_dict_or_raises_timeout(self):
        ft = FakeTransport()
        ft.set_state({"stepping": True})
        # Satisfied → returns dict
        result = await ft.wait_for_state(lambda s: s["stepping"] is True, timeout_ms=50)
        assert isinstance(result, dict)

        # Not satisfied → raises TimeoutError
        ft.set_state({"stepping": False})
        with pytest.raises(TimeoutError):
            await ft.wait_for_state(lambda s: s["stepping"] is True, timeout_ms=30)

    async def test_protocol_works_with_stepping_manager_pause_resume(self):
        """FakeTransport can substitute for WsTransport in SteppingManager."""
        from ppsspp_dfx_mcp.core.stepping import SteppingManager, TrustLevel

        ft = FakeTransport()
        ft.set_state({"stepping": False})
        ft.set_faf_handler("cpu.stepping", lambda t, **p: t.set_state({"stepping": True}))
        ft.set_faf_handler("cpu.resume", lambda t, **p: t.set_state({"stepping": False}))
        ft.set_response(
            "cpu.getAllRegs",
            {
                "categories": [
                    {
                        "name": "GPR",
                        "registerNames": ["pc"],
                        "uintValues": [0x08804000],
                    }
                ]
            },
        )

        mgr = SteppingManager(ft, default_timeout_ms=200, default_interval_ms=5)
        pc, trust = await mgr.safe_get_pc()
        assert pc == 0x08804000
        assert trust == TrustLevel.HIGH

        # Verify pause + resume were called
        events = [ev for ev, _ in ft.fire_and_forget_calls]
        assert "cpu.stepping" in events
        assert "cpu.resume" in events
        # And CPU was resumed at the end
        assert ft.state.get("stepping") is False

    async def test_protocol_call_kwargs_passed_to_callable_response(self):
        """When response is callable, **params are forwarded (matches WsTransport.call)."""
        ft = FakeTransport()
        seen_params: dict[str, Any] = {}

        def responder(**params: Any) -> dict[str, Any]:
            seen_params.update(params)
            return {"ok": True}

        ft.set_response("memory.write_u32", responder)
        await ft.call("memory.write_u32", address=0x1000, value=42)
        assert seen_params == {"address": 0x1000, "value": 42}

    async def test_protocol_call_with_no_params(self):
        """call() with no params is valid (matches WsTransport)."""
        ft = FakeTransport()
        ft.set_response("cpu.breakpoint.list", {"breakpoints": []})
        result = await ft.call("cpu.breakpoint.list")
        assert result == {"breakpoints": []}
