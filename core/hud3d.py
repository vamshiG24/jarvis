"""
Stark HUD — a 3D J.A.R.V.I.S. interface in the style of the Iron Man films.

WHAT IT IS
    A small software 3D engine for one scene: a holographic projection table.
    A perspective camera looks down on a gyroscope of rings turning on
    different axes around an arc-reactor core; a grid floor recedes to the
    horizon with a projector beam rising from it; particles drift through the
    volume; an equalizer crown around the core is the actual audio; sonar
    pulses roll across the floor while the assistant speaks. Gauges, a
    heading tape and corner readouts sit in front of it in screen space.

WHY SOFTWARE
    Same rule as the rest of this app: no GPU, no OpenGL, nothing to install.
    numpy does the 3D (rotate + project a few thousand points), QPainter draws
    the result. Geometry is batched by depth so a whole ring costs three pen
    changes, not a hundred and forty. Measured at roughly 6–9 ms a frame.

WHAT MOVES MEANS SOMETHING
    Ring speed is the state (calm listening, fast amber thinking), the crown
    and the floor pulses are the voice, the core swells with it, the camera
    leans toward the mouse, and the colour is the state: cyan listening,
    white-hot speaking, gold thinking, red muted, amber recovering.
"""
from __future__ import annotations

import math
import time

import numpy as np
from PyQt6.QtCore import QLineF, QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QBrush, QColor, QFont, QLinearGradient, QPainter, QPen, QPixmap, QPolygonF,
    QRadialGradient,
)

TAU = math.tau


def _rot(rx: float, ry: float, rz: float) -> np.ndarray:
    """Rotation matrix applied as Rz · Ry · Rx (row vectors: P @ R.T)."""
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)
    Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]], dtype=np.float32)
    Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]], dtype=np.float32)
    Rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]], dtype=np.float32)
    return Rz @ Ry @ Rx


def _ease(x: float) -> float:
    x = max(0.0, min(1.0, x))
    return 1.0 - (1.0 - x) ** 3


def _qc(c: QColor, a: float) -> QColor:
    """Same colour, alpha as a 0–1 float."""
    return QColor(c.red(), c.green(), c.blue(), int(max(0.0, min(1.0, a)) * 255))


class _Ring:
    """One ring of the gyroscope."""

    def __init__(self, r, tilt, spin, pattern="solid", width=1.2, role="pri",
                 glow=False, ticks=0, precess=(0.0, 0.0, 0.0), n=96):
        self.r, self.tilt, self.spin_speed = r, tilt, spin
        self.pattern, self.width, self.role = pattern, width, role
        self.glow, self.ticks, self.precess = glow, ticks, precess
        self.theta = np.linspace(0.0, TAU, n + 1, dtype=np.float32)
        self.spin = float(np.random.default_rng(int(r * 1000)).random() * TAU)

    def mask(self, deg: np.ndarray, thinking: bool, head: float) -> np.ndarray:
        p = self.pattern
        if p == "dash":
            return (deg % 12.0) < 7.0
        if p == "seg":
            return (deg % 45.0) < 33.0
        if p == "long":
            return (deg % 120.0) < 96.0
        if p == "sparse":
            return (deg % 90.0) < 22.0
        if p == "chase":
            # A lit run that sweeps round the ring — fast while thinking.
            d = (deg - head) % 360.0
            return d < (140.0 if thinking else 70.0)
        return np.ones_like(deg, dtype=bool)


