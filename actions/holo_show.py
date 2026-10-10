"""
holo_show — answer with a 3D hologram.

"Show me a water molecule", "the solar system", "a DNA helix", "show the
engine.stl on my desktop": the object opens as a hologram in its own window
(never over the HUD), and the user turns it with the mouse or their hands.

Two sources, both real:
  * a subject — Gemini DESIGNS the scene as structured JSON (core/holo3d.py
    SPEC_GUIDE): spheres, rods, rings, labels, orbiting parts. Parsed as data,
    clamped, never executed.
  * a file — a real .obj / .stl from the user's folders, drawn as a feature-
    edge hologram.
"""
from __future__ import annotations

import itertools
import json
import os
import threading
import time
from pathlib import Path

from core import gemini, holo3d, known_folders


_request_ids = itertools.count(1)


def _design(subject: str, tier: str = gemini.STRUCTURED, timeout_ms: int = 45_000) -> dict:
    """Ask Gemini to design the hologram. Returns the parsed scene."""
    from google.genai import types as gtypes

    cfg = gtypes.GenerateContentConfig(
        system_instruction=holo3d.SPEC_GUIDE,
        response_mime_type="application/json",
        temperature=0.4,
    )
    resp = gemini.call(f"Hologram request: {subject}", tier=tier,
                       config=cfg, timeout_ms=timeout_ms)
    if resp is None:
        raise RuntimeError("no Gemini model could design it right now")
    raw = (getattr(resp, "text", None) or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`").split("\n", 1)[-1]
    spec = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
    if not isinstance(spec, dict):
        raise ValueError("the design was not a scene")
    return spec


def _resolve_file(raw: str) -> Path:
    p = Path(os.path.expandvars(raw.strip().strip('"'))).expanduser()
    if not p.is_absolute():
        for base in (known_folders.desktop(), known_folders.downloads(),
                     known_folders.documents(), Path.home()):
            if (base / p).exists():
                p = base / p
                break
    return known_folders.remap_default(p)


def holo_show(parameters: dict, player=None) -> str:
    subject = str(parameters.get("subject", "")).strip()
    file_path = str(parameters.get("file_path", "")).strip()
    if player is None or not hasattr(player, "show_hologram"):
        return "Holograms need the HUD, which is not available."

    t0 = time.perf_counter()
    rid = next(_request_ids)
    try:
        if file_path:
            p = _resolve_file(file_path)
            if not p.exists():
                return (f"No 3D file at {p}. Find it first with file_controller "
                        f"(action='find', extension='.stl' or '.obj').")
            model = holo3d.from_mesh(p)
        elif subject:
            spec = _design(subject)
            model = holo3d.from_spec(spec)
            if model.n_objects == 0:
                return "The design came back empty — say it differently and I will try again."
        else:
            return "Say what to show — a subject, or a .obj/.stl file path."
    except Exception as e:
        return f"Could not build that hologram: {e}"

    player.show_hologram(model, rid)
    secs = time.perf_counter() - t0
    if subject and not file_path:
        # The quick design answers now; the rich one replaces it when ready,
        # keeping the user's angle and zoom. Dropped if the hologram has been
        # closed or replaced by then (the UI checks the request id).
        threading.Thread(target=_refine, args=(subject, rid, player, model.n_objects),
                         daemon=True, name="holo-refine").start()
    print(f"[Holo] {model.title!r}: {model.n_objects} parts, {len(model.edges)} edges "
          f"in {secs:.1f}s ({model.source})")
    hands = bool(getattr(player, "hands_active", lambda: False)())
    how = ("drag to rotate, right-drag to move, scroll to zoom"
           + (", or pinch and move with their hands" if hands else
              " (hand control can be switched on with holo_control action='hands_on')"))
    return (f"Hologram open in its own window: {model.title}. {model.caption} "
            f"({model.size_text}). The user can {how}. "
            f"Tell them in one or two sentences what they are looking at — the "
            f"interesting parts, not coordinates.")


# The rich models answer in 15–35 s when healthy. Past REFINE_DEADLINE_S the
# user has been looking at the quick design for long enough that swapping it
# would be a surprise, not an improvement.
REFINE_TIMEOUT_MS = 45_000
REFINE_DEADLINE_S = 100


def _refine(subject: str, rid: int, player, quick_parts: int) -> None:
    t0 = time.perf_counter()
    try:
        rich = holo3d.from_spec(_design(subject, gemini.STRUCTURED_RICH, REFINE_TIMEOUT_MS))
    except Exception as e:
        print(f"[Holo] refinement skipped: {e}")
        return
    took = time.perf_counter() - t0
    if took > REFINE_DEADLINE_S:
        print(f"[Holo] refinement ready after {took:.0f}s — too late to swap, kept the quick design")
        return
    if rich.n_objects <= quick_parts:
        return                                 # not actually richer — keep what is up
    if player.refine_hologram(rich, rid):
        print(f"[Holo] refined {rich.title!r}: {quick_parts} → {rich.n_objects} parts "
              f"in {time.perf_counter() - t0:.0f}s")


TOOL = {
    "name": "holo_show",
    "description": (
        "Show a 3D hologram in the HUD that the user can rotate, move and zoom "
        "— with the mouse or their hands. Use it whenever a 3D object would "
        "answer or illustrate what they asked: molecules, planets and orbits, "
        "anatomy, machines, buildings, geometry, a 3D chart of numbers, or "
        "when they ask to SEE something in 3D / as a hologram. Also shows a "
        "real 3D file (.obj or .stl) from their computer."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "subject": {
                "type": "STRING",
                "description": ("What to show, described precisely in English, "
                                "including any numbers or names that matter "
                                "(e.g. 'water molecule H2O with bond angle', "
                                "'inner solar system with orbits', 'bar chart of "
                                "CPU 23, RAM 71, disk 45 percent')."),
            },
            "file_path": {
                "type": "STRING",
                "description": "A .obj or .stl file to show instead of a subject.",
            },
        },
        "required": [],
    },
    "handler": holo_show,
}
