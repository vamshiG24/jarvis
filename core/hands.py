"""
Hand control — handle holograms with your bare hands.

The webcam is read on this computer and every frame stays in memory. MediaPipe's
HandLandmarker (Google, Apache-2.0) finds 21 points on each hand, and
GestureMapper turns them into what the hologram should do:

    pinch (thumb + index tip) and move ........ rotate
    fist and move ............................. drag it around
    pinch with BOTH hands, pull apart ......... scale — twist them to roll

Nothing is recorded, saved or sent anywhere. Off by default: switched on from
CONTROLS → HAND CONTROL, or by asking ("let me use my hands").

MediaPipe is an optional install, because its wheel depends on
opencv-contrib-python and that would land on top of the opencv-python the rest
of the app uses (two packages, one `cv2`). INSTALL_HINT is the way that keeps
one OpenCV.
"""
from __future__ import annotations

import atexit
import math
import shutil
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
             "hand_landmarker/float16/latest/hand_landmarker.task")
MODEL_PATH = Path(__file__).resolve().parent.parent / "models" / "hand_landmarker.task"

INSTALL_HINT = ("pip install mediapipe --no-deps  then  pip install matplotlib absl-py "
                "flatbuffers  (--no-deps keeps your opencv-python)")

# Landmark indices (MediaPipe hand model).
WRIST, THUMB_TIP, INDEX_TIP, MIDDLE_MCP = 0, 4, 8, 9
_FINGERS = ((8, 6), (12, 10), (16, 14), (20, 18))      # (tip, pip) — index … pinky
_PALM = (0, 5, 9, 13, 17)

# Hands rarely reach the edge of the camera frame, so its central part maps to
# the whole HUD — a comfortable reach covers the whole hologram area.
_REACH = 0.12
# A hand whose palm is this low in the frame is resting — on the keyboard, on
# the desk, half out of view. It gets a dim cursor and drives nothing, so
# typing never drags the hologram. Raise the hand to take control.
_REST_Y = 0.84
# A gesture has to hold for this many frames (~150 ms) before it acts, so a
# hand passing through a pinch on its way somewhere does nothing.
_HOLD_FRAMES = 3


@dataclass
class Hand:
    x: float            # 0..1 across the HUD, mirrored like a mirror
    y: float            # 0..1 down the HUD
    pinch: bool
    fist: bool
    key: str            # "Left" / "Right" as MediaPipe sees it
    live: bool = True   # False while resting low in the frame


def _dist(a, b) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


def _reach(v: float) -> float:
    return min(1.0, max(0.0, (v - _REACH) / (1.0 - 2 * _REACH)))


