"""
Holograms — 3D objects that are the answer to what you asked.

WHAT IT IS
    "Show me a water molecule", "the solar system", "a DNA helix", "a 3D
    chart of my week": the assistant asks Gemini to DESIGN the object as a
    small scene — spheres, boxes, cylinders, cones, rings, tori, lines and
    labels, optionally orbiting or spinning — and this module turns that
    description into geometry and draws it as a hologram you can grab: drag
    to rotate, right-drag to move, scroll to zoom, or use your hands through
    the webcam (core/hands.py). Real 3D files (.obj / .stl) load the same way.

HOW IT DRAWS
    Same rules as the Stark HUD: software QPainter, numpy for the 3D, no GPU.
    Every primitive becomes vertices + edges once, when the hologram is
    created; a frame is one rotation + projection of all vertices and a
    handful of batched drawLines calls bucketed by colour and depth. Spheres
    get a glowing radial-gradient body instead of thousands of filled
    triangles, which is both cheaper and closer to the films. Large meshes
    keep only their feature edges (creases and borders), the look of a CAD
    hologram, so a 200k-triangle file still draws in a few milliseconds.

SAFETY
    A model-written scene is data, never code: it is parsed field by field,
    every number clamped, every list capped, unknown types ignored.
"""
from __future__ import annotations

import math
import struct
import time
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QLineF, QPointF, QRectF, Qt
from PyQt6.QtGui import (QBrush, QColor, QFont, QFontMetricsF, QPainter, QPen, QPolygonF,
                         QRadialGradient)

TAU = math.tau
MAX_OBJECTS = 160
MAX_EDGES = 14000
PALETTE_DEFAULT = "#4fd8ff"


# ── small math helpers ──────────────────────────────────────────────────────

def _rot(rx: float, ry: float, rz: float) -> np.ndarray:
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)
    Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]], dtype=np.float32)
    Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]], dtype=np.float32)
    Rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]], dtype=np.float32)
    return Rz @ Ry @ Rx


def _axis_angle(axis, angle: float) -> np.ndarray:
    a = np.asarray(axis, dtype=np.float32)
    n = float(np.linalg.norm(a))
    if n < 1e-6:
        return np.eye(3, dtype=np.float32)
    x, y, z = a / n
    c, s, C = math.cos(angle), math.sin(angle), 1 - math.cos(angle)
    return np.array([[c + x * x * C, x * y * C - z * s, x * z * C + y * s],
                     [y * x * C + z * s, c + y * y * C, y * z * C - x * s],
                     [z * x * C - y * s, z * y * C + x * s, c + z * z * C]], dtype=np.float32)


def _frame_from(direction) -> np.ndarray:
    """A rotation taking +Y onto `direction` (for cylinders and cones)."""
    d = np.asarray(direction, dtype=np.float32)
    n = float(np.linalg.norm(d))
    if n < 1e-6:
        return np.eye(3, dtype=np.float32)
    d = d / n
    y = np.array([0, 1, 0], dtype=np.float32)
    v = np.cross(y, d)
    s = float(np.linalg.norm(v))
    c = float(np.dot(y, d))
    if s < 1e-6:
        return np.eye(3, dtype=np.float32) if c > 0 else np.diag([1, -1, -1]).astype(np.float32)
    return _axis_angle(v / s, math.atan2(s, c))


def _num(v, default=0.0, lo=-1e4, hi=1e4) -> float:
    try:
        f = float(v)
        if math.isfinite(f):
            return max(lo, min(hi, f))
    except (TypeError, ValueError):
        pass
    return default


def _vec(v, default=(0.0, 0.0, 0.0)) -> np.ndarray:
    if isinstance(v, (list, tuple)) and len(v) >= 3:
        return np.array([_num(v[0]), _num(v[1]), _num(v[2])], dtype=np.float32)
    return np.array(default, dtype=np.float32)


def _color(v, default=PALETTE_DEFAULT) -> str:
    s = str(v or "").strip()
    c = QColor(s)
    return c.name() if c.isValid() else default


# ── geometry ────────────────────────────────────────────────────────────────

