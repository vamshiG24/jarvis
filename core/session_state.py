"""
Crash-safe conversation continuity across restarts.

WHY THIS EXISTS
    The recent-turns buffer lived only in RAM, so when the app stopped — a
    crash, a killed process, a power cut, or simply closing it by mistake —
    the next launch started a blank conversation and greeted the user as if
    nothing had happened, even if they were halfway through a request a
    minute earlier.

    This file keeps just enough on disk to pick up where things stopped:

      turns         the last 24 conversation lines — the same recap a fresh
                    session is seeded with after a server 1011
      pending       the last request that never got an answer, so the
                    assistant can say what it was doing and ask to continue
                    instead of silently redoing it (re-running "send that
                    message" twice is worse than asking)
      clean_exit    whether the last run ended on purpose

WHY NOT THE RESUMPTION HANDLE
    It was tried, against the live server. A session killed while the model
    was generating, when resumed from the next launch, REPLAYED the old half-
    finished answer before anything else — the assistant suddenly carried on
    with a reply nobody had heard the start of — and in one run never
    answered anything at all afterwards. Within one run the handle stays the
    right tool (link refreshes, device changes); across a restart, a clean
    session seeded with the transcript is the one that reliably answers. It
    also means no session token is ever written to disk.

HOW LONG IT COUNTS
    The transcript is trusted for 6 hours after a crash and 2 hours after a
    deliberate close — after that it is no longer "where it stopped", it is
    yesterday, and the morning briefing's session summary covers that instead.

    Written atomically (temp file + replace), so a crash mid-write can never
    leave a half-written file that breaks the next launch.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

if getattr(sys, "frozen", False):
    _BASE = Path(sys.executable).parent
else:
    _BASE = Path(__file__).resolve().parent.parent

STATE_FILE = _BASE / "memory" / "session_state.json"

TURNS_TTL_CRASH  = 6 * 3600     # after a crash, the conversation is still "now"
TURNS_TTL_CLEAN  = 2 * 3600     # after a deliberate close, a little less so
MAX_TURNS        = 24

_lock = threading.Lock()


def load() -> dict:
    """What can be restored right now, already filtered by age. {} if nothing."""
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    try:
        age = max(0.0, time.time() - float(data.get("last_seen", 0)))
    except (TypeError, ValueError):
        return {}
    clean = bool(data.get("clean_exit"))
    if age > (TURNS_TTL_CLEAN if clean else TURNS_TTL_CRASH):
        return {}
    turns = [str(t) for t in (data.get("turns") or [])][-MAX_TURNS:]
    if not turns:
        return {}
    return {
        "age": age,
        "clean_exit": clean,
        "turns": turns,
        "pending": data.get("pending") or None,
    }


def save(turns, pending: str | None, clean_exit: bool = False) -> None:
    payload = {
        "version": 2,
        "last_seen": time.time(),
        "clean_exit": bool(clean_exit),
        "turns": list(turns)[-MAX_TURNS:],
        "pending": pending,
    }
    with _lock:
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            os.replace(tmp, STATE_FILE)
        except Exception as e:
            print(f"[State] Could not save session state: {e}")


def clear() -> None:
    with _lock:
        try:
            STATE_FILE.unlink(missing_ok=True)
        except Exception:
            pass