class GestureMapper:
    """Successive frames of landmarks → hologram operations. No camera, no Qt.

    Operations:  ("rotate", dyaw, dpitch)  radians
                 ("move", dx, dy)          fractions of the HUD area
                 ("zoom", factor)
                 ("roll", radians)
    A gesture only produces movement from its second frame on, so starting
    one never makes the hologram jump."""

    ROT_GAIN = 5.5          # radians for a sweep across the whole frame
    DEAD = 0.002            # ignore tremor smaller than this (frame fraction)
    SMOOTH = 0.55           # weight of the newest position

    def __init__(self):
        self._mode = None
        self._prev = None
        self._cand = None
        self._cand_n = 0
        self._pinching: dict[str, bool] = {}
        self._pos: dict[str, tuple[float, float]] = {}

    def read(self, result) -> list[Hand]:
        hands = []
        seen = set()
        for i, lms in enumerate(result.hand_landmarks or []):
            try:
                key = result.handedness[i][0].category_name
            except Exception:
                key = str(i)
            if key in seen:                     # two hands read as the same side
                key = f"{key}{i}"
            seen.add(key)

            palm = _dist(lms[WRIST], lms[MIDDLE_MCP]) or 1e-6
            curled = sum(_dist(lms[t], lms[WRIST]) < _dist(lms[p], lms[WRIST])
                         for t, p in _FINGERS)
            fist = curled >= 4
            ratio = _dist(lms[THUMB_TIP], lms[INDEX_TIP]) / palm
            # Hysteresis: easy to hold a pinch, deliberate to start one.
            pinch = (not fist) and ratio < (0.45 if self._pinching.get(key) else 0.30)
            self._pinching[key] = pinch

            palm_y = sum(lms[j].y for j in _PALM) / len(_PALM)
            live = palm_y < _REST_Y
            if fist:
                px = sum(lms[j].x for j in _PALM) / len(_PALM)
                py = palm_y
            else:
                px = (lms[THUMB_TIP].x + lms[INDEX_TIP].x) / 2
                py = (lms[THUMB_TIP].y + lms[INDEX_TIP].y) / 2
            x, y = _reach(1.0 - px), _reach(py)
            if key in self._pos:
                ox, oy = self._pos[key]
                x = ox + (x - ox) * self.SMOOTH
                y = oy + (y - oy) * self.SMOOTH
            self._pos[key] = (x, y)
            hands.append(Hand(x, y, pinch and live, fist and live, key, live))
        for k in list(self._pos):
            if k not in seen:
                self._pos.pop(k, None)
                self._pinching.pop(k, None)
        return hands

    def update(self, hands: list[Hand]) -> list[tuple]:
        pinches = sorted((h for h in hands if h.pinch), key=lambda h: h.x)
        fists = [h for h in hands if h.fist]
        if len(pinches) >= 2:
            a, b = pinches[0], pinches[1]
            mode = "scale"
            cur = (math.hypot(b.x - a.x, b.y - a.y), math.atan2(b.y - a.y, b.x - a.x))
        elif len(pinches) == 1:
            mode, cur = "rotate", (pinches[0].x, pinches[0].y)
        elif fists:
            mode, cur = "move", (fists[0].x, fists[0].y)
        else:
            mode, cur = None, None

        # Debounce: a new gesture only takes over after holding for a moment.
        if mode != self._mode:
            if mode == self._cand:
                self._cand_n += 1
            else:
                self._cand, self._cand_n = mode, 1
            if mode is None or self._cand_n >= _HOLD_FRAMES:
                self._mode, self._prev = mode, cur
                self._cand, self._cand_n = None, 0
            return []
        self._cand, self._cand_n = None, 0

        ops: list[tuple] = []
        if mode is not None and self._prev is not None:
            if mode == "scale":
                if self._prev[0] > 0.03:
                    f = cur[0] / self._prev[0]
                    if abs(f - 1.0) > 0.004:
                        ops.append(("zoom", f))
                da = (cur[1] - self._prev[1] + math.pi) % (2 * math.pi) - math.pi
                if abs(da) > 0.004:
                    ops.append(("roll", da))
            else:
                dx, dy = cur[0] - self._prev[0], cur[1] - self._prev[1]
                if abs(dx) + abs(dy) > self.DEAD:
                    if mode == "rotate":
                        ops.append(("rotate", dx * self.ROT_GAIN, dy * self.ROT_GAIN))
                    else:
                        ops.append(("move", dx, dy))
        self._mode, self._prev = mode, cur
        return ops


def ensure_model() -> Path:
    """The hand model (~7.5 MB), downloaded the first time hand control is
    switched on and never before."""
    if MODEL_PATH.exists() and MODEL_PATH.stat().st_size > 1_000_000:
        return MODEL_PATH
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MODEL_PATH.with_suffix(".part")
    print("[Hands] Downloading the hand model (about 7.5 MB, first time only)…")
    with urllib.request.urlopen(MODEL_URL, timeout=30) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    if tmp.stat().st_size < 1_000_000:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("the hand model download came back incomplete")
    tmp.replace(MODEL_PATH)
    return MODEL_PATH


def _open_camera(cv2):
    """The same camera the rest of the app uses. Retries briefly, because the
    live camera panel may still be letting go of it."""
    try:
        from actions.screen_processor import _cv2_backend, _get_camera_index
        index, backend = _get_camera_index(), _cv2_backend()
    except Exception:
        index, backend = 0, getattr(cv2, "CAP_DSHOW", 0)
    deadline = time.monotonic() + 2.5
    while True:
        cap = cv2.VideoCapture(index, backend)
        if cap.isOpened():
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            return cap
        cap.release()
        if time.monotonic() > deadline:
            raise RuntimeError(f"the webcam (camera {index}) could not be opened — "
                               f"is another app using it?")
        time.sleep(0.3)