class StarkScene:
    """State + renderer. Call step() every tick, paint() when drawing."""

    D = 4.3          # camera distance (world units; the main ring has r = 1)
    FLOOR_Y = -1.18

    def __init__(self):
        rng = np.random.default_rng(3)
        self.t = 0.0
        self.boot_t0 = time.time()
        self.cam_yaw = 0.35
        self.yaw_off = 0.0
        self.pitch_off = 0.0
        self.amp = 0.0
        self.amp_hist = np.zeros(72, dtype=np.float32)
        self._hist_i = 0
        self._hist_acc = 0.0
        self.pulses: list[float] = []
        self._last_pulse = 0.0
        self.speed = 1.0

        # The gyroscope. Radii, tilts and patterns are chosen so no two rings
        # share a plane — that is what makes it read as a 3D object rather
        # than a target.
        self.rings = [
            _Ring(1.00, (0.00, 0.0, 0.00),  0.22, "seg",    2.2, "pri",  glow=True, ticks=72),
            _Ring(1.12, (0.18, 0.0, 0.10), -0.15, "dash",   1.0, "pri"),
            _Ring(0.86, (1.57, 0.0, 0.00),  0.35, "long",   1.6, "acc",  glow=True,
                  precess=(0.0, 0.11, 0.0)),
            _Ring(0.74, (1.05, 0.0, 0.55), -0.42, "chase",  2.0, "acc",
                  precess=(0.0, -0.07, 0.0)),
            _Ring(0.56, (0.00, 0.0, 0.00),  0.60, "sparse", 3.0, "pri"),
            _Ring(1.42, (0.00, 0.0, 0.00), -0.05, "solid",  0.8, "dim",  ticks=120),
            _Ring(1.27, (0.38, 0.0, -0.22), 0.09, "sparse", 1.4, "gold",
                  precess=(0.0, 0.04, 0.0)),
        ]

        # Particles in a shell around the table.
        n = 240
        self.p_r = rng.uniform(1.5, 3.8, n).astype(np.float32)
        self.p_a = rng.uniform(0, TAU, n).astype(np.float32)
        self.p_y = rng.uniform(-1.1, 1.7, n).astype(np.float32)
        self.p_s = rng.uniform(0.02, 0.09, n).astype(np.float32)

        # Floor grid: lines in x and z, sampled so they can fade with distance.
        L, step, k = 4.6, 0.575, 6
        ticks = np.arange(-L, L + 1e-6, step, dtype=np.float32)
        u = np.linspace(-L, L, k + 1, dtype=np.float32)
        segs = []
        for c in ticks:
            for a, b in zip(u[:-1], u[1:]):
                segs.append(((a, c), (b, c)))      # along x at z=c
                segs.append(((c, a), (c, b)))      # along z at x=c
        s = np.array(segs, dtype=np.float32)       # (M, 2, 2) as (x, z)
        self.grid_a = np.stack([s[:, 0, 0], np.full(len(s), self.FLOOR_Y), s[:, 0, 1]], 1)
        self.grid_b = np.stack([s[:, 1, 0], np.full(len(s), self.FLOOR_Y), s[:, 1, 1]], 1)
        mid = (s[:, 0] + s[:, 1]) / 2
        self.grid_fade = np.clip(1.0 - np.hypot(mid[:, 0], mid[:, 1]) / L, 0, 1) ** 1.4

        self.circle64 = np.linspace(0, TAU, 65, dtype=np.float32)
        self._bg_cache: QPixmap | None = None
        self._bg_key = None

    # ── time ────────────────────────────────────────────────────────────────

    def step(self, dt: float, amp: float, state: str, speaking: bool,
             muted: bool, mouse: tuple[float, float] | None) -> None:
        dt = max(0.0, min(0.1, dt))
        thinking = state in ("THINKING", "PROCESSING", "CONNECTING", "INITIALISING")
        sleeping = state == "SLEEPING"
        target = 1.0 + (2.3 if thinking else 0.0) + (0.9 if speaking else 0.0)
        if sleeping or muted:
            target = 0.35
        self.speed += (target - self.speed) * min(1.0, dt * 2.5)
        self.t += dt * self.speed
        self.amp = amp

        for ring in self.rings:
            ring.spin += ring.spin_speed * dt * self.speed

        # Slow orbit of the camera, plus a lean toward the mouse.
        self.cam_yaw += dt * 0.045 * (0.4 if sleeping else 1.0)
        mx, my = mouse if mouse else (0.0, 0.0)
        self.yaw_off += (mx * 0.32 - self.yaw_off) * min(1.0, dt * 3.0)
        self.pitch_off += (my * 0.16 - self.pitch_off) * min(1.0, dt * 3.0)

        # Audio history for the crown: ~30 samples a second.
        self._hist_acc += dt
        while self._hist_acc >= 1 / 30:
            self._hist_acc -= 1 / 30
            self.amp_hist[self._hist_i] = amp
            self._hist_i = (self._hist_i + 1) % self.amp_hist.size

        # Floor sonar: on voice peaks, and a slow idle heartbeat.
        now = self.t
        if (amp > 0.30 and now - self._last_pulse > 0.55) or now - self._last_pulse > 4.5:
            self.pulses.append(now)
            self._last_pulse = now
        self.pulses = [p for p in self.pulses if now - p < 3.2][-4:]

    def boot(self) -> float:
        return min(1.0, (time.time() - self.boot_t0) / 3.2)

    def booting(self) -> bool:
        """True while the boot sequence (and its 'online' fade) is on screen."""
        return time.time() - self.boot_t0 < 4.6

    # ── projection ──────────────────────────────────────────────────────────

    def _setup(self, cx: float, cy: float, unit_px: float) -> None:
        self.cx, self.cy = cx, cy
        self.f = unit_px * self.D
        yaw = self.cam_yaw + self.yaw_off
        pitch = 0.44 + self.pitch_off
        self.cyw, self.syw = math.cos(yaw), math.sin(yaw)
        self.cp, self.sp = math.cos(pitch), math.sin(pitch)
        self.yaw_deg = math.degrees(yaw) % 360.0

    def _proj(self, P: np.ndarray):
        x1 = P[:, 0] * self.cyw + P[:, 2] * self.syw
        z1 = -P[:, 0] * self.syw + P[:, 2] * self.cyw
        y2 = P[:, 1] * self.cp + z1 * self.sp
        z2 = -P[:, 1] * self.sp + z1 * self.cp
        zc = z2 + self.D
        zc_safe = np.maximum(zc, 0.25)
        return (self.cx + self.f * x1 / zc_safe,
                self.cy - self.f * y2 / zc_safe,
                zc)

    # ── drawing helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _lines(p: QPainter, xa, ya, xb, yb, idx) -> None:
        if len(idx):
            p.drawLines([QLineF(float(xa[i]), float(ya[i]), float(xb[i]), float(yb[i]))
                         for i in idx])

    def _draw_segments(self, p, xa, ya, xb, yb, depth, col, alpha, width,
                       on=None, glow=False):
        """Segments bucketed by depth: near = bright and thick, far = faint."""
        keep = np.ones(len(xa), dtype=bool) if on is None else on
        buckets = ((0.0, 0.34, 1.00, 1.15), (0.34, 0.67, 0.62, 0.95),
                   (0.67, 1.01, 0.30, 0.75))
        for lo, hi, ka, kw in buckets:
            idx = np.nonzero(keep & (depth >= lo) & (depth < hi))[0]
            if not len(idx):
                continue
            if glow:
                pen = QPen(_qc(col, alpha * ka * 0.20), width * kw * 4.0)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                p.setPen(pen)
                self._lines(p, xa, ya, xb, yb, idx)
            pen = QPen(_qc(col, alpha * ka), width * kw)
            pen.setCapStyle(Qt.PenCapStyle.FlatCap)
            p.setPen(pen)
            self._lines(p, xa, ya, xb, yb, idx)

    # ── the scene ───────────────────────────────────────────────────────────

    def paint(self, p: QPainter, full: QRectF, core: QRectF, pal: dict,
              name: str, telemetry: dict) -> None:
        """Draw the whole scene. `full` is the canvas, `core` the area the
        gyroscope should sit in (between the side panels)."""
        boot = self.boot()
        eb = _ease(boot)
        unit = min(core.width(), core.height()) * 0.27
        cx = core.center().x()
        cy = core.top() + core.height() * 0.45
        self._setup(cx, cy, unit)

        pri, acc, gold = pal["pri"], pal["acc"], pal["gold"]
        dim, white, bg = pal["dim"], pal["white"], pal["bg"]
        roles = {"pri": pri, "acc": acc, "gold": gold, "dim": dim}
        thinking = pal.get("thinking", False)
        speaking = pal.get("speaking", False)
        amp = self.amp

        # ── background: deep space + a pool of light under the table ─────
        # Cached: a full-window gradient costs more than everything on the
        # floor put together, and it only changes with size and theme.
        key = (int(full.width()), int(full.height()), int(cx), int(cy), int(unit),
               pri.name(), bg.name())
        if self._bg_cache is None or self._bg_key != key:
            pm = QPixmap(max(1, int(full.width())), max(1, int(full.height())))
            pm.fill(bg)
            bp = QPainter(pm)
            g = QRadialGradient(cx, cy + unit * 0.9, max(full.width(), full.height()) * 0.62)
            g.setColorAt(0.0, _qc(pri, 0.18))
            g.setColorAt(0.35, _qc(pal["deep"], 0.20))
            g.setColorAt(1.0, _qc(bg, 0.0))
            bp.fillRect(QRectF(0, 0, full.width(), full.height()), QBrush(g))
            bp.end()
            self._bg_cache, self._bg_key = pm, key
        p.drawPixmap(int(full.left()), int(full.top()), self._bg_cache)

        # ── floor grid ───────────────────────────────────────────────────
        # Faint, thin and everywhere: antialiasing here costs the most and
        # shows the least, so the floor is drawn without it.
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        ax, ay, az = self._proj(self.grid_a)
        bx, by, bz = self._proj(self.grid_b)
        vis = (az > 0.4) & (bz > 0.4)
        zmid = (az + bz) * 0.5
        depth = np.clip((zmid - (self.D - 4.6)) / 9.2, 0, 1)
        fade = self.grid_fade * (1.0 - depth * 0.6) * eb
        for lo, hi, a in ((0.55, 1.01, 0.30), (0.30, 0.55, 0.18), (0.10, 0.30, 0.09)):
            idx = np.nonzero(vis & (fade >= lo) & (fade < hi))[0]
            if len(idx):
                p.setPen(QPen(_qc(pri, a), 1.0))
                self._lines(p, ax, ay, bx, by, idx)

        # Floor rings: static radar circles + expanding sonar pulses.
        c64 = self.circle64
        def floor_circle(r, a, w):
            P = np.stack([np.cos(c64) * r, np.full_like(c64, self.FLOOR_Y),
                          np.sin(c64) * r], 1)
            sx, sy, zc = self._proj(P)
            if (zc > 0.4).all():
                p.setPen(QPen(_qc(pri, a), w))
                p.drawPolyline(QPolygonF([QPointF(float(x), float(y)) for x, y in zip(sx, sy)]))
        for r, a in ((0.45, 0.30), (1.0, 0.16), (1.7, 0.08)):
            floor_circle(r, a * eb, 1.0)
        for born in self.pulses:
            age = self.t - born
            r = 0.35 + age * 1.15
            a = max(0.0, 1.0 - age / 3.2) * (0.55 if speaking else 0.25)
            floor_circle(r, a * eb, 1.0)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # ── projector beam: a cone of light from the table to the core ──
        fb = np.array([[-0.30, self.FLOOR_Y, 0], [0.30, self.FLOOR_Y, 0],
                       [0, self.FLOOR_Y, 0], [0, -0.05, 0]], dtype=np.float32)
        bx_, by_, _bz = self._proj(fb)
        half_b = abs(bx_[1] - bx_[0]) / 2
        half_t = unit * 1.15
        beam = QPolygonF([QPointF(float(bx_[2]) - half_b, float(by_[2])),
                          QPointF(cx - half_t, float(by_[3])),
                          QPointF(cx + half_t, float(by_[3])),
                          QPointF(float(bx_[2]) + half_b, float(by_[2]))])
        lg = QLinearGradient(0, float(by_[2]), 0, float(by_[3]))
        lg.setColorAt(0.0, _qc(pri, 0.13 * eb))
        lg.setColorAt(0.55, _qc(pri, 0.05 * eb))
        lg.setColorAt(1.0, _qc(pri, 0.0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(lg))
        p.drawPolygon(beam)
        # Projector lens on the floor.
        rg = QRadialGradient(float(bx_[2]), float(by_[2]), half_b * 1.6)
        rg.setColorAt(0.0, _qc(white, 0.55 * eb))
        rg.setColorAt(0.3, _qc(pri, 0.35 * eb))
        rg.setColorAt(1.0, _qc(pri, 0.0))
        p.setBrush(QBrush(rg))
        p.drawEllipse(QPointF(float(bx_[2]), float(by_[2])), half_b * 1.6, half_b * 0.55)
        p.setBrush(Qt.BrushStyle.NoBrush)

        # ── particles ────────────────────────────────────────────────────
        ang = self.p_a + self.t * self.p_s
        P = np.stack([np.cos(ang) * self.p_r, self.p_y, np.sin(ang) * self.p_r], 1)
        sx, sy, zc = self._proj(P)
        d = np.clip((zc - (self.D - 3.8)) / 7.6, 0, 1)
        for lo, hi, a, w in ((0.0, 0.4, 0.75, 2.4), (0.4, 0.7, 0.45, 1.8), (0.7, 1.01, 0.22, 1.3)):
            idx = np.nonzero((zc > 0.4) & (d >= lo) & (d < hi))[0]
            if len(idx):
                pen = QPen(_qc(pri, a * eb), w)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                p.setPen(pen)
                p.drawPoints(QPolygonF([QPointF(float(sx[i]), float(sy[i])) for i in idx]))

        # ── equalizer crown: the voice, standing up round the core ──────
        n = self.amp_hist.size
        hist = np.roll(self.amp_hist, -self._hist_i)[::-1]          # newest first
        th = np.linspace(0, TAU, n, endpoint=False, dtype=np.float32) + self.t * 0.25
        r0, y0 = 0.84, -0.06
        h = 0.02 + hist * 0.30 + (0.012 * np.sin(self.t * 3 + th * 5) if not speaking else 0)
        A = np.stack([np.cos(th) * r0, np.full_like(th, y0), np.sin(th) * r0], 1)
        B = A.copy()
        B[:, 1] += h * eb
        ax, ay, az = self._proj(A)
        bx2, by2, bz2 = self._proj(B)
        dep = np.clip((az - (self.D - r0)) / (2 * r0), 0, 1)
        crown_col = white if speaking else (gold if thinking else pri)
        self._draw_segments(p, ax, ay, bx2, by2, dep, crown_col,
                            0.30 + 0.45 * min(1.0, amp * 2), 1.5)

        # ── the gyroscope ────────────────────────────────────────────────
        head = (self.t * (160.0 if thinking else 40.0)) % 360.0
        for i, ring in enumerate(self.rings):
            reveal = _ease(boot * 1.6 - i * 0.09)
            if reveal <= 0.0:
                continue
            tilt = np.array(ring.tilt, dtype=np.float32) + np.array(ring.precess, dtype=np.float32) * self.t
            R = _rot(*tilt)
            th = ring.theta + ring.spin
            local = np.stack([np.cos(th) * ring.r, np.zeros_like(th), np.sin(th) * ring.r], 1)
            if ring.role in ("pri", "acc") and live_swell(speaking, amp):
                local *= 1.0 + 0.05 * amp
            P = local @ R.T
            sx, sy, zc = self._proj(P)
            dep_pts = np.clip((zc - (self.D - ring.r)) / (2 * ring.r), 0, 1)
            dep = (dep_pts[:-1] + dep_pts[1:]) * 0.5
            deg = (np.degrees(ring.theta[:-1]) % 360.0)
            on = ring.mask(deg, thinking, head)
            on &= np.arange(len(deg)) < int(len(deg) * reveal)
            on &= (zc[:-1] > 0.4) & (zc[1:] > 0.4)
            col = roles.get(ring.role, pri)
            alpha = 0.95 if ring.role != "dim" else 0.55
            self._draw_segments(p, sx[:-1], sy[:-1], sx[1:], sy[1:], dep, col,
                                alpha * (0.6 + 0.4 * reveal), ring.width,
                                on=on, glow=ring.glow)
            if ring.ticks:
                tt = np.linspace(0, TAU, ring.ticks, endpoint=False, dtype=np.float32) + ring.spin
                major = (np.arange(ring.ticks) % 6) == 0
                rin = np.where(major, ring.r * 1.035, ring.r * 1.015)
                rout = np.where(major, ring.r * 1.085, ring.r * 1.045)
                Pa = np.stack([np.cos(tt) * rin, np.zeros_like(tt), np.sin(tt) * rin], 1) @ R.T
                Pb = np.stack([np.cos(tt) * rout, np.zeros_like(tt), np.sin(tt) * rout], 1) @ R.T
                tax, tay, taz = self._proj(Pa)
                tbx, tby, _ = self._proj(Pb)
                tdep = np.clip((taz - (self.D - ring.r)) / (2 * ring.r), 0, 1)
                ton = (np.arange(ring.ticks) < int(ring.ticks * reveal)) & (taz > 0.4)
                self._draw_segments(p, tax, tay, tbx, tby, tdep, col,
                                    0.7 * reveal, 1.1, on=ton)

        # Degree labels round the outer dial, front half only.
        if boot > 0.6:
            p.setFont(QFont("Consolas", max(6, int(unit * 0.045))))
            outer = self.rings[5]
            for k in range(12):
                a = TAU * k / 12 + outer.spin
                P = np.array([[math.cos(a) * 1.55, 0.0, math.sin(a) * 1.55]], dtype=np.float32)
                lx, ly, lz = self._proj(P)
                dd = (lz[0] - (self.D - 1.55)) / 3.1
                if dd > 0.72:
                    continue
                p.setPen(QPen(_qc(pri, 0.75 * (1 - dd))))
                p.drawText(QRectF(float(lx[0]) - 20, float(ly[0]) - 7, 40, 14),
                           Qt.AlignmentFlag.AlignCenter, f"{k * 30:03d}")

        # ── arc reactor core (billboard at the origin) ───────────────────
        ox, oy, oz = self._proj(np.zeros((1, 3), dtype=np.float32))
        ox, oy = float(ox[0]), float(oy[0])
        rr = self.f * 0.30 / float(oz[0]) * (0.4 + 0.6 * eb) * (1.0 + 0.10 * amp)
        self._reactor(p, ox, oy, rr, pal, amp, eb)

        # ── screen-space HUD ─────────────────────────────────────────────
        self._gauges(p, ox, oy, unit, pal, telemetry, eb, core)
        self._heading_tape(p, core, pal, eb)
        if self.booting():
            self._boot_text(p, ox, core.bottom() - 112, unit, pal, boot, name)

    def _reactor(self, p, ox, oy, rr, pal, amp, eb):
        pri, white, acc = pal["pri"], pal["white"], pal["acc"]
        hot = pal.get("speaking", False)
        core_col = white if hot else pri
        # Glow
        g = QRadialGradient(ox, oy, rr * 2.4)
        g.setColorAt(0.0, _qc(white, (0.70 + 0.3 * amp) * eb))
        g.setColorAt(0.18, _qc(core_col, 0.55 * eb))
        g.setColorAt(0.5, _qc(pri, 0.14 * eb))
        g.setColorAt(1.0, _qc(pri, 0.0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g))
        p.drawEllipse(QPointF(ox, oy), rr * 2.4, rr * 2.4)

        # Coil segments — ten trapezoids, the arc reactor's signature.
        rot = self.t * 0.35
        r1, r2 = rr * 0.62, rr * 0.95
        p.setBrush(QBrush(_qc(pri, 0.20 * eb)))
        p.setPen(QPen(_qc(core_col, 0.85 * eb), 1.3))
        for k in range(10):
            a0 = rot + TAU * k / 10 + 0.06
            a1 = rot + TAU * (k + 1) / 10 - 0.06
            poly = QPolygonF([
                QPointF(ox + math.cos(a0) * r1, oy + math.sin(a0) * r1),
                QPointF(ox + math.cos(a0) * r2, oy + math.sin(a0) * r2),
                QPointF(ox + math.cos(a1) * r2, oy + math.sin(a1) * r2),
                QPointF(ox + math.cos(a1) * r1, oy + math.sin(a1) * r1),
            ])
            p.drawPolygon(poly)
        p.setBrush(Qt.BrushStyle.NoBrush)
        for k_r, a, w in ((1.06, 0.8, 1.6), (0.55, 0.9, 1.4), (1.22, 0.35, 1.0)):
            p.setPen(QPen(_qc(core_col, a * eb), w))
            p.drawEllipse(QPointF(ox, oy), rr * k_r, rr * k_r)
        # Triangle element.
        tri = QPolygonF([QPointF(ox + math.cos(-rot * 0.5 + TAU * k / 3 - math.pi / 2) * rr * 0.42,
                                 oy + math.sin(-rot * 0.5 + TAU * k / 3 - math.pi / 2) * rr * 0.42)
                         for k in range(3)])
        p.setPen(QPen(_qc(white, 0.9 * eb), 1.6))
        p.setBrush(QBrush(_qc(acc if pal.get("thinking") else core_col, 0.25 * eb)))
        p.drawPolygon(tri)
        # Hot centre.
        c = QRadialGradient(ox, oy, rr * 0.42)
        c.setColorAt(0.0, _qc(white, 1.0 * eb))
        c.setColorAt(0.6, _qc(core_col, 0.5 * eb))
        c.setColorAt(1.0, _qc(core_col, 0.0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(c))
        p.drawEllipse(QPointF(ox, oy), rr * 0.42, rr * 0.42)
        p.setBrush(Qt.BrushStyle.NoBrush)

    def _gauges(self, p, ox, oy, unit, pal, tel, eb, core):
        """Two arc gauges flanking the core — CPU left, memory right."""
        pri, gold, red, dimc = pal["pri"], pal["gold"], pal["red"], pal["dim"]
        # Inside the core area, labels included — never under a side panel.
        R = min(unit * 1.95, core.width() / 2 - 78)
        if R < unit * 1.2:
            return
        box = QRectF(ox - R, oy - R, 2 * R, 2 * R)
        for side, key, label in ((-1, "cpu", "CPU"), (1, "mem", "MEM")):
            v = float(tel.get(key, 0.0) or 0.0) / 100.0
            start = 150 if side < 0 else -30          # degrees, Qt: CCW from 3 o'clock
            span = 60
            pen = QPen(_qc(dimc, 0.55 * eb), 5.0)
            pen.setCapStyle(Qt.PenCapStyle.FlatCap)
            p.setPen(pen)
            p.drawArc(box, int(start * 16), int(span * 16))
            col = red if v > 0.85 else (gold if v > 0.65 else pri)
            pen = QPen(_qc(col, 0.95 * eb), 5.0)
            pen.setCapStyle(Qt.PenCapStyle.FlatCap)
            p.setPen(pen)
            fill = span * max(0.0, min(1.0, v)) * eb
            # Fill from the bottom of each arc upwards.
            if side < 0:
                p.drawArc(box, int((start + span - fill) * 16), int(fill * 16))
            else:
                p.drawArc(box, int(start * 16), int(fill * 16))
            for k in range(7):                          # ticks
                a = math.radians(start + span * k / 6)
                c, s = math.cos(a), -math.sin(a)
                p.setPen(QPen(_qc(pri, 0.5 * eb), 1))
                p.drawLine(QLineF(ox + c * (R + 5), oy + s * (R + 5),
                                  ox + c * (R + 11), oy + s * (R + 11)))
            a = math.radians(start + span / 2)
            tx = ox + math.cos(a) * (R + 30)
            ty = oy - math.sin(a) * (R + 30)
            p.setFont(QFont("Bahnschrift", max(7, int(unit * 0.05)), QFont.Weight.DemiBold))
            p.setPen(QPen(_qc(col, 0.95 * eb)))
            p.drawText(QRectF(tx - 40, ty - 18, 80, 16), Qt.AlignmentFlag.AlignCenter, label)
            p.setFont(QFont("Bahnschrift", max(9, int(unit * 0.075)), QFont.Weight.Bold))
            p.drawText(QRectF(tx - 40, ty - 2, 80, 22), Qt.AlignmentFlag.AlignCenter,
                       f"{v * 100:.0f}%")

    def _heading_tape(self, p, core, pal, eb):
        """Helmet-style heading tape along the top, scrolling with the camera."""
        pri = pal["pri"]
        w = core.width() * 0.46
        x0 = core.center().x() - w / 2
        y = core.top() + 26
        p.setPen(QPen(_qc(pri, 0.35 * eb), 1))
        p.drawLine(QLineF(x0, y, x0 + w, y))
        span = 90.0
        first = math.floor((self.yaw_deg - span / 2) / 5) * 5
        p.setFont(QFont("Consolas", 7))
        d = first
        while d <= self.yaw_deg + span / 2:
            x = x0 + (d - (self.yaw_deg - span / 2)) / span * w
            edge = 1.0 - abs(x - (x0 + w / 2)) / (w / 2)
            major = int(d) % 15 == 0
            p.setPen(QPen(_qc(pri, (0.85 if major else 0.4) * edge * eb), 1))
            p.drawLine(QLineF(x, y, x, y - (8 if major else 4)))
            if major:
                p.drawText(QRectF(x - 16, y - 22, 32, 12), Qt.AlignmentFlag.AlignCenter,
                           f"{int(d) % 360:03d}")
            d += 5
        cxm = x0 + w / 2
        p.setBrush(QBrush(_qc(pal["gold"], 0.9 * eb)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPolygon(QPolygonF([QPointF(cxm - 5, y + 9), QPointF(cxm + 5, y + 9),
                                 QPointF(cxm, y + 3)]))
        p.setBrush(Qt.BrushStyle.NoBrush)

    def _boot_text(self, p, ox, y, unit, pal, boot, name):
        since = time.time() - self.boot_t0
        pri, white = pal["pri"], pal["white"]
        if boot < 1.0:
            txt = f"INITIALISING  {name}"
            sub = f"{int(boot * 100):3d}%   CORE · NEURAL NET · VOICE LINK"
            a = 1.0
        else:
            txt = "ALL SYSTEMS ONLINE"
            sub = f"{name}  ·  AT YOUR SERVICE"
            a = max(0.0, 1.0 - (since - 3.2) / 1.4)
        f = QFont("Bahnschrift", max(10, int(unit * 0.085)), QFont.Weight.Bold)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 4)
        p.setFont(f)
        p.setPen(QPen(_qc(white, a)))
        p.drawText(QRectF(ox - 300, y, 600, 30), Qt.AlignmentFlag.AlignCenter, txt)
        p.setFont(QFont("Consolas", max(7, int(unit * 0.05))))
        p.setPen(QPen(_qc(pri, a * 0.85)))
        p.drawText(QRectF(ox - 300, y + 28, 600, 18), Qt.AlignmentFlag.AlignCenter, sub)
        if boot < 1.0:
            bw = unit * 2.2
            p.setPen(QPen(_qc(pri, 0.4), 1))
            p.drawRect(QRectF(ox - bw / 2, y + 52, bw, 4))
            p.fillRect(QRectF(ox - bw / 2, y + 52, bw * boot, 4), _qc(pri, 0.9))


def live_swell(speaking: bool, amp: float) -> bool:
    return speaking and amp > 0.05