class _Builder:
    """Accumulates vertices, edges, sphere bodies and labels."""

    def __init__(self):
        self.verts: list[np.ndarray] = []
        self.edges: list[np.ndarray] = []
        self.edge_col: list[np.ndarray] = []
        self.n = 0
        self.colors: list[str] = []
        self.spheres: list[tuple[int, float, int]] = []  # (centre vertex, radius, colour)
        self.labels: list[tuple[int, str, int]] = []     # (anchor vertex, text, colour)
        self.parts: list[dict] = []                      # animated vertex ranges

    def color(self, hexs: str) -> int:
        if hexs not in self.colors:
            self.colors.append(hexs)
        return self.colors.index(hexs)

    def add(self, V: np.ndarray, E: np.ndarray, col: int) -> int:
        start = self.n
        self.verts.append(V.astype(np.float32))
        if len(E):
            self.edges.append(E.astype(np.int32) + start)
            self.edge_col.append(np.full(len(E), col, dtype=np.int16))
        self.n += len(V)
        return start

    # primitives — every one returns (start, end) of its vertex range
    def sphere(self, c, r, col, segs=16, rings=9):
        lon = np.linspace(0, TAU, segs, endpoint=False)
        lat = np.linspace(-math.pi / 2, math.pi / 2, rings + 2)[1:-1]
        V = [c + r * np.array([math.cos(b) * math.cos(a), math.sin(b), math.cos(b) * math.sin(a)])
             for b in lat for a in lon]
        V += [c + np.array([0, r, 0]), c + np.array([0, -r, 0]), c]
        V = np.array(V, dtype=np.float32)
        E = []
        for i in range(len(lat)):
            for j in range(segs):
                E.append((i * segs + j, i * segs + (j + 1) % segs))      # latitude rings
                if i + 1 < len(lat):
                    E.append((i * segs + j, (i + 1) * segs + j))          # meridians
        top, bot = len(V) - 3, len(V) - 2
        for j in range(0, segs, 2):
            E.append(((len(lat) - 1) * segs + j, top))
            E.append((j, bot))
        start = self.add(V, np.array(E), col)
        self.spheres.append((start + len(V) - 1, float(r), col))
        return start, start + len(V)

    def box(self, c, size, rot, col):
        sx, sy, sz = (np.asarray(size, dtype=np.float32) / 2)
        V = np.array([[x, y, z] for x in (-sx, sx) for y in (-sy, sy) for z in (-sz, sz)], dtype=np.float32)
        V = V @ _rot(*rot).T + c
        E = np.array([(0, 1), (2, 3), (4, 5), (6, 7), (0, 2), (1, 3), (4, 6), (5, 7),
                      (0, 4), (1, 5), (2, 6), (3, 7)])
        s = self.add(V, E, col)
        return s, s + 8

    def _circle(self, c, r, R, segs):
        t = np.linspace(0, TAU, segs, endpoint=False)
        return (np.stack([np.cos(t) * r, np.zeros_like(t), np.sin(t) * r], 1) @ R.T + c).astype(np.float32)

    def cylinder(self, a, b, r, col, segs=14, cap_top=True):
        R = _frame_from(b - a)
        c0, c1 = self._circle(a, r, R, segs), self._circle(b, r, R, segs)
        V = np.concatenate([c0, c1])
        E = [(i, (i + 1) % segs) for i in range(segs)]
        if cap_top:
            E += [(segs + i, segs + (i + 1) % segs) for i in range(segs)]
        E += [(i, segs + i) for i in range(0, segs, 2)]
        s = self.add(V, np.array(E), col)
        return s, s + len(V)

    def cone(self, a, b, r, col, segs=14):
        R = _frame_from(b - a)
        V = np.concatenate([self._circle(a, r, R, segs), b[None, :]])
        E = [(i, (i + 1) % segs) for i in range(segs)] + [(i, segs) for i in range(0, segs, 2)]
        s = self.add(V, np.array(E), col)
        return s, s + len(V)

    def torus(self, c, R_, r, rot, col, segs=28, tube=8):
        M = _rot(*rot)
        V = []
        for i in range(segs):
            u = TAU * i / segs
            for j in range(tube):
                v = TAU * j / tube
                V.append([(R_ + r * math.cos(v)) * math.cos(u), r * math.sin(v),
                          (R_ + r * math.cos(v)) * math.sin(u)])
        V = np.array(V, dtype=np.float32) @ M.T + c
        E = []
        for i in range(segs):
            for j in range(tube):
                a = i * tube + j
                E.append((a, i * tube + (j + 1) % tube))
                if j % 2 == 0:
                    E.append((a, ((i + 1) % segs) * tube + j))
        s = self.add(V, np.array(E), col)
        return s, s + len(V)

    def ring(self, c, r, rot, col, segs=64):
        V = self._circle(c, r, _rot(*rot), segs)
        E = np.array([(i, (i + 1) % segs) for i in range(segs)])
        s = self.add(V, E, col)
        return s, s + segs

    def polyline(self, pts, col, closed=False):
        V = np.asarray(pts, dtype=np.float32)
        if len(V) < 2:
            return self.n, self.n
        E = [(i, i + 1) for i in range(len(V) - 1)] + ([(len(V) - 1, 0)] if closed else [])
        s = self.add(V, np.array(E), col)
        return s, s + len(V)

    def label(self, pos, text, col):
        s = self.add(np.asarray([pos], dtype=np.float32), np.zeros((0, 2)), col)
        self.labels.append((s, str(text)[:40], col))
        return s, s + 1