_active: "HandTracker | None" = None


def latest_frame():
    """The newest webcam frame (BGR) while hand control holds the camera, so
    a snapshot or the camera panel can share it instead of fighting for the
    device. None when hand control is off."""
    t = _active
    return t.latest_frame() if t is not None and t.running else None


class HandTracker:
    """Owns the webcam while hand control is on.

    on_frame(hands, ops) and on_state(running, message) are called from the
    tracker's own thread — the UI forwards them through Qt signals."""

    def __init__(self, on_frame, on_state):
        self._on_frame = on_frame
        self._on_state = on_state
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._latest = None
        self.ready = threading.Event()      # set once running, or once it failed
        self.error = ""
        self.fps = 0.0

    @property
    def running(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive() and not self._stop.is_set()

    def start(self) -> None:
        global _active
        with self._lock:
            if self.running:
                return
            self._stop.clear()
            self.ready.clear()
            self.error = ""
            _active = self
            self._thread = threading.Thread(target=self._run, daemon=True, name="hands")
            self._thread.start()

    def stop(self, wait: float = 0.0) -> None:
        self._stop.set()
        t = self._thread
        if wait and t is not None and t is not threading.current_thread():
            t.join(wait)

    def latest_frame(self):
        with self._lock:
            return None if self._latest is None else self._latest.copy()

    def _run(self) -> None:
        cap = landmarker = None
        try:
            try:
                import cv2
                import mediapipe as mp
                from mediapipe.tasks.python import BaseOptions, vision
            except ImportError as e:
                raise RuntimeError(f"MediaPipe is not installed ({e.name} missing). "
                                   f"Install it with: {INSTALL_HINT}") from None
            model = ensure_model()
            landmarker = vision.HandLandmarker.create_from_options(
                vision.HandLandmarkerOptions(
                    base_options=BaseOptions(model_asset_path=str(model)),
                    running_mode=vision.RunningMode.VIDEO,
                    num_hands=2,
                    min_hand_detection_confidence=0.6,
                    min_hand_presence_confidence=0.5,
                    min_tracking_confidence=0.5))
            cap = _open_camera(cv2)
            if self._stop.is_set():
                return              # switched off while starting — never announce "on"
            mapper = GestureMapper()
            self.ready.set()
            self._on_state(True, "")

            ts = 0
            misses = 0
            n, t_fps = 0, time.monotonic()
            while not self._stop.is_set():
                ok, frame = cap.read()
                if not ok or frame is None:
                    misses += 1
                    if misses > 60:
                        raise RuntimeError("the webcam stopped sending frames")
                    time.sleep(0.03)
                    continue
                misses = 0
                with self._lock:
                    self._latest = frame
                h, w = frame.shape[:2]
                small = frame if w <= 640 else cv2.resize(frame, (640, int(h * 640 / w)))
                rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
                ts = max(ts + 1, int(time.monotonic() * 1000))
                res = landmarker.detect_for_video(
                    mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), ts)
                hands = mapper.read(res)
                self._on_frame(hands, mapper.update(hands))
                n += 1
                now = time.monotonic()
                if now - t_fps >= 2.0:
                    first = self.fps == 0.0
                    self.fps = n / (now - t_fps)
                    n, t_fps = 0, now
                    if first:
                        print(f"[Hands] Tracking at {self.fps:.0f} fps.")
        except Exception as e:
            self.error = str(e) or type(e).__name__
            print(f"[Hands] {self.error}")
        finally:
            self._stop.set()
            self.ready.set()
            if cap is not None:
                cap.release()
            if landmarker is not None:
                try:
                    landmarker.close()
                except Exception:
                    pass
            with self._lock:
                self._latest = None
            try:
                self._on_frame([], [])
                self._on_state(False, self.error)
            except Exception:
                pass


@atexit.register
def _release_camera() -> None:
    t = _active
    if t is not None:
        t.stop(wait=1.0)
