"""
core/agent.py — the background agent: long, multi-step work while the
conversation carries on.

WHY THIS EXISTS
    Every tool the assistant has is one call: the Live model decides, a handler
    runs, a sentence comes back. That covers "open WhatsApp". It does not cover
    "find the three best laptops under 80k, compare them and put a report on my
    desktop" — fifteen calls with judgement between each — and the Live session
    is the wrong place to run that:
      - a tool call holds the voice turn, so a chain of them is a long silence,
        and long tool calls are a 1011 risk on the 3.x Live models;
      - the conversation's context belongs to the user, and page dumps push
        the user's own words out of it;
      - the session reconnects (GoAway, quota fail-over), and a plan that lives
        only in its context dies with it.

THE DESIGN
    The Live model hands the goal to `agent_task` (actions/agent_task.py) and
    keeps talking. Here, on a thread of its own, a REST model (the AGENT ladder
    in core/gemini.py) runs a loop of function calls over the SAME tools the
    conversation has — every action and plugin — plus a few that only make
    sense for an agent: a plan, notes, looking at the screen, reading a web
    page, waiting, asking the user, and finishing. Every step shows in the
    Tasks window; questions, milestones and the result are spoken through the
    conversation at a quiet moment (main.py's announcer).

LONG TASKS
    - Context is compacted. Past a size budget the older turns are summarised
      into a digest and the loop continues from a fresh brief: goal, plan and
      notes in full, the digest, and the most recent turns verbatim.
    - State is on disk (memory/agent_tasks.json). A crash or restart leaves a
      running task INTERRUPTED; resuming rebuilds the context from its plan,
      notes and step log. Steps are never replayed.
    - A model that runs out of quota or is overloaded is rested and the next
      rung carries on mid-task. When every rung is down the task waits and
      retries rather than failing.
    - Budgets in steps and minutes ("agent_max_steps", "agent_max_minutes" in
      the config). Time spent waiting for the user does not count. Running out
      asks the model for its best summary — never silence.

SAFETY
    No files unless the user asked for one. A task starts with files OFF: a
    call that would create or write a file is refused before it runs (see
    _writes_files), and the answer goes into the spoken summary instead. The
    user turns files on by asking for a file — when handing the task over, or
    afterwards with agent_task 'save'. Asked for by the user ("dont allow it
    write report until i say explicitly"); a prompt alone was not enough,
    because the conversation model once added "save it to the desktop" to a
    goal on its own.
    The irreversible-action gate (core/confirm.py) still applies, and the agent
    WAITS for the user's CONFIRM or CANCEL and is told which it was. One task
    runs at a time — the tools share one mouse, one keyboard and one browser —
    and the rest queue behind it.
"""
from __future__ import annotations

import copy
import json
import os
import platform
import sys
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Callable, Optional

from core import confirm, gemini

if getattr(sys, "frozen", False):
    _BASE = Path(sys.executable).parent
else:
    _BASE = Path(__file__).resolve().parent.parent

_STORE = _BASE / "memory" / "agent_tasks.json"
_CONFIG = _BASE / "config" / "api_keys.json"
_KEEP_TASKS = 30            # finished tasks kept on disk and in the window

DEFAULT_MAX_STEPS = 120     # tool calls per run
DEFAULT_MAX_MINUTES = 90    # working minutes per run (waiting for you is free)

_TURN_TIMEOUT_MS = 120_000  # one model turn — it may think before it acts
_RESULT_CHARS = 6000        # one tool result, as the model sees it
_LOG_CHARS = 1500           # one tool result, as the step log keeps it
_LOG_ENTRIES = 400          # step log entries kept per task
_CONTEXT_BUDGET = 140_000   # characters of history before it is compacted
_KEEP_RECENT = 8            # turns kept verbatim through a compaction
_SPEAK_GAP = 60.0           # seconds between spoken progress updates
_RETRY_ROUNDS = 6           # every model down: wait and retry this many times

ACTIVE = ("queued", "running", "waiting_user", "waiting_confirm")

# Tools the agent is never given. The agent itself (no recursion), and the
# ones that only steer something the user is looking at in the moment.
_EXCLUDED = {"agent_task", "holo_control", "shutdown_jarvis"}


# ── Task state ────────────────────────────────────────────────────────────────

@dataclass
class Task:
    id: str
    goal: str
    status: str = "queued"
    created: float = 0.0
    updated: float = 0.0
    started: float = 0.0
    finished: float = 0.0
    worked: float = 0.0          # seconds of work across runs, waits excluded
    steps: int = 0               # tool calls made
    plan: list = field(default_factory=list)    # [{"title", "status"}]
    notes: list = field(default_factory=list)   # [str]
    log: list = field(default_factory=list)     # [{"t", "kind", "text", ...}]
    digest: str = ""             # summary of turns compacted away
    current: str = ""            # what it is doing right now, for the window
    question: str = ""           # set while waiting for the user
    summary: str = ""            # the final report
    outcome: str = ""            # success | partial | failed
    model: str = ""              # the model on the latest turn
    error: str = ""
    runs: int = 0
    allow_files: bool = False    # the user explicitly asked for a file
    followup: str = ""           # what the user asked for when resuming it

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


def _now() -> float:
    return time.time()