class HoloModel:
    """Finished geometry: vertices in a unit-sized model space."""

    def __init__(self, b: _Builder, title: str, caption: str, source: str):
        self.title, self.caption, self.source = title, caption, source
        self.colors = [QColor(c) for c in (b.colors or [PALETTE_DEFAULT])]
        V = np.concatenate(b.verts) if b.verts else np.zeros((1, 3), np.float32)
        E = np.concatenate(b.edges) if b.edges else np.zeros((0, 2), np.int32)
        EC = np.concatenate(b.edge_col) if b.edge_col else np.zeros(0, np.int16)
        # Normalise: centre the bounding box, fit the largest extent into 2.
        lo, hi = V.min(0), V.max(0)
        centre = (lo + hi) / 2
        span = float(max(1e-6, (hi - lo).max()))
        self.norm = 2.0 / span
        self.verts = ((V - centre) * self.norm).astype(np.float32)
        self.edges, self.edge_col = E, EC
        self.spheres = [(i, r * self.norm, c) for i, r, c in b.spheres]
        self.labels = b.labels
        self.parts = []
        for part in b.parts:
            p = dict(part)
            p["center"] = (np.asarray(p["center"], np.float32) - centre) * self.norm
            self.parts.append(p)
        self.n_objects = 0

    @property
    def size_text(self) -> str:
        """How big it is, in words: parts for a design, triangles for a file."""
        if self.source == "file":
            return self.caption.split(" · ")[0]
        return f"{self.n_objects} part{'' if self.n_objects == 1 else 's'}"

    def posed(self, t: float) -> np.ndarray:
        """Vertices at time t, with orbiting/spinning parts moved."""
        if not self.parts:
            return self.verts
        V = self.verts.copy()
        for p in self.parts:
            ang = TAU * t / p["period"] if p["period"] else 0.0
            R = _axis_angle(p["axis"], ang)
            s, e = p["range"]
            V[s:e] = (V[s:e] - p["center"]) @ R.T + p["center"]
        return V


# ── spec → model ────────────────────────────────────────────────────────────

