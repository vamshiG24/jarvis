"""
holo_control — handle the hologram by voice: turn it, zoom, spin, reset,
close, and switch webcam hand control on or off.
"""
from __future__ import annotations

_ACTIONS = ("rotate_left", "rotate_right", "rotate_up", "rotate_down", "zoom_in",
            "zoom_out", "spin_on", "spin_off", "reset", "close", "hands_on", "hands_off")


def holo_control(parameters: dict, player=None) -> str:
    action = str(parameters.get("action", "")).strip().lower()
    amount = parameters.get("amount")
    if player is None or not hasattr(player, "holo_command"):
        return "Holograms need the HUD, which is not available."
    if action not in _ACTIONS:
        return f"Unknown hologram action '{action}'. Use one of: {', '.join(_ACTIONS)}."
    if action.startswith("hands_"):
        return player.set_hand_control(action == "hands_on")
    if not player.hologram_active() and action != "close":
        return "There is no hologram on screen right now — show one with holo_show first."
    try:
        value = float(amount) if amount is not None else 0.0
    except (TypeError, ValueError):
        value = 0.0
    player.holo_command(action, value)
    return f"Done: {action.replace('_', ' ')}."


TOOL = {
    "name": "holo_control",
    "description": (
        "Control the hologram on screen: rotate it, zoom in/out, start/stop "
        "spinning, reset the view, or close it; and switch webcam hand "
        "control on or off (hands_on / hands_off). Use when the user says "
        "things like 'turn it', 'bigger', 'spin it', 'close the hologram', "
        "'let me use my hands'."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": " | ".join(_ACTIONS)},
            "amount": {"type": "NUMBER",
                       "description": "Optional: degrees for rotate_*, factor for zoom_* (e.g. 1.5)."},
        },
        "required": ["action"],
    },
    "handler": holo_control,
}