def _short(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _config() -> dict:
    try:
        return json.loads(_CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ── The agent's own tools ─────────────────────────────────────────────────────
# Everything else it can do comes from the app's tool registries.

_BUILTIN_DECLS = [
    {
        "name": "update_plan",
        "description": (
            "Set or revise your plan: the full list of steps, each with its status. "
            "Call it first, and again whenever a step starts, finishes, or the route "
            "changes. The user watches this list."),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "steps": {
                    "type": "ARRAY",
                    "items": {
                        "type": "OBJECT",
                        "properties": {
                            "title":  {"type": "STRING", "description": "One concrete step"},
                            "status": {"type": "STRING", "description": "pending | active | done | skipped"},
                        },
                        "required": ["title", "status"],
                    },
                },
            },
            "required": ["steps"],
        },
    },
    {
        "name": "note",
        "description": (
            "Keep a finding for later: a number, a name, a file path, a URL, a "
            "decision. On long tasks older steps are summarised away, but notes are "
            "always kept in full — anything you will need in the final answer goes here."),
        "parameters": {
            "type": "OBJECT",
            "properties": {"text": {"type": "STRING"}},
            "required": ["text"],
        },
    },
    {
        "name": "read_webpage",
        "description": (
            "Fetch a web page and return its readable text (title, headings, body). "
            "Use it to read the sources a search found. Long pages come in parts: "
            "pass the 'start' the previous part told you to get the next one."),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "url":   {"type": "STRING"},
                "start": {"type": "INTEGER", "description": "Character offset to read from (default 0)"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "look_at_screen",
        "description": (
            "Take a screenshot of the user's screen and answer a question about it. "
            "Use it to check what an app or the browser is actually showing before "
            "and after you act on it."),
        "parameters": {
            "type": "OBJECT",
            "properties": {"question": {"type": "STRING", "description": "What to look for"}},
            "required": ["question"],
        },
    },
    {
        "name": "system_status",
        "description": "This computer's live CPU, RAM, GPU, temperature, disk and uptime.",
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "recall_memory",
        "description": (
            "Search what the assistant has stored about the user (name, city, job, "
            "preferences, projects, people). Empty query lists everything."),
        "parameters": {
            "type": "OBJECT",
            "properties": {"query": {"type": "STRING"}},
        },
    },
    {
        "name": "wait",
        "description": (
            "Pause up to 600 seconds — for a download, an install, a page or an app "
            "to finish loading — then check again with a tool."),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "seconds": {"type": "INTEGER"},
                "reason":  {"type": "STRING"},
            },
            "required": ["seconds"],
        },
    },
    {
        "name": "tell_user",
        "description": (
            "Say a short progress update out loud. Only for a milestone worth "
            "interrupting the user for — a few per task at most, never routine steps."),
        "parameters": {
            "type": "OBJECT",
            "properties": {"message": {"type": "STRING", "description": "One sentence, in English"}},
            "required": ["message"],
        },
    },
    {
        "name": "ask_user",
        "description": (
            "Ask the user a question and wait for the answer. Only when you are truly "
            "blocked on something only they know or must decide; otherwise take the "
            "most reasonable reading and carry on."),
        "parameters": {
            "type": "OBJECT",
            "properties": {"question": {"type": "STRING", "description": "One clear question, in English"}},
            "required": ["question"],
        },
    },
    {
        "name": "finish",
        "description": (
            "End the task. Call exactly once, when the goal is met or cannot be met. "
            "The summary is read aloud to the user."),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "summary": {
                    "type": "STRING",
                    "description": ("2-6 sentences in English: the answer itself first — "
                                    "unless a file was asked for, this is everything the user "
                                    "gets — then the concrete facts (numbers, names, sources, "
                                    "any file path), then what was not done and why."),
                },
                "outcome": {"type": "STRING", "description": "success | partial | failed"},
            },
            "required": ["summary", "outcome"],
        },
    },
]
_BUILTIN_NAMES = {d["name"] for d in _BUILTIN_DECLS}
# How the agent's own bookkeeping reads in the window, instead of raw arguments.
_LABELS = {"update_plan": "Updating the plan", "note": "Noting a finding",
           "tell_user": "Reporting progress", "ask_user": "Asking you"}

_SYSTEM = """You are the autonomous task agent inside {assistant}, a desktop assistant on the \
user's {platform} computer. The user handed you a goal by voice and went on with their day. \
You work in the background with real tools on their real machine, and report when you are done.

HOW TO WORK
- First call update_plan with 3-8 concrete steps. Keep it current: mark steps active and done \
as you go, and revise it when you learn something that changes the route.
- Act through tools. Every fact you report must come from a tool result in this task, never \
from memory or a guess. When a tool fails, read the error and change the approach (another \
tool, other arguments, another source); never repeat an identical failing call.
- To research: web_search to find sources, then read_webpage on the best ones. Prefer primary \
and recent sources, and note where each key fact came from.
- Read pages with read_webpage: it is fast and happens out of sight. browser_control and \
look_at_screen take over the user's screen while they may be working, so use them only for \
what read_webpage cannot do — clicking, filling forms, a logged-in account, a page that shows \
nothing without a browser.
- Verify before you claim: after writing a file, read it back or list the folder; after \
changing something, check that it changed.
- Save what matters with note — numbers, names, paths, URLs, decisions. On long tasks older \
steps are summarised away; your plan and notes are always kept.
- Work one tool call at a time and do not narrate what you are about to do — do it.

THE USER
- They are not watching. tell_user is for a real milestone only, a few times per task at most.
- ask_user only when truly blocked on something only they know or must decide. Otherwise take \
the most reasonable reading and carry on.
- Never send messages or emails, post anything, buy anything or delete anything unless the \
goal explicitly asks for it. Irreversible actions put a CONFIRM button in front of the user: if \
they cancel, accept it and adapt.

FILES
- Create or save a file ONLY when the brief says FILES: ALLOWED. Otherwise do not write any \
report, note, list or document to disk by any route — no file tools, no code tools, no typing \
into an editor. Such calls are refused. Put the answer in the finish summary; the user can ask \
you to save it afterwards.
- When files are allowed, save the deliverable where the goal says (the Desktop if it does not \
say), as clear Markdown or plain text with sources, and name the path in the summary.

FINISHING
- Call finish exactly once, when the goal is met or cannot be met. Write the summary in \
English; the assistant says it in the user's language.

Today is {today}. Desktop: {desktop}. Downloads: {downloads}."""


# ── The manager ───────────────────────────────────────────────────────────────

class _Cancelled(Exception):
    pass


class _FlattenHistory(Exception):
    """The model refused the history itself — after a switch between model
    generations its thought signatures can be rejected. The cure is to carry
    the task over as text, which compaction already knows how to do."""


class AgentManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._tasks: dict[str, Task] = {}
        self._queue: list[str] = []
        self._worker: Optional[threading.Thread] = None
        self._cancel: dict[str, threading.Event] = {}
        self._replies: dict[str, list[str]] = {}
        self._inbox: dict[str, list[str]] = {}    # instructions for a running task
        self._reply_ev: dict[str, threading.Event] = {}
        self._last_spoken = 0.0
        self._last_save = 0.0
        self._clock = time.monotonic()      # working-time stopwatch, see _tick
        self._interrupted: list[Task] = []  # stopped by the last shutdown

        # Bound by main.py (see bind). Until then the agent can plan and read
        # the web but has none of the app's tools, and nobody hears it.
        self._tools: Callable[[], list] = lambda: []
        self._execute: Optional[Callable[[str, dict, Callable[[str], None]], str]] = None
        self._on_change: Optional[Callable[[list, bool], None]] = None
        self._announce: Optional[Callable[[str, bool], None]] = None
        self._log_cb: Optional[Callable[[str], None]] = None
        self._assistant = "JARVIS"

        self._load()

    # ── wiring ────────────────────────────────────────────────────────────
    def bind(self, tools, execute, on_change=None, announce=None, log=None,
             assistant: str = "JARVIS") -> None:
        self._tools = tools
        self._execute = execute
        self._on_change = on_change
        self._announce = announce
        self._log_cb = log
        self._assistant = assistant or "JARVIS"
        self._changed(None)

    def take_stopped(self) -> list[Task]:
        """Tasks the last shutdown cut off and nobody has resumed yet — once.
        main.py says so in the crash greeting, or announces it."""
        with self._lock:
            stopped, self._interrupted = self._interrupted, []
            return [t for t in stopped if t.status == "interrupted"]

    # ── public API (any thread; nothing here blocks) ──────────────────────
    def start(self, goal: str, allow_files: bool = False) -> Task:
        goal = str(goal or "").strip()
        t = Task(id=uuid.uuid4().hex[:6], goal=goal, created=_now(), updated=_now(),
                 allow_files=bool(allow_files))
        with self._lock:
            self._tasks[t.id] = t
            self._queue.append(t.id)
            self._trim()
        self._log(f"AGENT: Task {t.id} queued — {_short(goal, 80)}")
        self._persist(force=True)
        self._changed(t, reveal=True)
        self._kick()
        return t

    def resume(self, task_id: str = "", followup: str = "",
               allow_files: bool = False) -> Optional[Task]:
        with self._lock:
            t = self._find(task_id, lambda x: x.status not in ACTIVE)
            if t is None:
                return None
            if followup:
                t.followup = str(followup).strip()
            if allow_files:
                t.allow_files = True
            t.status = "queued"
            t.finished = 0.0
            t.error = ""
            t.question = ""
            self._queue.append(t.id)
        self._entry(t, "sys", "Resumed by the user."
                    + (f" New instruction: {t.followup}" if followup else ""))
        self._persist(force=True)
        self._changed(t, reveal=True)
        self._kick()
        return t

    def cancel(self, task_id: str = "") -> Optional[Task]:
        with self._lock:
            t = self._find(task_id, lambda x: x.status in ACTIVE)
            if t is None:
                return None
            if t.id in self._queue:
                self._queue.remove(t.id)
                t.status = "cancelled"
                t.finished = _now()
                self._entry(t, "sys", "Cancelled before it started.")
            ev = self._cancel.get(t.id)
            if ev:
                ev.set()
                t.current = "Cancelling — stops after the current step"
            rev = self._reply_ev.get(t.id)
            if rev:
                rev.set()
        self._persist(force=True)
        self._changed(t)
        return t

    def instruct(self, task_id: str, text: str, allow_files: bool = False) -> Optional[Task]:
        """Something new from the user for a task. A running one reads it before
        its next step; a finished or stopped one is resumed to act on it."""
        text = str(text or "").strip()
        with self._lock:
            t = self._find(task_id)
            if t is None:
                return None
            if t.status in ACTIVE:
                if allow_files:
                    t.allow_files = True
                self._inbox.setdefault(t.id, []).append(text)
                self._entry(t, "reply", f"New instruction: {text}")
        if t.status not in ACTIVE:
            return self.resume(t.id, followup=text, allow_files=allow_files)
        self._persist(force=True)
        self._changed(t)
        return t

    def reply(self, task_id: str, message: str) -> Optional[Task]:
        with self._lock:
            t = self._find(task_id, lambda x: x.status == "waiting_user")
            if t is None:
                return None
            self._replies.setdefault(t.id, []).append(str(message or "").strip())
            ev = self._reply_ev.get(t.id)
            if ev:
                ev.set()
        return t

    def get(self, task_id: str = "") -> Optional[Task]:
        with self._lock:
            return self._find(task_id)

    def tasks(self) -> list[Task]:
        with self._lock:
            return list(self._tasks.values())

    def snapshot(self) -> list[dict]:
        """Deep copies, newest first — safe to hand to another thread."""
        with self._lock:
            return [copy.deepcopy(asdict(t)) for t in
                    sorted(self._tasks.values(), key=lambda x: x.created, reverse=True)]

    def describe(self, t: Task) -> str:
        """A status report for the conversation model."""
        with self._lock:
            lines = [f"Task {t.id}: {t.goal}", f"Status: {t.status}"]
            if t.started:
                lines.append(f"Worked {int(t.worked // 60)} min {int(t.worked % 60)} s, "
                             f"{t.steps} tool calls so far.")
            if t.plan:
                lines.append("Plan: " + "; ".join(
                    f"[{s.get('status', 'pending')}] {s.get('title', '')}" for s in t.plan))
            if t.current:
                lines.append(f"Doing now: {t.current}")
            if t.question:
                lines.append(f"Waiting for the user's answer to: {t.question}")
            recent = [e for e in t.log if e.get("kind") in ("tool", "note", "say")][-3:]
            if recent:
                lines.append("Latest: " + " | ".join(_short(e.get("text", ""), 120) for e in recent))
            if t.summary:
                lines.append(f"Result ({t.outcome}): {t.summary}")
            if t.error:
                lines.append(f"Error: {t.error}")
            return "\n".join(lines)

    # ── bookkeeping ───────────────────────────────────────────────────────
    def _find(self, task_id: str = "", where=None) -> Optional[Task]:
        task_id = str(task_id or "").strip().lower()
        if task_id:
            t = self._tasks.get(task_id)
            return t if t and (where is None or where(t)) else None
        pool = [t for t in self._tasks.values() if where is None or where(t)]
        if not pool:
            return None
        # The active one if there is one, otherwise the most recent.
        live = [t for t in pool if t.status in ACTIVE and t.status != "queued"]
        return live[0] if live else max(pool, key=lambda x: x.updated)

    def _trim(self) -> None:
        done = sorted((t for t in self._tasks.values() if t.status not in ACTIVE),
                      key=lambda x: x.updated)
        for t in done[:max(0, len(self._tasks) - _KEEP_TASKS)]:
            self._tasks.pop(t.id, None)

    def _entry(self, t: Task, kind: str, text: str, **extra) -> None:
        with self._lock:
            t.log.append({"t": _now(), "kind": kind, "text": str(text), **extra})
            if len(t.log) > _LOG_ENTRIES:
                del t.log[: len(t.log) - _LOG_ENTRIES]
            t.updated = _now()

    def _set(self, t: Task, reveal: bool = False, **changes) -> None:
        with self._lock:
            for k, v in changes.items():
                setattr(t, k, v)
            t.updated = _now()
        self._persist(force="status" in changes)
        self._changed(t, reveal=reveal)

    def _changed(self, t: Optional[Task], reveal: bool = False) -> None:
        if self._on_change:
            try:
                self._on_change(self.snapshot(), reveal)
            except Exception as e:
                print(f"[Agent] window update failed: {e}")

    def _log(self, text: str) -> None:
        print(f"[Agent] {text}")
        if self._log_cb:
            try:
                self._log_cb(text)
            except Exception:
                pass

    def _say(self, text: str, urgent: bool) -> None:
        if self._announce:
            try:
                self._announce(text, urgent)
            except Exception as e:
                print(f"[Agent] announce failed: {e}")

    # ── persistence ───────────────────────────────────────────────────────
    def _load(self) -> None:
        try:
            raw = json.loads(_STORE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as e:
            print(f"[Agent] could not read {_STORE.name}: {e}")
            return
        for d in raw.get("tasks", []):
            try:
                t = Task.from_dict(d)
            except Exception:
                continue
            # Nothing is running yet in this process: whatever was mid-flight
            # when the last one ended has stopped.
            if t.status in ACTIVE:
                was = t.status
                t.status = "interrupted"
                t.current = ""
                t.question = ""
                t.log.append({"t": _now(), "kind": "sys",
                              "text": f"Interrupted — the assistant closed while it was {was}."})
                self._interrupted.append(t)
            self._tasks[t.id] = t

    def _persist(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_save < 2.0:
            return
        self._last_save = now
        with self._lock:
            data = {"version": 1, "tasks": [asdict(t) for t in self._tasks.values()]}
        try:
            _STORE.parent.mkdir(parents=True, exist_ok=True)
            tmp = _STORE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, _STORE)
        except Exception as e:
            print(f"[Agent] could not save tasks: {e}")

    # ── the worker ────────────────────────────────────────────────────────
    def _kick(self) -> None:
        with self._lock:
            if self._worker and self._worker.is_alive():
                return
            self._worker = threading.Thread(target=self._work, daemon=True, name="agent-worker")
            self._worker.start()

    def _work(self) -> None:
        while True:
            with self._lock:
                if not self._queue:
                    self._worker = None
                    return
                t = self._tasks.get(self._queue.pop(0))
                if t is None or t.status != "queued":
                    continue
                self._cancel[t.id] = threading.Event()
                self._reply_ev[t.id] = threading.Event()
            try:
                self._run(t)
            except _Cancelled:
                self._entry(t, "sys", "Cancelled by the user.")
                self._set(t, status="cancelled", finished=_now(), current="", question="")
                self._log(f"AGENT: Task {t.id} cancelled.")
            except Exception as e:
                traceback.print_exc()
                self._entry(t, "error", f"The agent crashed: {e}")
                self._set(t, status="failed", finished=_now(), current="", error=str(e))
                self._log(f"ERR: Task {t.id} crashed — {e}")
                self._say(f'The background task "{_short(t.goal, 80)}" stopped with an '
                          f"error: {_short(str(e), 160)}.", urgent=True)
            finally:
                with self._lock:
                    self._cancel.pop(t.id, None)
                    self._reply_ev.pop(t.id, None)
                    self._replies.pop(t.id, None)
                self._persist(force=True)

    def _check(self, t: Task) -> None:
        ev = self._cancel.get(t.id)
        if ev is not None and ev.is_set():
            raise _Cancelled()

    def _sleep(self, t: Task, seconds: float) -> None:
        ev = self._cancel.get(t.id)
        if ev is not None and ev.wait(max(0.0, seconds)):
            raise _Cancelled()

    # ── one run of a task ─────────────────────────────────────────────────
    def _run(self, t: Task) -> None:
        from google.genai import types

        cfg = _config()
        max_steps = int(cfg.get("agent_max_steps") or DEFAULT_MAX_STEPS)
        max_secs = 60.0 * float(cfg.get("agent_max_minutes") or DEFAULT_MAX_MINUTES)

        resumed = t.runs > 0
        self._set(t, reveal=True, status="running", runs=t.runs + 1,
                  started=t.started or _now(), current="Planning")
        self._log(f"AGENT: Task {t.id} {'resumed' if resumed else 'started'} — {_short(t.goal, 80)}")

        decls = self._declarations()
        config = types.GenerateContentConfig(
            system_instruction=self._system_prompt(),
            tools=[types.Tool(function_declarations=decls)],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        contents = [self._brief(t, resumed=resumed)]

        run_steps = 0
        self._restart_clock()
        worked0 = t.worked
        idle = 0
        flattened = 0
        avoid: set[str] = set()      # models that garbled a tool call this run
        while True:
            self._check(t)
            self._tick(t)
            if run_steps >= max_steps or t.worked - worked0 >= max_secs:
                self._wrap_up(t, contents, config,
                              f"{run_steps} tool calls" if run_steps >= max_steps
                              else f"{int(max_secs // 60)} minutes")
                return

            with self._lock:
                news = self._inbox.pop(t.id, [])
            if news:
                contents.append(types.Content(role="user", parts=[types.Part(text=(
                    "NEW INSTRUCTION FROM THE USER: " + " / ".join(news)
                    + ("\nFILES: ALLOWED now — the user asked for a file."
                       if t.allow_files else "")))]))

            if self._size(contents) > _CONTEXT_BUDGET:
                contents = self._compact(t, contents, keep=_KEEP_RECENT)

            try:
                resp, model = self._generate(t, contents, config, avoid)
            except _FlattenHistory:
                flattened += 1
                if flattened > 2:
                    self._fail(t, "Gemini kept refusing the task history.")
                    return
                self._entry(t, "sys", "Carried the task over to another model as a summary.")
                contents = self._compact(t, contents, keep=0)
                continue
            flattened = 0
            if resp is None:
                self._fail(t, model or "No Gemini model answered after several retries.")
                return
            if model != t.model:
                self._set(t, model=model)

            cand = (resp.candidates or [None])[0]
            content = getattr(cand, "content", None)
            parts = list(getattr(content, "parts", None) or [])
            calls = [p.function_call for p in parts if getattr(p, "function_call", None)]
            said = " ".join(p.text.strip() for p in parts
                            if getattr(p, "text", None) and not getattr(p, "thought", False)).strip()
            if said:
                self._entry(t, "think", said)

            if not calls:
                idle += 1
                if "MALFORMED" in str(getattr(cand, "finish_reason", "") or ""):
                    # Asking the same model again gets the same garbled call
                    # (seen five times running); the next rung usually manages.
                    avoid.add(model)
                    self._entry(t, "sys", f"{model} garbled a tool call — trying another model.")
                if idle >= 3 and said:
                    # It keeps answering in prose: that prose is its answer.
                    self._finish(t, {"summary": said, "outcome": "partial"})
                    return
                if idle >= 5:
                    self._fail(t, "The model stopped producing usable replies "
                                  f"({getattr(cand, 'finish_reason', None) or 'empty'}).")
                    return
                if content is not None and parts:
                    contents.append(content)
                why = getattr(cand, "finish_reason", None) if cand else "no candidate"
                nudge = ("Continue the task: call the next tool. If the goal is complete, "
                         "call finish with the summary.")
                if not parts:
                    nudge = (f"Your last reply was empty ({why}). Continue: call exactly one "
                             f"tool with valid arguments, or finish.")
                contents.append(types.Content(role="user", parts=[types.Part(text=nudge)]))
                self._persist()
                continue
            idle = 0
            contents.append(content)

            responses = []
            done = None
            for fc in calls:
                self._check(t)
                name = fc.name or ""
                args = dict(fc.args or {})
                if name == "finish":
                    done = args
                    responses.append(types.Part.from_function_response(
                        name=name, response={"result": "ok"}))
                    continue
                result = self._dispatch(t, name, args)
                run_steps += 1
                responses.append(types.Part.from_function_response(
                    name=name, response={"result": result}))
            if done is not None:
                self._finish(t, done)
                return
            contents.append(types.Content(role="user", parts=responses))

    def _tick(self, t: Task) -> None:
        """Add the time since the last tick to the task's working time."""
        now = time.monotonic()
        with self._lock:
            t.worked += max(0.0, now - self._clock)
        self._clock = now

    def _restart_clock(self) -> None:
        """After waiting for the user: that wait was not working time."""
        self._clock = time.monotonic()

    # ── model turns ───────────────────────────────────────────────────────
    def _generate(self, t: Task, contents: list, config, avoid=frozenset()):
        """One turn, walking the AGENT ladder. Returns (response, model) or
        (None, reason). Waits and retries when every model is down. Models in
        `avoid` go last, not out: a garbled call beats no answer at all."""
        last = ""
        for rnd in range(_RETRY_ROUNDS + 1):
            self._check(t)
            try:
                cl = gemini.client(timeout_ms=_TURN_TIMEOUT_MS)
            except Exception as e:
                return None, f"Gemini is not set up: {e}"
            ladder = gemini.agent_ladder()
            ladder = tuple(m for m in ladder if m not in avoid) + tuple(m for m in ladder if m in avoid)
            tried = [m for m in ladder if not gemini.is_cooling(m)] or list(ladder)
            transient = 0
            for model in tried:
                self._check(t)
                try:
                    return cl.models.generate_content(
                        model=model, contents=contents, config=config), model
                except Exception as e:
                    msg = str(e)
                    last = f"{model}: {_short(msg, 200)}"
                    low = msg.lower()
                    if "api key not valid" in low or "api_key_invalid" in low:
                        return None, "The Gemini API key was rejected."
                    if "400" in msg and ("signature" in low or "thought" in low):
                        raise _FlattenHistory()
                    gemini.note_failure(model, e)
                    if not ("400" in msg or "invalid_argument" in low):
                        transient += 1     # quota, overload, 500, network: passes
            if transient == 0:
                # Every model refused the request itself — sending the same
                # request again will not change that.
                return None, f"The request was refused: {last}"
            wait = min(300, 15 * 2 ** rnd)
            self._entry(t, "sys", f"Every model is busy or out of quota — retrying in {wait} s.")
            self._set(t, current=f"Waiting {wait} s for a free model")
            self._sleep(t, wait)
        return None, f"No model answered after {_RETRY_ROUNDS} retries. Last: {last}"

    # ── tools ─────────────────────────────────────────────────────────────
    def _declarations(self) -> list[dict]:
        out = [dict(d) for d in _BUILTIN_DECLS]
        seen = set(_BUILTIN_NAMES)
        try:
            app_tools = list(self._tools() or [])
        except Exception as e:
            print(f"[Agent] tool list failed: {e}")
            app_tools = []
        for d in app_tools:
            name = d.get("name") if isinstance(d, dict) else None
            if not name or name in seen or name in _EXCLUDED:
                continue
            seen.add(name)
            out.append({"name": name, "description": d.get("description", ""),
                        "parameters": d.get("parameters") or {"type": "OBJECT", "properties": {}}})
        return out

    def _dispatch(self, t: Task, name: str, args: dict) -> str:
        label = _LABELS.get(name) or f"{name} {_short(_fmt_args(args), 90)}".strip()
        self._set(t, current=label)
        started = time.monotonic()
        writes = "" if t.allow_files else _writes_files(name, args)
        try:
            handler = getattr(self, f"_tool_{name}", None) if name in _BUILTIN_NAMES else None
            if writes:
                result = (f"REFUSED — {writes}. The user has not asked for a file, so nothing "
                          f"may be created or saved on disk, by this tool or any other. Give "
                          f"the result in your finish summary instead; the user can ask for a "
                          f"file afterwards.")
                self._entry(t, "error", f"Blocked: {writes} (files are off for this task)")
            elif handler is not None:
                result = handler(t, args)
            elif self._execute is not None:
                result = self._execute(name, args, lambda s: self._entry(t, "tool_say", s))
            else:
                result = f"Tool '{name}' is not available to the agent."
        except _Cancelled:
            raise
        except Exception as e:
            traceback.print_exc()
            result = f"Tool '{name}' failed: {e}"
        result = str(result if result is not None else "Done.")

        if result.startswith("[CONFIRMATION_PENDING]"):
            result = self._await_confirmation(t, name)

        with self._lock:
            t.steps += 1
        if name not in ("update_plan", "note", "tell_user", "ask_user"):
            self._entry(t, "tool", label, name=name, result=result[:_LOG_CHARS],
                        secs=round(time.monotonic() - started, 1))
        self._set(t, current="Thinking")
        if len(result) > _RESULT_CHARS:
            result = (result[:_RESULT_CHARS]
                      + f"\n…[{len(result) - _RESULT_CHARS} more characters cut]")
        return result

    def _await_confirmation(self, t: Task, name: str) -> str:
        seq = confirm.latest_seq()
        title = confirm.pending_title() or name
        self._entry(t, "ask", f"Waiting for you to press CONFIRM or CANCEL: {title}")
        self._set(t, reveal=True, status="waiting_confirm", current=f"Waiting for CONFIRM: {title}")
        self._say(f'The background task "{_short(t.goal, 70)}" is waiting for the user to press '
                  f"CONFIRM or CANCEL on screen for: {title}. Tell them in one sentence.",
                  urgent=True)
        self._tick(t)
        ev = self._cancel.get(t.id)
        status, out = confirm.wait_outcome(seq, cancelled=lambda: bool(ev and ev.is_set()))
        self._restart_clock()
        if ev is not None and ev.is_set() and confirm.latest_seq() == seq and confirm.pending_title():
            # The task is gone: its banner must not stay up to be confirmed later.
            confirm.resolve(False)
        self._check(t)
        self._set(t, status="running")
        self._entry(t, "reply", f"Confirmation: {status}")
        if status == "confirmed":
            return f"The user CONFIRMED it and it was done. Result: {out}"
        if status == "failed":
            return f"The user confirmed it but it failed: {out}"
        if status == "cancelled":
            return ("The user pressed CANCEL. It was NOT done. Do not try it again — adapt "
                    "the plan or finish.")
        return ("Nobody confirmed in time, so it was NOT done. Do not try it again — adapt "
                "the plan, or ask_user whether they want it.")

    def _tool_update_plan(self, t: Task, args: dict) -> str:
        steps = []
        for s in args.get("steps") or []:
            if isinstance(s, dict) and str(s.get("title", "")).strip():
                st = str(s.get("status", "pending")).strip().lower()
                steps.append({"title": _short(s["title"], 160),
                              "status": st if st in ("pending", "active", "done", "skipped") else "pending"})
        if not steps:
            return "The plan needs at least one step with a title."
        self._set(t, plan=steps[:20])
        done = sum(1 for s in steps if s["status"] == "done")
        return f"Plan saved: {len(steps)} steps, {done} done."

    def _tool_note(self, t: Task, args: dict) -> str:
        text = _short(args.get("text", ""), 600)
        if not text:
            return "Empty note ignored."
        with self._lock:
            t.notes.append(text)
            if len(t.notes) > 60:
                del t.notes[0]
        self._entry(t, "note", text)
        return f"Noted ({len(t.notes)} notes)."

    def _tool_tell_user(self, t: Task, args: dict) -> str:
        msg = _short(args.get("message", ""), 300)
        self._entry(t, "say", msg)
        now = time.monotonic()
        if now - self._last_spoken < _SPEAK_GAP:
            return ("Shown in the task window. Spoken updates are limited to one a minute, "
                    "so this one was not read aloud.")
        self._last_spoken = now
        self._say(f'Progress on the background task "{_short(t.goal, 70)}": {msg}', urgent=False)
        return "The user will hear it at the next quiet moment."

    def _tool_ask_user(self, t: Task, args: dict) -> str:
        q = _short(args.get("question", ""), 400)
        if not q:
            return "Ask a question."
        ev = self._reply_ev.get(t.id)
        if ev is not None:
            ev.clear()
        self._entry(t, "ask", q)
        self._set(t, reveal=True, status="waiting_user", question=q, current="Waiting for your answer")
        self._log(f"AGENT: Task {t.id} needs you — {_short(q, 90)}")
        self._say(f'The background task "{_short(t.goal, 70)}" needs an answer from the user: {q} '
                  f"Ask them. When they answer, call agent_task with action 'reply', task_id "
                  f"'{t.id}' and their answer as the message, in English.", urgent=True)
        self._tick(t)
        while True:
            if ev is not None:
                ev.wait(5.0)
            self._check(t)
            with self._lock:
                answers = self._replies.pop(t.id, [])
                if ev is not None:
                    ev.clear()
            if answers:
                break
        self._restart_clock()
        answer = " / ".join(a for a in answers if a) or "(no answer)"
        self._entry(t, "reply", answer)
        self._set(t, status="running", question="", current="Thinking")
        return f"The user answered: {answer}"

    def _tool_wait(self, t: Task, args: dict) -> str:
        try:
            secs = max(1, min(600, int(args.get("seconds") or 10)))
        except Exception:
            secs = 10
        self._set(t, current=f"Waiting {secs} s — {_short(args.get('reason', ''), 60)}".rstrip(" —"))
        self._sleep(t, secs)
        return f"Waited {secs} seconds."

    def _tool_read_webpage(self, t: Task, args: dict) -> str:
        return read_webpage(str(args.get("url", "")), int(args.get("start") or 0))

    def _tool_look_at_screen(self, t: Task, args: dict) -> str:
        from google.genai import types
        from actions.screen_processor import _capture_screen
        img, mime = _capture_screen()
        q = str(args.get("question") or "Describe what is on the screen.")
        answer = gemini.text(
            [types.Part.from_bytes(data=img, mime_type=mime),
             f"This is a screenshot of the user's computer screen. {q} Answer precisely: "
             f"name the windows, the visible text and the state of anything relevant."],
            tier=gemini.SMART, timeout_ms=60_000)
        return answer or "The screen was captured but no model could read it just now."

    def _tool_system_status(self, t: Task, args: dict) -> str:
        from actions.system_monitor import get_system_status
        return str(get_system_status())

    def _tool_recall_memory(self, t: Task, args: dict) -> str:
        from memory.memory_manager import search_memory
        return search_memory(str(args.get("query", "")), limit=12)

    # ── endings ───────────────────────────────────────────────────────────
    def _finish(self, t: Task, args: dict) -> None:
        summary = str(args.get("summary") or "").strip() or "Finished."
        outcome = str(args.get("outcome") or "success").strip().lower()
        if outcome not in ("success", "partial", "failed"):
            outcome = "success"
        self._tick(t)
        with self._lock:
            for s in t.plan:
                if outcome == "success" and s.get("status") in ("active", "pending"):
                    s["status"] = "done"
        self._entry(t, "done", summary)
        status = "done" if outcome != "failed" else "failed"
        self._set(t, status=status, outcome=outcome, summary=summary,
                  finished=_now(), current="", question="", followup="")
        self._log(f"AGENT: Task {t.id} {outcome} — {_short(summary, 110)}")
        word = {"success": "finished", "partial": "finished in part",
                "failed": "could not be completed"}[outcome]
        self._say(f'The background task "{_short(t.goal, 80)}" {word}. Tell the user the '
                  f"result with its concrete facts, in a few sentences: {summary}", urgent=True)

    def _fail(self, t: Task, why: str) -> None:
        self._tick(t)
        self._entry(t, "error", why)
        self._set(t, status="failed", outcome="failed", error=why, finished=_now(), current="")
        self._log(f"ERR: Task {t.id} stopped — {_short(why, 110)}")
        self._say(f'The background task "{_short(t.goal, 80)}" had to stop: {_short(why, 200)}. '
                  f"Tell the user in one sentence; it can be resumed later.", urgent=True)

    def _wrap_up(self, t: Task, contents: list, config, spent: str) -> None:
        """Out of budget: one last turn to report what it has, never silence."""
        from google.genai import types
        self._entry(t, "sys", f"Budget reached ({spent}) — asking for a final report.")
        contents = contents + [types.Content(role="user", parts=[types.Part(text=(
            f"The budget for this task is used up ({spent}). Do not call any other tool. "
            f"Call finish now: say what was achieved, the concrete results so far, and what "
            f"is left. Use outcome 'partial' unless the goal is fully met."))])]
        try:
            resp, _ = self._generate(t, contents, config)
        except _FlattenHistory:
            resp = None
        for p in (getattr(getattr((getattr(resp, "candidates", None) or [None])[0],
                                  "content", None), "parts", None) or []):
            fc = getattr(p, "function_call", None)
            if fc and fc.name == "finish":
                self._finish(t, dict(fc.args or {}))
                return
        notes = "; ".join(t.notes[-5:])
        self._finish(t, {"summary": f"I ran out of budget ({spent}) before finishing. "
                                    f"Findings so far: {notes or 'see the task window'}.",
                         "outcome": "partial"})

    # ── context ───────────────────────────────────────────────────────────
    def _system_prompt(self) -> str:
        try:
            from core import known_folders as kf
            desktop, downloads = str(kf.desktop()), str(kf.downloads())
        except Exception:
            desktop = downloads = "(unknown)"
        return _SYSTEM.format(assistant=self._assistant,
                              platform=f"{platform.system()} {platform.release()}",
                              today=time.strftime("%A, %d %B %Y, %H:%M"),
                              desktop=desktop, downloads=downloads)

    def _brief(self, t: Task, resumed: bool = False, digest: str = ""):
        """The first user turn: the goal, and — after a compaction or a restart
        — everything the model needs to carry on without the turns it lost."""
        from google.genai import types
        with self._lock:
            lines = [f"GOAL:\n{t.goal}"]
            if resumed:
                lines.append(
                    "This task was stopped part-way (a restart, an error, a cancel or the "
                    "budget) and is now being RESUMED. Check the current state with tools "
                    "before redoing anything that has side effects, and do not repeat work "
                    "that is already done.")
            if t.plan:
                lines.append("PLAN SO FAR:\n" + "\n".join(
                    f"{i + 1}. [{s['status']}] {s['title']}" for i, s in enumerate(t.plan)))
            if t.notes:
                lines.append("YOUR NOTES:\n" + "\n".join(f"- {n}" for n in t.notes))
            if resumed and t.summary:
                lines.append(f"RESULT REPORTED LAST TIME:\n{t.summary}")
            if t.followup:
                lines.append(f"NEW INSTRUCTION FROM THE USER:\n{t.followup}\nDo this — and "
                             f"only this, unless the goal is still unfinished.")
            lines.append("FILES: ALLOWED — the user asked for a file." if t.allow_files else
                         "FILES: NOT ALLOWED — the user has not asked for a file. Give the "
                         "result in the finish summary; do not write anything to disk.")
            digest = digest or t.digest
            if digest:
                lines.append("PROGRESS SO FAR (summary of earlier steps):\n" + digest)
            elif resumed:
                recent = [e for e in t.log if e.get("kind") in
                          ("tool", "note", "think", "ask", "reply", "error", "sys")][-25:]
                if recent:
                    lines.append("RECENT STEPS:\n" + "\n".join(
                        f"- {e['kind']}: {_short(e.get('text', ''), 200)}"
                        + (f" -> {_short(e.get('result', ''), 300)}" if e.get("result") else "")
                        for e in recent))
            if resumed or digest:
                lines.append("Continue from here.")
            else:
                lines.append("Start by calling update_plan, then work through it.")
        return types.Content(role="user", parts=[types.Part(text="\n\n".join(lines))])

    @staticmethod
    def _size(contents: list) -> int:
        n = 0
        for c in contents:
            for p in getattr(c, "parts", None) or []:
                if getattr(p, "text", None):
                    n += len(p.text)
                fc = getattr(p, "function_call", None)
                if fc:
                    n += len(json.dumps(dict(fc.args or {}), default=str))
                fr = getattr(p, "function_response", None)
                if fr:
                    n += len(json.dumps(fr.response or {}, default=str))
        return n

    def _compact(self, t: Task, contents: list, keep: int) -> list:
        """Summarise the older turns into a digest and continue from a fresh
        brief plus the last `keep` turns verbatim. The kept slice starts on a
        model turn so the user/model alternation stays valid."""
        cut = len(contents) - keep if keep else len(contents)
        while 1 <= cut < len(contents) and getattr(contents[cut], "role", "") != "model":
            cut += 1
        if cut <= 1 and keep:
            return contents
        old = contents[1:cut]
        self._set(t, current="Condensing notes")
        transcript = _render_turns(old)
        prompt = (
            "You are condensing the working history of an autonomous agent so it can carry "
            "on with less context. Write a factual digest, at most 400 words: what has been "
            "done, what each tool returned that matters (exact numbers, names, file paths, "
            "URLs), what failed and why, and what remains. No advice, no preamble.\n\n"
            f"GOAL: {t.goal}\n\n"
            + (f"EARLIER DIGEST:\n{t.digest}\n\n" if t.digest else "")
            + f"NEW HISTORY:\n{transcript[-60000:]}")
        digest = gemini.text(prompt, tier=gemini.SMART, timeout_ms=90_000)
        if not digest:
            # No model to summarise with: keep a mechanical record instead.
            digest = ((t.digest + "\n") if t.digest else "") + _short(transcript[-6000:], 6000)
        with self._lock:
            t.digest = digest[-8000:]
        self._entry(t, "sys", "Condensed the earlier steps into a summary to keep going.")
        self._persist(force=True)
        return [self._brief(t, digest=t.digest)] + list(contents[cut:])


# ── helpers ───────────────────────────────────────────────────────────────────

# Calls that put something on disk, by tool and action. code_helper's default
# action is 'auto', which may write, so a missing action counts as writing.
_FILE_WRITES = {
    "file_controller": {"create_file", "write", "create_folder"},
    "code_helper":     {"write", "edit", "build", "auto", ""},
}


def _writes_files(name: str, args: dict) -> str:
    """What this call would write to disk, in words — '' when it writes nothing."""
    if name == "dev_agent":
        return "dev_agent builds a project on disk"
    acts = _FILE_WRITES.get(name)
    if acts is None:
        return ""
    action = str((args or {}).get("action") or "").strip().lower()
    if action in acts:
        return f"{name} {action or 'auto'} would write a file"
    return ""


def _fmt_args(args: dict) -> str:
    bits = []
    for k, v in (args or {}).items():
        if isinstance(v, str) and len(v) > 60:
            v = v[:57] + "…"
        bits.append(f"{k}={v}")
    return " ".join(bits)


def _render_turns(contents: list) -> str:
    out = []
    for c in contents:
        role = getattr(c, "role", "")
        for p in getattr(c, "parts", None) or []:
            if getattr(p, "thought", False):
                continue
            if getattr(p, "text", None):
                out.append(f"[{role}] {p.text.strip()}")
            fc = getattr(p, "function_call", None)
            if fc:
                out.append(f"[call] {fc.name}({_short(json.dumps(dict(fc.args or {}), default=str), 400)})")
            fr = getattr(p, "function_response", None)
            if fr:
                res = (fr.response or {}).get("result", fr.response)
                out.append(f"[result of {fr.name}] {_short(str(res), 1200)}")
    return "\n".join(out)


_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
_PAGE_CHARS = 12_000


def read_webpage(url: str, start: int = 0) -> str:
    """A page's readable text, in parts of _PAGE_CHARS characters."""
    import requests
    url = (url or "").strip()
    if not url:
        return "Give a URL."
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        r = requests.get(url, headers={"User-Agent": _UA, "Accept-Language": "en,*;q=0.5"},
                         timeout=20, allow_redirects=True)
    except Exception as e:
        return f"Could not fetch {url}: {e}"
    if r.status_code >= 400:
        return f"{url} answered HTTP {r.status_code}."
    ctype = r.headers.get("content-type", "").lower()
    if "pdf" in ctype or url.lower().endswith(".pdf"):
        return (f"{url} is a PDF ({len(r.content) // 1024} KB), not a web page. Save it with "
                f"browser_control or file tools, then read it with file_processor.")
    if "html" in ctype or "<html" in r.text[:500].lower():
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "form", "iframe"]):
            tag.decompose()
        title = (soup.title.get_text(strip=True) if soup.title else "") or url
        body = soup.find("main") or soup.find("article") or soup.body or soup
        text = body.get_text("\n", strip=True)
    else:
        title, text = url, r.text
    lines = [ln.strip() for ln in text.splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    start = max(0, int(start or 0))
    part = text[start:start + _PAGE_CHARS]
    if not part:
        return f"{title}\n(no readable text at offset {start}; the page has {len(text)} characters)"
    more = len(text) - (start + len(part))
    tail = (f"\n…[{more} more characters — call read_webpage with start={start + len(part)}]"
            if more > 0 else "")
    return f"TITLE: {title}\nURL: {r.url}\n\n{part}{tail}"


# ── module-level API, as the rest of the app uses it ──────────────────────────

_manager: Optional[AgentManager] = None
_mgr_lock = threading.Lock()


def manager() -> AgentManager:
    global _manager
    with _mgr_lock:
        if _manager is None:
            _manager = AgentManager()
        return _manager


def bind(**kwargs) -> None:
    """Wire the agent to the app. Called once from main.py at startup."""
    manager().bind(**kwargs)