def from_spec(spec: dict) -> HoloModel:
    """Build a hologram from a scene description (see SPEC_GUIDE)."""
    b = _Builder()
    objs = spec.get("objects") if isinstance(spec, dict) else None
    count = 0
    for o in (objs if isinstance(objs, list) else [])[:MAX_OBJECTS]:
        if not isinstance(o, dict):
            continue
        t = str(o.get("type", "")).lower().strip()
        col = b.color(_color(o.get("color")))
        pos = _vec(o.get("pos"))
        rot = _vec(o.get("rot"))
        r = _num(o.get("radius", 0.3), 0.3, 1e-4, 1e3)
        rng = None
        if t == "sphere":
            rng = b.sphere(pos, r, col)
        elif t == "box":
            size = _vec(o.get("size"), (0.5, 0.5, 0.5))
            rng = b.box(pos, np.abs(size) + 1e-4, rot, col)
        elif t in ("cylinder", "rod", "bond", "cone", "arrow"):
            a = _vec(o.get("from"), tuple(pos))
            bb = _vec(o.get("to"), tuple(pos + np.array([0, 1, 0], np.float32)))
            if t == "cone":
                rng = b.cone(a, bb, r, col)
            elif t == "arrow":
                tip = a + (bb - a) * 0.78
                s0, _ = b.cylinder(a, tip, r * 0.35, col)
                _, e1 = b.cone(tip, bb, r, col)
                rng = (s0, e1)
            else:
                rng = b.cylinder(a, bb, r, col)
        elif t == "torus":
            rng = b.torus(pos, r, _num(o.get("tube", r * 0.25), r * 0.25, 1e-4, 1e3), rot, col)
        elif t in ("ring", "orbit", "circle"):
            rng = b.ring(pos, r, rot, col)
        elif t in ("line", "path", "curve"):
            pts = [_vec(p) for p in (o.get("points") or [])[:400] if isinstance(p, (list, tuple))]
            rng = b.polyline(pts, col, bool(o.get("closed")))
        elif t in ("label", "text"):
            rng = b.label(pos, o.get("text", ""), col)
        else:
            continue
        count += 1
        if o.get("label") and t not in ("label", "text"):
            b.label(pos + np.array([0, r * 1.25 + 0.05, 0], np.float32), o["label"], col)
        orbit = o.get("orbit")
        if isinstance(orbit, dict) and rng and rng[1] > rng[0]:
            b.parts.append({"range": rng, "center": _vec(orbit.get("center")),
                            "axis": _vec(orbit.get("axis"), (0, 1, 0)),
                            "period": _num(orbit.get("period", 10), 10, 0.5, 600)})
        spin = o.get("spin")
        if spin and rng and rng[1] > rng[0]:
            b.parts.append({"range": rng, "center": pos, "axis": (0, 1, 0),
                            "period": _num(spin if not isinstance(spin, bool) else 6, 6, 0.5, 600)})
    m = HoloModel(b, str(spec.get("title") or "Hologram")[:60],
                  str(spec.get("caption") or "")[:140], "gemini")
    m.n_objects = count
    return m


SPEC_GUIDE = """Return ONE JSON object describing a 3D hologram that answers the request.
{
 "title": "short title",
 "caption": "one short line explaining what is shown",
 "objects": [ ... up to 120 objects ... ]
}
Object types (coordinates roughly within -1..1; y is UP; colours as "#rrggbb"):
 {"type":"sphere","pos":[x,y,z],"radius":r,"color":"#..","label":"optional"}
 {"type":"box","pos":[x,y,z],"size":[w,h,d],"rot":[rx,ry,rz],"color":"#.."}   (radians)
 {"type":"cylinder","from":[x,y,z],"to":[x,y,z],"radius":r,"color":"#.."}    (rods, bonds, bars)
 {"type":"cone","from":[base],"to":[tip],"radius":r,"color":"#.."}
 {"type":"arrow","from":[x,y,z],"to":[x,y,z],"radius":r,"color":"#.."}
 {"type":"torus","pos":[x,y,z],"radius":R,"tube":r,"rot":[rx,ry,rz],"color":"#.."}
 {"type":"ring","pos":[x,y,z],"radius":r,"rot":[rx,ry,rz],"color":"#.."}       (orbits, outlines)
 {"type":"line","points":[[x,y,z],...],"closed":false,"color":"#.."}           (curves, helices, graphs)
 {"type":"label","pos":[x,y,z],"text":"short text","color":"#.."}
Any object may also have "orbit":{"center":[x,y,z],"axis":[0,1,0],"period":seconds}
(it revolves around that point — planets, electrons) or "spin":seconds_per_turn.
Rules: be faithful to the real thing (correct structure, proportions, relative
positions, real colours where they exist); compose complex things from many
primitives; label the important parts; prefer 20-100 objects; a helix or curve
is a "line" with enough points to be smooth; data charts use "box" bars with
"label"s. Output ONLY the JSON."""


# ── mesh files ──────────────────────────────────────────────────────────────

def _load_obj(path: Path):
    V, F = [], []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if line.startswith("v "):
                p = line.split()
                V.append((float(p[1]), float(p[2]), float(p[3])))
            elif line.startswith("f "):
                idx = [int(x.split("/")[0]) for x in line.split()[1:]]
                idx = [i - 1 if i > 0 else len(V) + i for i in idx]
                for k in range(1, len(idx) - 1):
                    F.append((idx[0], idx[k], idx[k + 1]))
    return np.array(V, np.float32), np.array(F, np.int64)


