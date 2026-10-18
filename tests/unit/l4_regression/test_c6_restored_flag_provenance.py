"""C-6 lock: `restored` must describe provenance, not the load path.

`_load_sessions` stamps `restored=True` on every record it reads. That is only
meaningful for a PPSSPP owned by a PREVIOUS server run: a session THIS process
started is also re-read from sessions.json on every call, and the manager knows
it is live because its launcher is still in `_launchers`.

Before this fix the flag was therefore always 1 — it carried no information —
and the two attempts to clear it (transport establishment, resilient boot) were
unobservable: they wrote `restored=False` into the file, and the very next load
stamped it back to True. They also leaked a `restored` key into a file whose
contract says it must never appear there.

The flag is now decided at read time from `_launchers`.
"""

from __future__ import annotations

import os

from ppsspp_dfx_mcp.models.session import Session
from ppsspp_dfx_mcp.session import session_manager as sm


def _seed(session_id: str) -> Session:
    """Persist one session record the way a previous server run would have."""
    sess = Session(session_id=session_id, iso_path="game.iso", pid=os.getpid())
    sm._save_sessions({session_id: sess})
    return sess


class TestRestoredFlagIsProvenanceNotLoadPath:
    async def test_live_session_is_not_reported_as_restored(self, isolated_sessions_path) -> None:
        """A session whose launcher is in `_launchers` is running in-process."""
        sid = "c6-live"
        _seed(sid)

        manager = sm.SessionManager()
        manager._launchers[sid] = object()  # liveness knowledge: we own it

        state = await manager.get_session_state(sid)
        assert not state.extra.get("restored"), (
            "a session this process is running must not be reported as restored"
        )
        listed = {s.session_id: s for s in await manager.list_sessions()}
        assert sid in listed
        assert not listed[sid].extra.get("restored")

    async def test_disk_only_session_is_reported_as_restored(self, isolated_sessions_path) -> None:
        """Negative control: with no launcher the flag must still say 'restored'.

        Without this the fix could 'pass' by making the flag always falsy, which
        would silently delete the field's entire meaning.
        """
        sid = "c6-disk"
        _seed(sid)

        manager = sm.SessionManager()  # no launchers: the process belongs to a past run
        state = await manager.get_session_state(sid)
        assert state.extra.get("restored") is True

    async def test_restored_key_never_reaches_the_file(self, isolated_sessions_path) -> None:
        """Read-time stamping must not persist the key (W4 invariant, C-6)."""
        sid = "c6-file"
        _seed(sid)

        manager = sm.SessionManager()
        manager._launchers[sid] = object()
        await manager.get_session_state(sid)
        await manager.list_sessions()

        raw = isolated_sessions_path.read_text(encoding="utf-8")
        assert "restored" not in raw
