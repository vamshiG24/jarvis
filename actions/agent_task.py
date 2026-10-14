"""
agent_task — hand long, multi-step work to the background agent.

The conversation model calls this and keeps talking; core/agent.py does the
work on its own thread with every tool the assistant has, shows each step in
the Tasks window, and reports back through the conversation when it needs the
user or is done. Every call here returns at once.
"""
from core import agent


def _since(t) -> str:
    secs = int(t.worked)
    return f"{secs // 60} min {secs % 60} s" if secs >= 60 else f"{secs} s"


def _truthy(v) -> bool:
    return v is True or str(v).strip().lower() in ("true", "yes", "1")


def agent_task(parameters: dict, player=None) -> str:
    action = str(parameters.get("action") or "start").strip().lower()
    task_id = str(parameters.get("task_id") or "").strip().lower()
    mgr = agent.manager()

    if action == "start":
        goal = str(parameters.get("goal") or "").strip()
        if len(goal) < 8:
            return "Give the agent the whole goal in one or two sentences, with every detail the user gave."
        busy = [t for t in mgr.tasks() if t.status in agent.ACTIVE]
        t = mgr.start(goal, allow_files=_truthy(parameters.get("save_file")))
        if busy:
            return (f"[AGENT_QUEUED] Task {t.id} is queued behind the task already running "
                    f"(\"{busy[0].goal[:80]}\") — one runs at a time. Tell the user that in ONE "
                    f"short sentence.")
        return (f"[AGENT_STARTED] Task {t.id} is now working in the background on: {goal}\n"
                f"Tell the user in ONE short sentence that you are on it and will report back; "
                f"the Tasks window shows every step. Do not wait for it, do not describe steps, "
                f"and do not call agent_task again unless the user asks about it.")

    if action == "status":
        t = mgr.get(task_id)
        if t is None:
            return "There is no background task." if not task_id else f"There is no task {task_id}."
        return ("Summarise this for the user in one or two sentences:\n" + mgr.describe(t))

    if action == "list":
        tasks = sorted(mgr.tasks(), key=lambda x: x.created, reverse=True)[:8]
        if not tasks:
            return "There are no background tasks."
        return "Background tasks, newest first:\n" + "\n".join(
            f"- {t.id} [{t.status}] {t.goal[:90]}"
            + (f" — worked {_since(t)}, {t.steps} steps" if t.started else "")
            for t in tasks)

    if action == "reply":
        msg = str(parameters.get("message") or "").strip()
        if not msg:
            return "Pass the user's answer as the message."
        t = mgr.reply(task_id, msg)
        if t is None:
            return "No background task is waiting for an answer right now."
        return (f"[AGENT_REPLY] Passed to task {t.id}; it carries on. Acknowledge in a few "
                f"words at most.")

    if action == "cancel":
        t = mgr.cancel(task_id)
        if t is None:
            return "No background task is running."
        return (f"Task {t.id} is being cancelled; it stops after the step it is on. "
                f"Confirm in one short sentence.")

    if action == "resume":
        t = mgr.resume(task_id)
        if t is None:
            return ("There is no stopped task to resume." if not task_id
                    else f"Task {task_id} is not stopped, or does not exist.")
        return (f"[AGENT_STARTED] Task {t.id} is resuming: {t.goal}\n"
                f"Tell the user in ONE short sentence. It picks up from its plan and notes.")

    if action == "save":
        where = str(parameters.get("message") or "").strip()
        text = ("Save the result to a file" + (f": {where}" if where else " on the Desktop")
                + ", then finish and name the path.")
        t = mgr.instruct(task_id, text, allow_files=True)
        if t is None:
            return "There is no task to save."
        return (f"[AGENT_SAVING] Task {t.id} will save its result to a file now. Confirm in "
                f"one short sentence.")

    if action == "show":
        if player is not None and hasattr(player, "show_agent_window"):
            player.show_agent_window()
            return "The Tasks window is open."
        return "The Tasks window is not available."

    return "Unknown action. Use start, status, reply, save, cancel, resume, list or show."


TOOL = {
    "name": "agent_task",
    "description": (
        "Your autonomous agent for long, multi-step or advanced work. It runs in the "
        "background with all of your tools — web search, reading pages, the browser, files, "
        "apps, code, the screen — while you keep talking, and reports back when it is done "
        "or needs the user. Use action='start' whenever a request takes several steps or more "
        "than about a minute: research and compare, plan something, organise files, build "
        "or fix a project, gather information from several sites, any job with 'and then'. "
        "Do NOT use it for a single quick action you can do with one of your own tools. "
        "It writes NO files unless save_file is true. Other actions: 'status' (how is the "
        "task going), 'reply' (the user's answer to a question the agent asked), 'save' "
        "(the user now asks for a task's result to be saved to a file; 'message' = where, "
        "if they said), 'cancel', 'resume' (continue a stopped or interrupted task), "
        "'list', 'show' (open the Tasks window)."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "start | status | reply | save | cancel | resume | list | show",
            },
            "goal": {
                "type": "STRING",
                "description": (
                    "For start: the complete goal in English with every detail the user "
                    "gave — what to do, where to save results, limits, preferences. The agent "
                    "cannot hear this conversation, so leave nothing implied. Add nothing "
                    "they did not ask for — never 'save a report' unless they said so."),
            },
            "save_file": {
                "type": "BOOLEAN",
                "description": (
                    "For start: true ONLY if the user's own words explicitly ask to save, "
                    "write or create a file, report or document. Never infer it from the "
                    "kind of task. Default false: the agent then writes nothing to disk and "
                    "reports by voice."),
            },
            "task_id": {
                "type": "STRING",
                "description": "Which task. Omit for the current or most recent one.",
            },
            "message": {
                "type": "STRING",
                "description": ("For reply: the user's answer, in English. For save: where "
                                "they want the file, if they said."),
            },
        },
        "required": ["action"],
    },
    "handler": agent_task,
}