def _load_stl(path: Path):
    data = path.read_bytes()
    if data[:5].lower() == b"solid" and b"facet" in data[:400]:
        pts = [tuple(map(float, l.split()[1:4])) for l in data.decode("utf-8", "ignore").splitlines()
               if l.strip().startswith("vertex")]
        V = np.array(pts, np.float32)
    else:
        n = struct.unpack("<I", data[80:84])[0]
        rec = np.frombuffer(data[84:84 + n * 50], dtype=np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")]))
        V = rec["v"].reshape(-1, 3).astype(np.float32)
    F = np.arange(len(V)).reshape(-1, 3)
    # Weld duplicate corners so edges are shared between triangles.
    Vr = np.round(V, 5)
    uniq, inv = np.unique(Vr, axis=0, return_inverse=True)
    return uniq.astype(np.float32), inv.reshape(-1)[F]


def from_mesh(path: str | Path, colour: str = PALETTE_DEFAULT) -> HoloModel:
    """A real 3D file as a hologram: feature edges (creases + borders) only."""
    p = Path(path)
    ext = p.suffix.lower()
    if ext == ".obj":
        V, F = _load_obj(p)
    elif ext == ".stl":
        V, F = _load_stl(p)
    else:
        raise ValueError(f"unsupported 3D file type: {ext} (use .obj or .stl)")
    if len(V) == 0 or len(F) == 0:
        raise ValueError("the file has no triangles")
    F = F[(F < len(V)).all(1)]
    # Unique edges and the faces on each side of them.
    e = np.sort(np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]]), axis=1)
    face_of = np.tile(np.arange(len(F)), 3)
    uniq, inv, counts = np.unique(e, axis=0, return_inverse=True, return_counts=True)
    inv = inv.reshape(-1)
    n = np.cross(V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]])
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
    order = np.argsort(inv, kind="stable")
    starts = np.searchsorted(inv[order], np.arange(len(uniq)))
    fa = face_of[order][starts]
    fb = face_of[order][np.minimum(starts + 1, len(order) - 1)]
    cosang = np.einsum("ij,ij->i", n[fa], n[fb])
    keep = (counts != 2) | (cosang < math.cos(math.radians(28)))
    E = uniq[keep]
    if len(E) < 200:                      # smooth object: show its real wireframe
        E = uniq
    if len(E) > MAX_EDGES:
        E = E[np.random.default_rng(1).choice(len(E), MAX_EDGES, replace=False)]
    b = _Builder()
    b.add(V, E, b.color(colour))
    m = HoloModel(b, p.stem.replace("_", " ").title()[:60],
                  f"{len(F):,} triangles · {p.name}", "file")
    m.n_objects = 1
    return m


# ── the live hologram: pose, interaction, drawing ───────────────────────────

