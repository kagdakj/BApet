"""A single desktop pet: translucent frameless window, behavior state machine, rendering."""

from __future__ import annotations

import logging
import math
import random
from typing import TYPE_CHECKING

import numpy as np
from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QBitmap, QColor, QCursor, QFont, QImage, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication, QWidget

from .config import PetState
from .models import LoadedModel
from .renderer import Frame

if TYPE_CHECKING:
    from .app import Controller

log = logging.getLogger(__name__)

BASE_PPU = 220.0  # logical pixels per model unit at 100% size (~240 px tall characters)
WALK_SPEED = 0.55  # model units per second at 100% speed
DRAG_THRESHOLD = 5
CROSSFADE = 0.25
GRAVITY = 2600.0  # px/s^2
TURN_SPEED = 7.0  # rad/s
MASK_INTERVAL = 0.12  # seconds between input-mask refreshes


def _wrap_angle(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi


class Pet(QWidget):
    def __init__(self, controller: "Controller", state: PetState) -> None:
        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.NoDropShadowWindowHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        if controller.settings.always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle("Desktop Pet")
        self.setMouseTracking(False)

        self.controller = controller
        self.state = state
        self.lm: LoadedModel | None = None
        self._acquired: str | None = None
        self.status_text = "Loading…"
        self.error: str | None = None

        # behavior
        self.mode = "loading"  # loading | idle | walk | drag | fall | react | custom
        self.anchor = QPointF(state.x, state.y)
        self.target = QPointF()
        self.idle_left = random.uniform(1.0, 3.0)
        self.idle_time = 0.0
        self.vy = 0.0
        self.yaw = 0.0
        self.target_yaw = 0.0

        # animation
        self.anim: int | None = None
        self.anim_time = 0.0
        self.anim_loop = True
        self.anim_speed = 1.0
        self.prev_pose: tuple[np.ndarray, dict] | None = None
        self.prev_anim: int | None = None
        self.fade = 1.0
        self.current_pose: tuple[np.ndarray, dict] | None = None

        # drag
        self._press_pos: QPoint | None = None
        self._press_anchor = QPointF()
        self._dragging = False

        # output
        self._frame: Frame | None = None
        self._frame_key = None
        self._image: QImage | None = None
        self._buffer: np.ndarray | None = None
        self._mask_age = MASK_INTERVAL
        self._mask_size = None

        self._place_initial()
        self._apply_geometry()

    # ------------------------------------------------------------------ model
    def set_model(self, file: str) -> None:
        self.release_model()
        self._acquired = file
        self.state.model = file
        self.error = None
        self.mode = "loading"
        self.status_text = "Loading…"
        self.prev_pose = None
        self.current_pose = None
        self._image = None
        self._buffer = None
        self._frame = None
        self.clearMask()
        lm = self.controller.manager.acquire(file)
        if lm is not None:
            self.on_model_ready(lm)
        self.update()

    def on_model_ready(self, lm: LoadedModel) -> None:
        self.lm = lm
        self.error = None
        self._frame_key = None
        if self.mode in ("loading", "custom", "react"):
            self.mode = "idle"
        self.play(lm.idle, loop=True, crossfade=False)
        self.idle_left = random.uniform(2.0, 5.0)
        if self.controller.settings.movement == "walk" and not self.state.still:
            self.mode = "fall"
            self.vy = 0.0

    def on_model_status(self, text: str) -> None:
        if self.lm is None:
            self.status_text = text
            self.update()

    def on_model_error(self, message: str) -> None:
        if self.lm is None:
            self.error = message
            self.status_text = "Failed to load — right-click to retry"
            self.update()

    def release_model(self) -> None:
        if self._acquired is not None:
            self.controller.manager.release(self._acquired)
            self._acquired = None
        self.lm = None
        self.anim = None

    @property
    def label(self) -> str:
        skin = self.controller.catalog.skin(self.state.model)
        return skin.label if skin else self.state.model.rsplit(".", 1)[0]

    # ------------------------------------------------------------------ animation
    def play(self, index: int | None, loop: bool = True, crossfade: bool = True, speed: float = 1.0) -> None:
        if self.lm is None:
            return
        if index is None:
            index = self.lm.idle
        if crossfade and self.current_pose is not None:
            self.prev_pose = self.current_pose
            self.prev_anim = self.anim
            self.fade = 0.0
        else:
            self.prev_pose = None
            self.prev_anim = None
            self.fade = 1.0
        if index != self.anim or not loop or not self.anim_loop:
            self.anim_time = 0.0
        self.anim = index
        self.anim_loop = loop
        self.anim_speed = speed
        self._frame_key = None

    def play_named(self, index: int) -> None:
        """Play an arbitrary animation once (from the context menu)."""
        self.mode = "custom"
        self.play(index, loop=False)

    def _pose(self) -> tuple[np.ndarray, dict]:
        model = self.lm.model
        if self.anim is None:
            rest = model.rest_pose()
            return rest.world[0], {n: w[0] for n, w in rest.weights.items()}
        world, weights = model.bake(self.anim).sample(self.anim_time, self.anim_loop)
        if self.prev_pose is not None and self.fade < 1.0:
            k = self.fade * self.fade * (3 - 2 * self.fade)
            pw, pweights = self.prev_pose
            world = pw + (world - pw) * np.float32(k)
            weights = dict(weights)
            for n, w in pweights.items():
                if n in weights and len(weights[n]) == len(w):
                    weights[n] = w + (weights[n] - w) * k
        return world, weights

    def _anim_finished(self) -> bool:
        if self.anim is None or self.anim_loop:
            return False
        return self.anim_time >= self.lm.model.animations[self.anim].duration

    # ------------------------------------------------------------------ geometry
    @property
    def ppu(self) -> float:
        return BASE_PPU * self.controller.settings.size * self.state.size

    def _compute_frame(self) -> Frame:
        s = self.controller.settings
        anims = {self.lm.idle, self.anim}
        if self.prev_pose is not None and self.fade < 1.0:
            anims.add(self.prev_anim)
        key = (tuple(sorted(a for a in anims if a is not None)), self.ppu, s.pitch_deg, s.outline_width)
        if key != self._frame_key or self._frame is None:
            radius, y_min, y_max = 0.0, 0.0, 0.0
            for a in anims:
                r, lo, hi = self.lm.anim_bounds(a)
                radius, y_min, y_max = max(radius, r), min(y_min, lo), max(y_max, hi)
            margin = int(math.ceil(s.outline_width if s.outline else 0)) + 3
            self._frame = Frame.fit(radius, y_min, y_max, self.ppu, math.radians(s.pitch_deg), margin)
            self._frame_key = key
        return self._frame

    def _window_rect(self) -> QRect:
        if self.lm is None or self._frame is None:
            w, h = 220, 44
            return QRect(int(self.anchor.x() - w / 2), int(self.anchor.y() - h), w, h)
        f = self._frame
        return QRect(int(round(self.anchor.x() - f.anchor_x)), int(round(self.anchor.y() - f.anchor_y)),
                     f.width, f.height)

    def _apply_geometry(self, rect: QRect | None = None) -> None:
        rect = rect or self._window_rect()
        if rect != self.geometry():
            if rect.size() != self.size():
                self._mask_age = MASK_INTERVAL  # refresh mask right away
            self.setGeometry(rect)

    def _screen_rect(self) -> QRect:
        screen = QApplication.screenAt(self.anchor.toPoint()) or self.screen() or QApplication.primaryScreen()
        return screen.availableGeometry()

    def _place_initial(self) -> None:
        if self.anchor.x() >= 0 and self.anchor.y() >= 0 and QApplication.screenAt(self.anchor.toPoint()):
            return
        area = QApplication.primaryScreen().availableGeometry()
        self.anchor = QPointF(
            random.uniform(area.left() + area.width() * 0.2, area.right() - area.width() * 0.2),
            area.bottom() - (0 if self.controller.settings.movement == "walk" else area.height() * 0.1),
        )

    def _bounds_for_anchor(self) -> tuple[float, float, float, float]:
        """Allowed range for the anchor so the visible body stays on screen."""
        area = self._screen_rect()
        f = self._frame
        half = (f.width / 2 if f else 60) * 0.5
        above = (f.anchor_y if f else 200) * 0.85
        return area.left() + half, area.right() - half, area.top() + above, float(area.bottom())

    # ------------------------------------------------------------------ behavior
    def tick(self, dt: float) -> None:
        if self.lm is None:
            self._apply_geometry()
            return
        s = self.controller.settings
        movement = "still" if (self.state.still or self.controller.paused) else s.movement
        speed_px = WALK_SPEED * self.ppu * s.speed

        if self.mode == "idle":
            self.idle_time += dt
            self.idle_left -= dt
            if self.idle_time > 1.2:
                self.target_yaw = 0.0
            if movement == "walk" and self._above_ground():
                self.mode, self.vy = "fall", 0.0
            elif self.idle_left <= 0:
                if movement != "still" and random.random() < 0.7:
                    self._start_walk(movement)
                else:
                    self.idle_left = random.uniform(3.0, 8.0)
        elif self.mode == "walk":
            if movement == "still":
                self._go_idle()
            else:
                d = self.target - self.anchor
                dist = math.hypot(d.x(), d.y())
                step = speed_px * dt
                if dist <= step or dist < 1:
                    self.anchor = QPointF(self.target)
                    self._go_idle()
                else:
                    self.anchor += d * (step / dist)
                    if movement == "walk":
                        self.target_yaw = math.copysign(math.radians(75), d.x())
                    else:
                        self.target_yaw = math.atan2(d.x(), d.y())
        elif self.mode == "fall":
            ground = self._bounds_for_anchor()[3]
            self.vy += GRAVITY * dt
            y = self.anchor.y() + self.vy * dt
            if y >= ground:
                y = ground
                self._go_idle()
            self.anchor = QPointF(self.anchor.x(), y)
        elif self.mode in ("react", "custom"):
            self.target_yaw = 0.0
            if self._anim_finished():
                self._go_idle()
        elif self.mode == "drag":
            self.target_yaw = 0.0

        # turn smoothly
        diff = _wrap_angle(self.target_yaw - self.yaw)
        max_turn = TURN_SPEED * dt
        self.yaw = _wrap_angle(self.yaw + max(-max_turn, min(max_turn, diff)))

        # advance animation
        self.anim_time += dt * self.anim_speed
        if self.fade < 1.0:
            self.fade = min(1.0, self.fade + dt / CROSSFADE)
            if self.fade >= 1.0:
                self.prev_pose = None
                self._frame_key = None

        self._compute_frame()
        self._apply_geometry()
        if self.isVisible():
            self._render()
            self._mask_age += dt
            if self._mask_age >= MASK_INTERVAL and not self._dragging:
                self._update_mask()

    def _above_ground(self) -> bool:
        return self.anchor.y() < self._bounds_for_anchor()[3] - 1

    def _go_idle(self) -> None:
        self.mode = "idle"
        self.idle_time = 0.0
        self.idle_left = random.uniform(2.5, 8.0)
        self.play(self.lm.idle, loop=True)

    def _start_walk(self, movement: str) -> None:
        x0, x1, y0, y1 = self._bounds_for_anchor()
        if x1 <= x0:
            return
        for _ in range(8):
            if movement == "walk":
                tx, ty = random.uniform(x0, x1), y1
            else:
                tx, ty = random.uniform(x0, x1), random.uniform(y0, y1)
            if math.hypot(tx - self.anchor.x(), ty - self.anchor.y()) > self.ppu * 0.4:
                break
        # Short trips are more natural than crossing the whole screen every time.
        max_trip = self.ppu * random.uniform(1.0, 4.0)
        d = QPointF(tx, ty) - self.anchor
        dist = math.hypot(d.x(), d.y())
        if dist > max_trip:
            d *= max_trip / dist
        self.target = self.anchor + d
        self.mode = "walk"
        self.play(self.lm.walk if self.lm.walk is not None else self.lm.idle, loop=True,
                  speed=self.controller.settings.speed)

    def react(self) -> None:
        if self.lm is None or self.lm.react is None:
            return
        self.mode = "react"
        self.play(self.lm.react, loop=False)

    # ------------------------------------------------------------------ rendering
    def _render(self) -> None:
        s = self.controller.settings
        world, weights = self._pose()
        self.current_pose = (world, weights)
        dpr = self.devicePixelRatioF()
        try:
            w, h, rgba = self.controller.renderer.render(
                self.lm.gpu, world, weights, self._frame, self.yaw, scale=dpr,
                outline_px=s.outline_width if s.outline else 0.0,
            )
        except Exception:  # noqa: BLE001
            log.exception("render failed")
            return
        self._buffer = rgba
        img = QImage(rgba.data, w, h, w * 4, QImage.Format.Format_RGBA8888_Premultiplied)
        img.setDevicePixelRatio(dpr)
        self._image = img
        self.update()

    def _update_mask(self) -> None:
        self._mask_age = 0.0
        if self._buffer is None or self._frame is None:
            self.clearMask()
            return
        lw, lh = self.width(), self.height()
        alpha = self._buffer[:, :, 3]
        h, w = alpha.shape
        ys = np.minimum((np.arange(lh) * h / max(lh, 1)).astype(int), h - 1)
        xs = np.minimum((np.arange(lw) * w / max(lw, 1)).astype(int), w - 1)
        solid = alpha[np.ix_(ys, xs)] > 16
        # Dilate by 2 px so edges stay clickable and anti-aliasing is not clipped.
        grown = solid.copy()
        for dy, dx in ((-2, 0), (2, 0), (0, -2), (0, 2), (-1, -1), (1, 1), (-1, 1), (1, -1)):
            grown |= np.roll(np.roll(solid, dy, axis=0), dx, axis=1)
        if not grown.any():
            grown[lh // 2, lw // 2] = True
        argb = np.where(grown, np.uint32(0xFF000000), np.uint32(0)).astype(np.uint32)
        img = QImage(argb.data, lw, lh, lw * 4, QImage.Format.Format_ARGB32)
        self.setMask(QBitmap.fromImage(img.createAlphaMask()))

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        p.fillRect(self.rect(), Qt.GlobalColor.transparent)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        if self.lm is not None and self._image is not None:
            p.drawImage(QPointF(0, 0), self._image)
        else:
            self._paint_status(p)
        p.end()

    def _paint_status(self, p: QPainter) -> None:
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(r, 10, 10)
        p.fillPath(path, QColor(20, 30, 50, 210))
        p.setPen(QPen(QColor(110, 180, 255) if not self.error else QColor(255, 120, 120), 1.5))
        p.drawPath(path)
        p.setPen(QColor(235, 240, 250))
        f = QFont(self.font())
        f.setPointSizeF(max(8.0, f.pointSizeF() * 0.9))
        p.setFont(f)
        name = self.label
        text = f"{name}\n{self.status_text}"
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)

    # ------------------------------------------------------------------ input
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.globalPosition().toPoint()
            self._press_anchor = QPointF(self.anchor)
            self._dragging = False
        elif event.button() == Qt.MouseButton.RightButton:
            self.controller.show_pet_menu(self, event.globalPosition().toPoint())

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._press_pos is None:
            return
        delta = event.globalPosition().toPoint() - self._press_pos
        if not self._dragging and delta.manhattanLength() >= DRAG_THRESHOLD:
            self._dragging = True
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            self.clearMask()  # keep receiving events even over transparent pixels
            if self.lm is not None:
                self.mode = "drag"
                self.play(self.lm.pickup, loop=True)
        if self._dragging:
            self.anchor = self._press_anchor + QPointF(delta)
            self._apply_geometry()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton or self._press_pos is None:
            return
        self._press_pos = None
        if self._dragging:
            self._dragging = False
            self.unsetCursor()
            self._mask_age = MASK_INTERVAL
            self.state.x, self.state.y = self.anchor.x(), self.anchor.y()
            self.controller.schedule_save()
            if self.lm is not None:
                if self.controller.settings.movement == "walk" and self._above_ground():
                    self.mode, self.vy = "fall", 0.0
                    self.play(self.lm.pickup, loop=True)
                else:
                    self._go_idle()
        elif self.lm is not None:
            self.react()
        elif self.error:
            self.controller.manager.retry(self.state.model)
            self.error = None
            self.status_text = "Retrying…"
            self.update()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self.lm is not None:
            self.react()

    def snapshot_position(self) -> None:
        self.state.x, self.state.y = self.anchor.x(), self.anchor.y()

    def cursor_inside(self) -> bool:
        return self.geometry().contains(QCursor.pos())