class HoloView:
    """The hologram on screen and how it is held: rotation, position, scale.

    Rotation is a matrix, updated incrementally, so dragging always turns the
    object the way your hand moves on screen — whatever angle it is already
    at — the way a trackball does, not the way Euler angles do."""

    D = 4.2

    def __init__(self):
        self.model: HoloModel | None = None
        self.R = _rot(-0.35, 0.6, 0.0)
        self.scale = 1.0
        self.offset = np.zeros(2, np.float32)   # screen px
        self.t0 = time.time()
        self.opened_at = 0.0
        self.closing_at = 0.0
        self.auto_spin = True
        self.last_touch = 0.0
        self.request_id = 0

    @property
    def active(self) -> bool:
        return self.model is not None

    def show(self, model: HoloModel, request_id: int = 0) -> None:
        self.request_id = request_id
        self.model = model
        self.R = _rot(-0.35, 0.6, 0.0)
        self.scale = 1.0
        self.offset[:] = 0
        self.t0 = self.opened_at = time.time()
        self.closing_at = 0.0
        self.auto_spin = True

    def replace_model(self, model: HoloModel, request_id: int) -> bool:
        """Swap in a refined design of the SAME request, keeping the pose the
        user has set. Refused if that hologram is gone or was replaced."""
        if self.model is None or self.closing_at or request_id != self.request_id:
            return False
        self.model = model
        self.opened_at = time.time() - 0.25     # brief re-materialise
        return True

    def close(self) -> None:
        if self.model is not None and not self.closing_at:
            self.closing_at = time.time()

    # interaction — all in screen pixels / radians
    def touch(self):
        self.last_touch = time.time()

    def rotate(self, dyaw: float, dpitch: float) -> None:
        self.R = (_rot(dpitch, 0, 0) @ _rot(0, dyaw, 0) @ self.R).astype(np.float32)
        self.touch()

    def roll(self, d: float) -> None:
        self.R = (_rot(0, 0, d) @ self.R).astype(np.float32)
        self.touch()

    def move(self, dx: float, dy: float) -> None:
        self.offset += np.array([dx, dy], np.float32)
        self.touch()

    def zoom(self, factor: float) -> None:
        self.scale = max(0.2, min(6.0, self.scale * factor))
        self.touch()

    def reset(self) -> None:
        self.R = _rot(-0.35, 0.6, 0.0)
        self.scale = 1.0
        self.offset[:] = 0
        self.touch()

    def step(self, dt: float) -> None:
        if self.auto_spin and time.time() - self.last_touch > 2.5:
            self.R = (_rot(0, dt * 0.35, 0) @ self.R).astype(np.float32)
        if self.closing_at and time.time() - self.closing_at > 0.45:
            self.model = None
            self.closing_at = 0.0

    # The title block above and the HUD's status band below are kept clear;
    # the object lives in the stage between them.
    TITLE_BAND = 96
    STATUS_BAND = 124

    def stage(self, rect: QRectF) -> QRectF:
        return QRectF(rect.left(), rect.top() + self.TITLE_BAND, rect.width(),
                      max(120.0, rect.height() - self.TITLE_BAND - self.STATUS_BAND))

    def caption_width(self, rect: QRectF) -> float:
        """The centre column for the caption — clear of the corner readouts
        and the close chip."""
        return max(220.0, min(560.0, rect.width() - 350.0))

    def hit(self, x: float, y: float, rect: QRectF) -> bool:
        """Is screen point (x, y) on the hologram (roughly)?"""
        if not self.model:
            return False
        st = self.stage(rect)
        cx = st.center().x() + self.offset[0]
        cy = st.center().y() + self.offset[1]
        r = min(st.width(), st.height()) * 0.40 * self.scale
        return (x - cx) ** 2 + (y - cy) ** 2 <= (r * 1.15) ** 2

    # drawing
    def paint(self, p: QPainter, rect: QRectF, pri: QColor, white: QColor) -> None:
        m = self.model
        if m is None:
            return
        now = time.time()
        grow = min(1.0, (now - self.opened_at) / 0.6)
        grow = 1 - (1 - grow) ** 3
        if self.closing_at:
            grow *= max(0.0, 1.0 - (now - self.closing_at) / 0.45)
        if grow <= 0.01:
            return
        st = self.stage(rect)
        unit = min(st.width(), st.height()) * 0.40 * self.scale * grow
        cx = st.center().x() + float(self.offset[0])
        cy = st.center().y() + float(self.offset[1])
        f = unit * self.D

        V = m.posed(now - self.t0) @ self.R.T
        zc = V[:, 2] + self.D
        zs = np.maximum(zc, 0.3)
        sx = cx + f * V[:, 0] / zs
        sy = cy - f * V[:, 1] / zs
        depth = np.clip((zc - (self.D - 1.2)) / 2.4, 0, 1)

        # Projection glow behind the object.
        g = QRadialGradient(cx, cy, unit * 1.4)
        g.setColorAt(0.0, QColor(pri.red(), pri.green(), pri.blue(), int(55 * grow)))
        g.setColorAt(1.0, QColor(pri.red(), pri.green(), pri.blue(), 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g))
        p.drawEllipse(QPointF(cx, cy), unit * 1.4, unit * 1.4)

        # Sphere bodies: a lit, translucent orb each, back to front.
        for vi, r, ci in sorted(m.spheres, key=lambda s: -zc[s[0]]):
            rp = f * r / max(0.3, float(zc[vi]))
            if rp < 0.8:
                continue
            col = m.colors[ci]
            a = (0.55 - 0.3 * float(depth[vi])) * grow
            sg = QRadialGradient(float(sx[vi]) - rp * 0.35, float(sy[vi]) - rp * 0.35, rp * 1.4)
            sg.setColorAt(0.0, QColor(255, 255, 255, int(170 * a)))
            sg.setColorAt(0.35, QColor(col.red(), col.green(), col.blue(), int(200 * a)))
            sg.setColorAt(1.0, QColor(col.red(), col.green(), col.blue(), int(25 * a)))
            p.setBrush(QBrush(sg))
            p.drawEllipse(QPointF(float(sx[vi]), float(sy[vi])), rp, rp)
        p.setBrush(Qt.BrushStyle.NoBrush)

        # Edges, batched by colour and depth (near = bright and thick).
        E = m.edges
        if len(E):
            ok = (zc[E[:, 0]] > 0.3) & (zc[E[:, 1]] > 0.3)
            ed = (depth[E[:, 0]] + depth[E[:, 1]]) * 0.5
            for ci, col in enumerate(m.colors):
                cmask = ok & (m.edge_col == ci)
                for lo, hi, ka, kw in ((0.0, 0.4, 1.0, 1.6), (0.4, 0.7, 0.62, 1.2), (0.7, 1.01, 0.32, 0.9)):
                    idx = np.nonzero(cmask & (ed >= lo) & (ed < hi))[0]
                    if not len(idx):
                        continue
                    pen = QPen(QColor(col.red(), col.green(), col.blue(), int(235 * ka * grow)), kw)
                    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                    p.setPen(pen)
                    a, b2 = E[idx, 0], E[idx, 1]
                    p.drawLines([QLineF(float(sx[i]), float(sy[i]), float(sx[j]), float(sy[j]))
                                 for i, j in zip(a, b2)])

        # Labels with a short leader line.
        if m.labels and grow > 0.6:
            lf = QFont("Bahnschrift", 9, QFont.Weight.DemiBold)
            lm = QFontMetricsF(lf)
            p.setFont(lf)
            placed: list[QRectF] = []
            for vi, text, ci in sorted(m.labels, key=lambda l: zc[l[0]]):
                if zc[vi] <= 0.3:
                    continue
                x, y = float(sx[vi]), float(sy[vi])
                w = lm.horizontalAdvance(text) + 4
                spot = None
                # up-right, down-right, up-left, down-left of the point
                for lx, ly, ex, ey in ((x + 16, y - 22, x + 14, y - 12),
                                       (x + 16, y + 6, x + 14, y + 10),
                                       (x - 16 - w, y - 22, x - 14, y - 12),
                                       (x - 16 - w, y + 6, x - 14, y + 10)):
                    r = QRectF(lx, ly, w, 18)
                    if not any(r.intersects(q) for q in placed):
                        spot = (r, ex, ey)
                        break
                if spot is None:
                    continue                        # hidden behind a nearer label
                r, ex, ey = spot
                placed.append(r)
                a = int(255 * (1.0 - 0.55 * float(depth[vi])))
                col = m.colors[ci]
                p.setPen(QPen(QColor(col.red(), col.green(), col.blue(), a // 2), 1))
                p.drawLine(QLineF(x, y, ex, ey))
                p.setPen(QPen(QColor(white.red(), white.green(), white.blue(), a)))
                p.drawText(r, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)

        # Title card: the name, and the caption wrapped to at most two lines
        # in the centre column.
        cw = self.caption_width(rect)
        cl = rect.center().x() - cw / 2
        top = rect.top() + 40
        tf = QFont("Bahnschrift", 11, QFont.Weight.Bold)
        p.setFont(tf)
        p.setPen(QPen(QColor(white.red(), white.green(), white.blue(), int(240 * grow))))
        title = QFontMetricsF(tf).elidedText(f"◆  {m.title.upper()}",
                                             Qt.TextElideMode.ElideRight, cw + 120)
        p.drawText(QRectF(rect.left(), top, rect.width(), 20), Qt.AlignmentFlag.AlignCenter,
                   title)
        if m.caption:
            cf = QFont("Consolas", 8)
            fm = QFontMetricsF(cf)
            cap = fm.elidedText(m.caption, Qt.TextElideMode.ElideRight, cw * 1.85)
            p.setFont(cf)
            p.setPen(QPen(QColor(pri.red(), pri.green(), pri.blue(), int(210 * grow))))
            p.drawText(QRectF(cl, top + 22, cw, fm.lineSpacing() * 2 + 2),
                       Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop
                       | Qt.TextFlag.TextWordWrap, cap)
