from __future__ import annotations

import math
import os
from typing import List, Optional, Tuple

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QWheelEvent
from PySide6.QtWidgets import QApplication, QMenu, QScrollArea, QWidget

from core.models import (
    CENTER_SNAP_TOLERANCE,
    AutomationPoint,
    ProjectModel,
    TrackModel,
    TrackSegment,
)
from core.theme import Theme
from core.waveform_cache import WaveformRenderCache


class TimelineCanvas(QWidget):
    project_changed = Signal()
    track_selected = Signal(int)
    status_changed = Signal(str)
    mutation_started = Signal()
    playhead_clicked = Signal(float)
    files_dropped = Signal(list)
    audio_mix_changed = Signal()  # emitted when segments move cross-track — forces audio re-render

    RULER_HEIGHT = 38
    LEFT_PADDING = 14
    POINT_RADIUS = 5

    def __init__(self, project: ProjectModel, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.project = project
        self.px_per_second = 30.0
        self.track_height = 128
        self.dragging_point: Optional[Tuple[int, int]] = None
        self.dragging_playhead = False
        self.dragging_segment: Optional[Tuple[int, int]] = None
        self.dragging_segment_mode: Optional[str] = None
        self.pending_segment_hit: Optional[Tuple[int, int, str]] = None
        self.mouse_press_pos: Optional[QPointF] = None
        self.drag_origin_time: float = 0.0
        self.drag_origin_segment_start: Optional[float] = None
        self._cross_track_target: Optional[int] = None
        self._cross_track_valid: bool = False
        self._drag_origin_track: Optional[int] = None
        self._drag_origin_seg_snapshot: Optional[TrackSegment] = None
        self._mutating = False
        self.waveform_cache = WaveformRenderCache()
        self.waveform_mode = 'signed'
        self.segments_locked = False
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.setMinimumWidth(1200)
        self._update_minimum_size()

    def _is_track_locked(self, track_index: int) -> bool:
        """Check if a specific track's segments are locked."""
        if 0 <= track_index < len(self.project.tracks):
            return self.project.tracks[track_index].locked
        return True

    def set_segments_locked(self, locked: bool) -> None:
        """Global lock toggle — kept for backward compat."""
        self.segments_locked = bool(locked)
        self._update_hover_cursor()
        self.update()

    def find_nearest_peak(self, direction: int) -> Optional[float]:
        """Находит ближайший локальный максимум waveform относительно playhead.
        direction: +1 — вправо, -1 — влево. Возвращает время пика или None."""
        if not self.project.tracks:
            return None
        current = self.project.playhead_time
        min_gap = 0.25
        best_time: Optional[float] = None
        best_score = 0.0
        for track in self.project.tracks:
            if track.audio_data is None or track.audio_data.size == 0:
                continue
            import numpy as np
            mip = track.mipmaps.get(100)
            if isinstance(mip, dict):
                data = np.abs(mip['max'])
                ratio = track.sample_rate / 100.0
            else:
                raw = track.mipmaps.get(1)
                if raw is None:
                    continue
                step = max(1, track.sample_rate // 50)
                data = np.abs(raw[::step])
                ratio = track.sample_rate / step
            if data.size < 3:
                continue
            for seg in track.segments:
                seg_src_start = seg.source_start
                seg_src_end = seg.source_start + seg.duration
                i_start = max(1, int(seg_src_start * ratio))
                i_end = min(data.size - 1, int(seg_src_end * ratio))
                if i_end - i_start < 3:
                    continue
                window = data[i_start:i_end]
                threshold = float(np.percentile(window, 92)) if window.size > 10 else 0.2
                threshold = max(threshold, 0.08)
                for i in range(1, window.size - 1):
                    v = float(window[i])
                    if v < threshold:
                        continue
                    if v <= window[i - 1] or v < window[i + 1]:
                        continue
                    src_time = (i_start + i) / ratio
                    tl_time = seg.start + (src_time - seg_src_start)
                    gap = tl_time - current
                    if direction > 0 and gap <= min_gap:
                        continue
                    if direction < 0 and gap >= -min_gap:
                        continue
                    distance = abs(gap)
                    score = v / (1.0 + distance * 0.5)
                    if score > best_score:
                        best_score = score
                        best_time = tl_time
        return best_time

    # --------- Geometry ---------

    def set_track_height(self, value: int) -> None:
        self.track_height = max(88, min(260, value))
        self._update_minimum_size()
        self.update()

    def _update_minimum_size(self) -> None:
        content_w = int(max(1000, self.LEFT_PADDING * 2 + self.project.duration() * self.px_per_second + 120))
        if not self.project.tracks:
            # Без треков — заполняем всю доступную высоту, ruler не показываем
            self.setMinimumSize(content_w, 200)
        else:
            content_h = self.RULER_HEIGHT + len(self.project.tracks) * self.track_height
            self.setMinimumSize(content_w, content_h + 2)
        self.resize(self.minimumSize())

    def time_to_x(self, sec: float) -> float:
        return self.LEFT_PADDING + sec * self.px_per_second

    def x_to_time(self, x: float) -> float:
        return max(0.0, (x - self.LEFT_PADDING) / self.px_per_second)

    def track_rect(self, index: int) -> QRectF:
        top = self.RULER_HEIGHT + index * self.track_height
        return QRectF(0, top, self.width(), self.track_height)

    def automation_value_to_y(self, track_index: int, value: float) -> float:
        rect = self.track_rect(track_index).adjusted(0, 18, 0, -18)
        return rect.bottom() - value * rect.height()

    def y_to_automation_value(self, track_index: int, y: float) -> float:
        rect = self.track_rect(track_index).adjusted(0, 18, 0, -18)
        if rect.height() <= 0:
            return 0.5
        value = (rect.bottom() - y) / rect.height()
        if abs(value - 0.5) <= CENTER_SNAP_TOLERANCE:
            value = 0.5
        return max(0.0, min(1.0, value))

    def visible_x_range(self) -> Tuple[float, float]:
        scroll = self._find_scroll_area()
        if scroll is None:
            return 0.0, float(self.width())
        left = float(scroll.horizontalScrollBar().value())
        right = left + float(scroll.viewport().width())
        return left, min(float(self.width()), right)

    def visible_time_range(self, pad_seconds: float = 0.5) -> Tuple[float, float]:
        left, right = self.visible_x_range()
        start = max(0.0, self.x_to_time(left) - pad_seconds)
        end = max(start, self.x_to_time(right) + pad_seconds)
        return start, end

    def invalidate_waveform_cache(self, track_index: Optional[int] = None) -> None:
        if track_index is None:
            self.waveform_cache.clear()
            return
        if 0 <= track_index < len(self.project.tracks):
            self.waveform_cache.invalidate_track(self.project.tracks[track_index])

    def _visible_track_indexes(self) -> range:
        if not self.project.tracks:
            return range(0, 0)
        scroll = self._find_scroll_area()
        if scroll is None:
            return range(0, len(self.project.tracks))
        top = scroll.verticalScrollBar().value()
        bottom = top + scroll.viewport().height()
        first = max(0, int(max(0, top - self.RULER_HEIGHT) // self.track_height))
        last = min(len(self.project.tracks) - 1, int(max(0, bottom - self.RULER_HEIGHT) // self.track_height) + 1)
        return range(first, last + 1)

    # --------- Painting ---------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.Antialiasing)
            painter.fillRect(self.rect(), Theme.BG)

            # Если треков нет — показываем только сетку и подсказку, без линейки и playhead
            if not self.project.tracks:
                self._draw_empty_state(painter)
                return

            self._draw_ruler(painter)
            for i, track in enumerate(self.project.tracks):
                self._draw_track(painter, i, track)
            self._draw_background_pattern(painter)
            self._draw_playhead(painter)
        finally:
            painter.end()

    def _draw_empty_state(self, painter: QPainter) -> None:
        """Пустой канвас: едва заметная сетка + подсказка drag & drop по центру."""
        rect = self.rect()
        # Тёмный фон
        painter.fillRect(rect, QColor('#15181E'))

        # Еле видимая сетка
        minor = 24
        major = minor * 4
        painter.setPen(QPen(QColor(255, 255, 255, 8), 1))
        for x in range(0, rect.width(), minor):
            painter.drawLine(x, 0, x, rect.height())
        for y in range(0, rect.height(), minor):
            painter.drawLine(0, y, rect.width(), y)
        painter.setPen(QPen(QColor(255, 255, 255, 14), 1))
        for x in range(0, rect.width(), major):
            painter.drawLine(x, 0, x, rect.height())
        for y in range(0, rect.height(), major):
            painter.drawLine(0, y, rect.width(), y)

        # Подсказка по центру
        scroll = self._find_scroll_area()
        if scroll is not None:
            viewport = scroll.viewport()
            vx = float(scroll.horizontalScrollBar().value())
            vy = float(scroll.verticalScrollBar().value())
            cx = vx + viewport.width() / 2.0
            cy = vy + viewport.height() / 2.0
        else:
            cx = rect.width() / 2.0
            cy = rect.height() / 2.0

        # Мягкий светящийся бейдж
        badge_w, badge_h = 420.0, 120.0
        badge_rect = QRectF(cx - badge_w / 2.0, cy - badge_h / 2.0, badge_w, badge_h)
        painter.setPen(QPen(QColor(255, 138, 61, 40), 1))
        painter.setBrush(QColor(32, 36, 44, 180))
        painter.drawRoundedRect(badge_rect, 16, 16)

        # Крупный заголовок
        painter.setPen(QColor('#EAECEF'))
        f = painter.font()
        f.setPixelSize(20)
        f.setBold(True)
        painter.setFont(f)
        title_rect = QRectF(badge_rect.left(), badge_rect.top() + 22, badge_rect.width(), 28)
        painter.drawText(title_rect, Qt.AlignCenter, 'Drag & drop audio files here')

        # Подпись мельче
        painter.setPen(QColor('#9BA6B2'))
        f2 = painter.font()
        f2.setPixelSize(12)
        f2.setBold(False)
        painter.setFont(f2)
        sub_rect = QRectF(badge_rect.left(), badge_rect.top() + 56, badge_rect.width(), 20)
        painter.drawText(sub_rect, Qt.AlignCenter, 'or use File → Open Tracks… (Ctrl+O)')

        # Акцентная полоска снизу бейджа
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(255, 138, 61, 180))
        painter.drawRoundedRect(QRectF(cx - 24, badge_rect.bottom() - 16, 48, 3), 1.5, 1.5)

    def _draw_background_pattern(self, painter: QPainter) -> None:
        start_y = self.RULER_HEIGHT if not self.project.tracks else self.RULER_HEIGHT + len(self.project.tracks) * self.track_height
        if start_y >= self.height():
            return
        minor = 24
        major = minor * 4
        clip = QRectF(0, start_y, self.width(), self.height() - start_y)
        painter.save()
        painter.setClipRect(clip)
        painter.fillRect(clip, QColor('#15181E'))
        painter.setPen(QPen(QColor('#20252E'), 1))
        for x in range(0, self.width(), minor):
            painter.drawLine(x, int(start_y), x, self.height())
        for y in range(int(start_y), self.height(), minor):
            painter.drawLine(0, y, self.width(), y)
        painter.setPen(QPen(QColor('#2A303B'), 1))
        for x in range(0, self.width(), major):
            painter.drawLine(x, int(start_y), x, self.height())
        for y in range(int(start_y), self.height(), major):
            painter.drawLine(0, y, self.width(), y)
        painter.restore()

    def _draw_ruler(self, painter: QPainter) -> None:
        painter.fillRect(QRectF(0, 0, self.width(), self.RULER_HEIGHT), Theme.PANEL_ALT)
        painter.setPen(QPen(Theme.GRID, 1))
        painter.drawLine(0, self.RULER_HEIGHT - 1, self.width(), self.RULER_HEIGHT - 1)
        duration = max(1.0, self.project.duration())
        step = self._pick_time_step()
        visible_start, visible_end = self.visible_time_range(step)
        t = math.floor(visible_start / step) * step
        while t <= min(duration + step, visible_end + step):
            x = self.time_to_x(t)
            painter.setPen(QPen(Theme.GRID, 1))
            painter.drawLine(int(x), self.RULER_HEIGHT - 14, int(x), self.RULER_HEIGHT)
            painter.setPen(Theme.TEXT_DIM)
            painter.drawText(QRectF(x + 4, 6, 86, 16), self._format_time(t))
            t += step

    def _pick_time_step(self) -> float:
        if self.px_per_second >= 240:
            return 0.25
        if self.px_per_second >= 150:
            return 0.5
        if self.px_per_second >= 80:
            return 1.0
        return 2.0

    @staticmethod
    def _format_time(seconds: float) -> str:
        seconds = max(0.0, seconds)
        mins = int(seconds // 60)
        secs = int(seconds % 60)
        millis = int((seconds - int(seconds)) * 1000)
        return f'{mins:02d}:{secs:02d}.{millis:03d}'

    def _draw_track(self, painter: QPainter, index: int, track: TrackModel) -> None:
        rect = self.track_rect(index)
        bg = Theme.TRACK_BG_SOLO if track.solo else (
            Theme.TRACK_BG_SELECTED if self.project.selected_track == index else Theme.TRACK_BG
        )
        painter.fillRect(rect, bg)
        painter.setPen(QPen(Theme.GRID_SOFT, 1))
        painter.drawLine(int(rect.left()), int(rect.bottom()), int(rect.right()), int(rect.bottom()))
        self._draw_vertical_grid(painter, rect)
        self._draw_segments(painter, rect, track, index)
        self._draw_waveform(painter, rect, track, index)
        self._draw_automation_line(painter, index, track)
        self._draw_automation_points(painter, index, track)

    def _draw_vertical_grid(self, painter: QPainter, rect: QRectF) -> None:
        step = self._pick_time_step()
        duration = max(1.0, self.project.duration())
        visible_start, visible_end = self.visible_time_range(step)
        t = math.floor(visible_start / step) * step
        while t <= min(duration + step, visible_end + step):
            x = self.time_to_x(t)
            painter.setPen(QPen(Theme.GRID_SOFT, 1))
            painter.drawLine(int(x), int(rect.top()), int(x), int(rect.bottom()))
            t += step

    def _draw_segments(self, painter: QPainter, rect: QRectF, track: TrackModel, track_index: int) -> None:
        lane = rect.adjusted(8, 12, -8, -12)
        selected_set = set(self.project.selected_segments)
        track_selected = (
            self.project.selected_track == track_index
            and self.project.selected_segment is None
            and not selected_set
        )
        visible_left, visible_right = self.visible_x_range()

        # Check if this segment is being dragged to another track — dim it
        dragging_away = (
            self.dragging_segment
            and self.dragging_segment_mode == 'body'
            and self._drag_origin_track == track_index
            and self._cross_track_target is not None
            and self._cross_track_target != track_index
        )

        for seg_index, seg in enumerate(track.segments):
            x1 = self.time_to_x(seg.start)
            x2 = self.time_to_x(seg.end)
            if x2 < visible_left - 32 or x1 > visible_right + 32:
                continue
            seg_rect = QRectF(x1, lane.top(), max(1.0, x2 - x1), lane.height())
            selected = self.project.selected_segment == (track_index, seg_index) or (track_index, seg_index) in selected_set

            # If this segment is being dragged away, draw it dimmed with dashed border
            is_being_dragged = (
                dragging_away
                and self._drag_origin_seg_snapshot is not None
                and math.isclose(seg.source_start, self._drag_origin_seg_snapshot.source_start, abs_tol=1e-4)
            )
            if is_being_dragged:
                fill = QColor(255, 255, 255, 6)
                border = QColor(255, 255, 255, 30)
                painter.setBrush(fill)
                painter.setPen(QPen(border, 1.0, Qt.DashLine))
            else:
                fill = QColor(255, 138, 61, 55) if selected else (
                    QColor(255, 255, 255, 18) if track_selected else QColor(255, 255, 255, 12)
                )
                border = Theme.SEGMENT_SELECTED if selected else (
                    QColor('#FFB27C') if track_selected else QColor(255, 255, 255, 18)
                )
                painter.setBrush(fill)
                painter.setPen(QPen(border, 1.4))
            painter.drawRoundedRect(seg_rect, 6, 6)

        # Draw cross-track ghost preview on target track
        if (
            self._cross_track_target == track_index
            and self._drag_origin_seg_snapshot is not None
            and self.dragging_segment
            and self.dragging_segment_mode == 'body'
        ):
            snap = self._drag_origin_seg_snapshot
            seg_dur = snap.end - snap.start
            mouse_pos = self.mapFromGlobal(self.cursor().pos())
            desired_start = max(0.0, self.x_to_time(mouse_pos.x()) - seg_dur / 2.0)
            gx1 = self.time_to_x(desired_start)
            gx2 = self.time_to_x(desired_start + seg_dur)
            ghost_rect = QRectF(gx1, lane.top(), max(1.0, gx2 - gx1), lane.height())
            if self._cross_track_valid:
                fill = QColor(61, 255, 138, 35)
                border = QColor(61, 255, 138, 120)
            else:
                fill = QColor(255, 61, 61, 35)
                border = QColor(255, 61, 61, 120)
            painter.setBrush(fill)
            painter.setPen(QPen(border, 1.8, Qt.DashLine))
            painter.drawRoundedRect(ghost_rect, 6, 6)

        # Draw selected gap highlight
        gap = self.project.selected_gap
        if gap is not None and gap[0] == track_index:
            _, left_idx, right_idx = gap
            segs = track.segments
            gap_start = segs[left_idx].end if left_idx >= 0 and left_idx < len(segs) else 0.0
            gap_end = segs[right_idx].start if right_idx >= 0 and right_idx < len(segs) else (
                track.duration if track.duration > 0 else self.project.duration()
            )
            if gap_end > gap_start:
                gx1 = self.time_to_x(gap_start)
                gx2 = self.time_to_x(gap_end)
                gap_rect = QRectF(gx1, lane.top(), max(1.0, gx2 - gx1), lane.height())
                painter.setBrush(QColor(255, 138, 61, 20))
                painter.setPen(QPen(QColor(255, 138, 61, 80), 1.0, Qt.DashLine))
                painter.drawRoundedRect(gap_rect, 6, 6)

    def _draw_waveform(self, painter: QPainter, rect: QRectF, track: TrackModel, track_index: int) -> None:
        del track_index
        lane = rect.adjusted(12, 18, -12, -18)
        visible_left, visible_right = self.visible_x_range()
        painter.save()
        painter.setClipRect(lane)
        color = QColor(210, 225, 245, 170)
        for seg in track.segments:
            # Skip if no audio source at all
            audio = seg.effective_audio(track)
            if audio is None or audio.size == 0:
                continue
            seg_left = self.time_to_x(seg.start)
            seg_right = self.time_to_x(seg.end)
            if seg_right < visible_left - 32 or seg_left > visible_right + 32:
                continue
            self._draw_segment_waveform(painter, lane, track, seg, color)
        painter.restore()

    def _draw_segment_waveform(self, painter: QPainter, lane: QRectF, track: TrackModel, seg: TrackSegment, color: QColor) -> None:
        visible_left, visible_right = self.visible_x_range()
        tl_start = max(seg.start, self.x_to_time(visible_left - 50))
        tl_end = min(seg.end, self.x_to_time(visible_right + 50))
        if tl_end <= tl_start:
            return

        # Use segment's own audio source (cross-track) or track's
        sample_rate = seg.effective_sample_rate(track)
        mipmaps = seg.effective_mipmaps(track)

        samples_per_px = sample_rate / self.px_per_second
        best_factor = 1
        for f in [1000, 100, 10]:
            if samples_per_px > f * 1.5:
                best_factor = f
                break

        center_y = lane.center().y()
        amplitude = max(1.0, lane.height() * 0.42)
        painter.setPen(QPen(color, 1))

        start_x = int(self.time_to_x(tl_start))
        end_x = int(self.time_to_x(tl_end))

        if best_factor == 1 or best_factor not in mipmaps:
            mono = mipmaps.get(1)
            if mono is None:
                return
            for x in range(start_x, end_x):
                t = self.x_to_time(x)
                idx = int((seg.source_start + (t - seg.start)) * sample_rate)
                if 0 <= idx < len(mono):
                    val = mono[idx]
                    y = int(center_y - val * amplitude)
                    painter.drawLine(x, int(center_y), x, y)
        else:
            data = mipmaps[best_factor]
            max_vals = data['max']
            min_vals = data['min']
            ratio = sample_rate / best_factor
            for x in range(start_x, end_x):
                t = self.x_to_time(x)
                idx = int((seg.source_start + (t - seg.start)) * ratio)
                if 0 <= idx < len(max_vals):
                    y1 = int(center_y - max_vals[idx] * amplitude)
                    y2 = int(center_y - min_vals[idx] * amplitude)
                    painter.drawLine(x, y1, x, y2)

    def _draw_automation_line(self, painter: QPainter, index: int, track: TrackModel) -> None:
        if not track.automation_points:
            return
        path = QPainterPath()
        points = sorted(track.automation_points, key=lambda p: p.time)
        first = points[0]
        path.moveTo(self.time_to_x(first.time), self.automation_value_to_y(index, first.value))
        for point in points[1:]:
            path.lineTo(self.time_to_x(point.time), self.automation_value_to_y(index, point.value))
        painter.setPen(QPen(Theme.ACCENT_SOFT, 2))
        painter.drawPath(path)
        center_y = self.automation_value_to_y(index, 0.5)
        painter.setPen(QPen(QColor(255, 138, 61, 55), 1, Qt.DashLine))
        painter.drawLine(int(self.LEFT_PADDING), int(center_y), int(self.width()), int(center_y))

    def _draw_automation_points(self, painter: QPainter, index: int, track: TrackModel) -> None:
        for point_index, point in enumerate(track.automation_points):
            x = self.time_to_x(point.time)
            y = self.automation_value_to_y(index, point.value)
            selected = self.project.selected_point == (index, point_index)
            painter.setPen(QPen(Theme.BG, 1))
            painter.setBrush(Theme.NODE_SELECTED if selected else Theme.NODE)
            painter.drawEllipse(QPointF(x, y), self.POINT_RADIUS, self.POINT_RADIUS)

    # --------- Hit testing ---------

    def _draw_playhead(self, painter: QPainter) -> None:
        x = self.time_to_x(self.project.playhead_time)
        painter.setPen(QPen(Theme.PLAYHEAD, 2))
        painter.drawLine(int(x), 0, int(x), self.height())

    def _find_track_at_y(self, y: float) -> Optional[int]:
        if y < self.RULER_HEIGHT:
            return None
        idx = int((y - self.RULER_HEIGHT) // self.track_height)
        return idx if 0 <= idx < len(self.project.tracks) else None

    def _find_point_at_pos(self, pos: QPointF) -> Optional[Tuple[int, int]]:
        for track_index, track in enumerate(self.project.tracks):
            for point_index, point in enumerate(track.automation_points):
                x = self.time_to_x(point.time)
                y = self.automation_value_to_y(track_index, point.value)
                if QRectF(x - 7, y - 7, 14, 14).contains(pos):
                    return track_index, point_index
        return None

    def _segment_hit_info(self, pos: QPointF) -> Optional[Tuple[int, int, str]]:
        track_index = self._find_track_at_y(pos.y())
        if track_index is None:
            return None
        lane = self.track_rect(track_index).adjusted(8, 12, -8, -12)
        edge_pad = 6.0
        for seg_index, seg in enumerate(self.project.tracks[track_index].segments):
            x1 = self.time_to_x(seg.start)
            x2 = self.time_to_x(seg.end)
            rect = QRectF(x1, lane.top(), max(1.0, x2 - x1), lane.height())
            if not rect.contains(pos):
                continue
            if abs(pos.x() - x1) <= edge_pad:
                return track_index, seg_index, 'left'
            if abs(pos.x() - x2) <= edge_pad:
                return track_index, seg_index, 'right'
            return track_index, seg_index, 'body'
        return None

    def _find_segment_at_pos(self, pos: QPointF) -> Optional[Tuple[int, int]]:
        hit = self._segment_hit_info(pos)
        return (hit[0], hit[1]) if hit else None

    def _find_gap_at(self, track_index: int, time: float) -> Optional[Tuple[int, int, int]]:
        """Find a gap between segments at a given time. Returns (track_index, left_seg_idx, right_seg_idx).
        left_seg_idx = -1 means gap is before first segment. right_seg_idx = -1 means gap is after last."""
        if not (0 <= track_index < len(self.project.tracks)):
            return None
        track = self.project.tracks[track_index]
        sorted_segs = sorted(enumerate(track.segments), key=lambda x: x[1].start)
        # Check gap before first segment
        if sorted_segs and time < sorted_segs[0][1].start:
            return (track_index, -1, sorted_segs[0][0])
        # Check gaps between segments
        for i in range(len(sorted_segs) - 1):
            left_idx, left_seg = sorted_segs[i]
            right_idx, right_seg = sorted_segs[i + 1]
            if left_seg.end <= time <= right_seg.start:
                return (track_index, left_idx, right_idx)
        # Check gap after last segment
        if sorted_segs and time > sorted_segs[-1][1].end:
            return (track_index, sorted_segs[-1][0], -1)
        return None

    # --------- Mutation tracking ---------

    def _begin_mutation(self) -> None:
        if not self._mutating:
            self._mutating = True
            self.mutation_started.emit()

    def _end_drag_mutation(self) -> None:
        self._mutating = False

    def _add_automation_point(self, track_index: int, pos: QPointF) -> None:
        track = self.project.tracks[track_index]
        self._begin_mutation()
        new_point = AutomationPoint(
            time=max(0.0, min(track.duration, self.x_to_time(pos.x()))),
            value=self.y_to_automation_value(track_index, pos.y()),
        )
        track.automation_points.append(new_point)
        track.automation_points.sort(key=lambda p: p.time)
        self.project.selected_point = (track_index, track.automation_points.index(new_point))
        self.project.selected_track = track_index
        self.project.selected_segment = None
        track._ensure_automation_bounds()
        self.project_changed.emit()
        self.status_changed.emit(f'Automation point added: {track.name}')
        self.update()

    # --------- Mouse events ---------

    def mousePressEvent(self, event) -> None:
        pos = event.position()
        self.mouse_press_pos = pos
        if event.button() == Qt.RightButton:
            self._show_context_menu(event.globalPosition().toPoint(), pos)
            return
        if event.button() != Qt.LeftButton:
            return
        track_index = self._find_track_at_y(pos.y())
        if event.modifiers() & Qt.ControlModifier and track_index is not None:
            self._add_automation_point(track_index, pos)
            return
        point_hit = self._find_point_at_pos(pos)
        if point_hit:
            self._begin_mutation()
            self.project.selected_point = point_hit
            self.project.selected_track = point_hit[0]
            self.project.selected_segment = None
            self.project.selected_gap = None
            self.dragging_point = point_hit
            self.track_selected.emit(point_hit[0])
            self.project_changed.emit()
            self.update()
            return

        segment_hit_info = self._segment_hit_info(pos)
        segment_hit = (segment_hit_info[0], segment_hit_info[1]) if segment_hit_info else None
        click_time = self.x_to_time(pos.x())
        self.project.playhead_time = click_time
        self.playhead_clicked.emit(click_time)
        self.dragging_playhead = True

        if event.modifiers() & Qt.ShiftModifier and segment_hit is not None:
            self.project.selected_point = None
            self.project.selected_gap = None
            if segment_hit in self.project.selected_segments:
                self.project.selected_segments = [s for s in self.project.selected_segments if s != segment_hit]
                if self.project.selected_segment == segment_hit:
                    self.project.selected_segment = self.project.selected_segments[-1] if self.project.selected_segments else None
            else:
                self.project.selected_segments.append(segment_hit)
                self.project.selected_segment = segment_hit
            self.project.selected_track = segment_hit[0]
            self.track_selected.emit(segment_hit[0])
            self.dragging_playhead = False
            self.project_changed.emit()
            self.update()
            return

        # Если трек заблокирован — не регистрируем pending_segment_hit для drag
        if segment_hit_info is not None and not self._is_track_locked(segment_hit_info[0]):
            self.pending_segment_hit = segment_hit_info
            self.dragging_playhead = False
            self.project.selected_gap = None

        if point_hit is None and segment_hit is None:
            self.project.selected_point = None
            self.project.selected_segment = None
            self.project.selected_segments = []
            # Select the track under cursor (empty area or gap click)
            if track_index is not None:
                self.project.selected_track = track_index
                self.track_selected.emit(track_index)
                # Detect if click is in a gap between segments
                self.project.selected_gap = self._find_gap_at(track_index, click_time)
            else:
                self.project.selected_gap = None
            self.project_changed.emit()
            self.update()
            return

        self.project_changed.emit()
        self.update()

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() != Qt.LeftButton:
            return super().mouseDoubleClickEvent(event)
        pos = event.position()
        track_index = self._find_track_at_y(pos.y())
        segment_hit = self._find_segment_at_pos(pos)
        self.dragging_playhead = False
        self.mouse_press_pos = None
        self.pending_segment_hit = None
        self.project.selected_point = None
        if segment_hit:
            already_selected = self.project.selected_segment == segment_hit or segment_hit in self.project.selected_segments
            self.project.selected_track = segment_hit[0]
            if already_selected:
                self.project.selected_segment = None
                self.project.selected_segments = []
            else:
                self.project.selected_segments = [segment_hit]
                self.project.selected_segment = segment_hit
                self.track_selected.emit(segment_hit[0])
        elif track_index is not None:
            if (
                self.project.selected_track == track_index
                and self.project.selected_segment is None
            ):
                self.project.selected_track = None
                self.project.selected_segments = []
            else:
                self.project.selected_track = track_index
                self.project.selected_segment = None
                self.project.selected_segments = []
                self.track_selected.emit(track_index)
        else:
            self.project.selected_track = None
            self.project.selected_segment = None
            self.project.selected_segments = []
        self.project_changed.emit()
        self.update()

    def _update_hover_cursor(self, pos: Optional[QPointF] = None) -> None:
        if self.dragging_segment:
            if self.dragging_segment_mode in ('left', 'right'):
                self.setCursor(Qt.SizeHorCursor)
            else:
                self.setCursor(Qt.ClosedHandCursor)
            return
        if self.dragging_point:
            self.setCursor(Qt.ArrowCursor)
            return
        if pos is None:
            pos = QPointF(self.mapFromGlobal(self.cursor().pos()))
        elif hasattr(pos, 'toPointF'):
            pos = pos.toPointF()
        else:
            pos = QPointF(pos)
        if not self.rect().contains(pos.toPoint()):
            self.unsetCursor()
            return
        if self._find_point_at_pos(pos) is not None:
            self.setCursor(Qt.ArrowCursor)
            return
        if QApplication.keyboardModifiers() & Qt.ControlModifier:
            track_index = self._find_track_at_y(pos.y())
            if track_index is not None:
                self.setCursor(Qt.ArrowCursor)
                return
        # В locked-режиме не показываем ладошку/resize-курсоры над сегментами
        hit = self._segment_hit_info(pos)
        if hit is None:
            self.unsetCursor()
        elif self._is_track_locked(hit[0]):
            self.unsetCursor()
        elif hit[2] in ('left', 'right'):
            self.setCursor(Qt.SizeHorCursor)
        else:
            self.setCursor(Qt.OpenHandCursor)

    def mouseMoveEvent(self, event) -> None:
        pos = event.position()
        if self.dragging_point:
            track_index, point_index = self.dragging_point
            track = self.project.tracks[track_index]
            point = track.automation_points[point_index]
            point.time = max(0.0, min(track.duration, self.x_to_time(pos.x())))
            point.value = self.y_to_automation_value(track_index, pos.y())
            track.automation_points.sort(key=lambda p: p.time)
            self.project.selected_point = (track_index, track.automation_points.index(point))
            self.project_changed.emit()
            self.update()
            self._update_hover_cursor(pos)
            return
        if self.dragging_segment:
            track_index, seg_index = self.dragging_segment
            track = self.project.tracks[track_index]
            current = self.x_to_time(pos.x())
            if self.dragging_segment_mode == 'body':
                # Check if mouse moved to a different track
                hover_track = self._find_track_at_y(pos.y())
                if hover_track is not None and hover_track != self._drag_origin_track:
                    # Cross-track drag preview
                    self._cross_track_target = hover_track
                    seg = self._drag_origin_seg_snapshot
                    if seg is not None:
                        seg_dur = seg.end - seg.start
                        desired_start = current - seg_dur / 2.0
                        target_track = self.project.tracks[hover_track]
                        if target_track.can_accept_segment(max(0.0, desired_start), max(0.0, desired_start) + seg_dur):
                            self._cross_track_valid = True
                        else:
                            self._cross_track_valid = False
                    self.update()
                else:
                    self._cross_track_target = None
                    self._cross_track_valid = False
                    # Same-track move with clamping
                    delta = current - self.drag_origin_time
                    original_start = self.drag_origin_segment_start
                    current_index = next(
                        (i for i, seg in enumerate(track.segments)
                         if math.isclose(seg.start, original_start, abs_tol=0.001)),
                        seg_index,
                    )
                    old_start = track.segments[current_index].start if 0 <= current_index < len(track.segments) else original_start
                    if track.move_segment(current_index, delta):
                        new_start = track.segments[current_index].start if 0 <= current_index < len(track.segments) else original_start + delta
                        actual_delta = new_start - old_start
                        self.drag_origin_time = self.drag_origin_time + actual_delta
                        self.drag_origin_segment_start = original_start + actual_delta
                        self.project_changed.emit()
                    self.update()
            else:
                edge = 'left' if self.dragging_segment_mode == 'left' else 'right'
                if track.trim_segment(seg_index, edge, current):
                    self.project_changed.emit()
                    self.update()
            self._update_hover_cursor(pos)
            return
        if self.pending_segment_hit and self.mouse_press_pos is not None:
            delta = (pos - self.mouse_press_pos).manhattanLength()
            if delta > 4:
                self._begin_mutation()
                track_index, seg_index, mode = self.pending_segment_hit
                self.dragging_segment = (track_index, seg_index)
                self.dragging_segment_mode = mode
                self.project.selected_track = track_index
                self.drag_origin_time = self.x_to_time(pos.x())
                seg = self.project.tracks[track_index].segments[seg_index]
                self.drag_origin_segment_start = seg.start
                self._drag_origin_track = track_index
                self._drag_origin_seg_snapshot = TrackSegment(
                    seg.start, seg.end, seg.source_start,
                    source_audio=seg.source_audio,
                    source_mipmaps=seg.source_mipmaps,
                    source_sample_rate=seg.source_sample_rate,
                )
                self._cross_track_target = None
                self._cross_track_valid = False
                self.pending_segment_hit = None
                self._update_hover_cursor(pos)
                return
        if self.dragging_playhead:
            self.project.playhead_time = self.x_to_time(pos.x())
            self.project_changed.emit()
            self.update()
            self._update_hover_cursor(pos)
            return

        self._update_hover_cursor(pos)

    def mouseReleaseEvent(self, event) -> None:
        release_pos = event.position()
        # Handle cross-track segment drop
        if (
            self.dragging_segment
            and self.dragging_segment_mode == 'body'
            and self._cross_track_target is not None
            and self._drag_origin_track is not None
            and self._drag_origin_seg_snapshot is not None
        ):
            self._finalize_cross_track_drop(release_pos)

        self.dragging_playhead = False
        self.dragging_point = None
        self.dragging_segment = None
        self.dragging_segment_mode = None
        self.pending_segment_hit = None
        self.mouse_press_pos = None
        self.drag_origin_segment_start = None
        self._cross_track_target = None
        self._cross_track_valid = False
        self._drag_origin_track = None
        self._drag_origin_seg_snapshot = None
        self.project_changed.emit()
        self.update()
        self._update_hover_cursor(release_pos)
        self._end_drag_mutation()

    # --------- Drag & Drop files ---------

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            audio_exts = {'.wav', '.mp3', '.flac', '.ogg', '.m4a', '.aac'}
            if any(
                os.path.splitext(url.toLocalFile())[1].lower() in audio_exts
                for url in urls if url.isLocalFile()
            ):
                event.acceptProposedAction()
                return
        event.ignore()

    def dragMoveEvent(self, event) -> None:
        event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        if not event.mimeData().hasUrls():
            return
        audio_exts = {'.wav', '.mp3', '.flac', '.ogg', '.m4a', '.aac'}
        paths = []
        for url in event.mimeData().urls():
            if url.isLocalFile():
                path = url.toLocalFile()
                if os.path.splitext(path)[1].lower() in audio_exts:
                    paths.append(path)
        if paths:
            self.files_dropped.emit(paths)
            event.acceptProposedAction()

    def wheelEvent(self, event: QWheelEvent) -> None:
        scroll = self._find_scroll_area()
        if event.modifiers() & Qt.ControlModifier:
            delta = event.angleDelta().y() / 120.0
            time_anchor = self.project.playhead_time
            viewport_center_x = scroll.viewport().width() / 2.0 if scroll else 0.0
            self.px_per_second = max(30.0, min(1600.0, self.px_per_second + delta * 18.0))
            self.track_height = max(88, min(260, int(self.track_height + delta * 8.0)))
            self._update_minimum_size()
            if scroll:
                new_scene_x = self.time_to_x(time_anchor)
                scroll.horizontalScrollBar().setValue(int(max(0.0, new_scene_x - viewport_center_x)))
            self.project_changed.emit()
            self.viewport_update()
            event.accept()
            return
        if scroll:
            if event.modifiers() & Qt.ShiftModifier:
                step = int(event.angleDelta().y() * -0.8)
                bar = scroll.horizontalScrollBar()
                bar.setValue(bar.value() + step)
            else:
                step = int(event.angleDelta().y() * -0.8)
                bar = scroll.verticalScrollBar()
                bar.setValue(bar.value() + step)
            event.accept()
            return
        super().wheelEvent(event)

    def _find_scroll_area(self) -> Optional[QScrollArea]:
        parent = self.parentWidget()
        while parent:
            if isinstance(parent, QScrollArea):
                return parent
            parent = parent.parentWidget()
        return None

    def viewport_update(self) -> None:
        scroll = self._find_scroll_area()
        if scroll is None:
            self.update()
            return
        vr = scroll.viewport().rect()
        top_left = self.mapFrom(scroll.viewport(), vr.topLeft())
        bottom_right = self.mapFrom(scroll.viewport(), vr.bottomRight())
        self.update(QRectF(QPointF(top_left), QPointF(bottom_right)).toRect().adjusted(-32, -32, 32, 32))

    # --------- Cross-track segment drop ---------

    def _finalize_cross_track_drop(self, pos: QPointF) -> None:
        """Move segment from source track to target track on mouse release."""
        src_track_idx = self._drag_origin_track
        tgt_track_idx = self._cross_track_target
        snap = self._drag_origin_seg_snapshot
        if src_track_idx is None or tgt_track_idx is None or snap is None:
            return
        if not (0 <= src_track_idx < len(self.project.tracks)):
            return
        if not (0 <= tgt_track_idx < len(self.project.tracks)):
            return

        src_track = self.project.tracks[src_track_idx]
        tgt_track = self.project.tracks[tgt_track_idx]
        seg_dur = snap.end - snap.start
        current_time = self.x_to_time(pos.x())
        desired_start = max(0.0, current_time - seg_dur / 2.0)

        # Try exact position first, then find nearest gap
        place_start = tgt_track.find_nearest_gap(desired_start, seg_dur)
        if place_start is None:
            # Can't fit — revert segment to original position on source track
            self._revert_segment_to_origin()
            self.status_changed.emit('No space on target track — segment returned')
            return

        # Find and remove the segment from source track
        found_idx = None
        for i, seg in enumerate(src_track.segments):
            if (
                math.isclose(seg.source_start, snap.source_start, abs_tol=1e-4)
                and abs(seg.duration - seg_dur) < 0.01
            ):
                found_idx = i
                break
        if found_idx is None:
            self._revert_segment_to_origin()
            return

        del src_track.segments[found_idx]
        src_track.ensure_full_segment()

        # Add to target track — carry source audio reference
        src_audio = snap.source_audio if snap.source_audio is not None else src_track.audio_data
        src_mipmaps = snap.source_mipmaps if snap.source_mipmaps is not None else src_track.mipmaps
        src_sr = snap.source_sample_rate if snap.source_sample_rate is not None else src_track.sample_rate
        # If target track has the same audio, no need for cross-reference
        if tgt_track.audio_data is not None and tgt_track.audio_data is src_audio:
            new_seg = TrackSegment(place_start, place_start + seg_dur, snap.source_start)
        else:
            new_seg = TrackSegment(
                place_start, place_start + seg_dur, snap.source_start,
                source_audio=src_audio,
                source_mipmaps=src_mipmaps,
                source_sample_rate=src_sr,
            )
        tgt_track.segments.append(new_seg)
        tgt_track.segments.sort(key=lambda s: s.start)
        tgt_track.ensure_full_segment()

        self.project.selected_track = tgt_track_idx
        self.project.selected_segment = None
        self.project.selected_segments = []
        self.project_changed.emit()
        self.audio_mix_changed.emit()
        self.status_changed.emit(f'Segment moved to {tgt_track.name}')

    def _revert_segment_to_origin(self) -> None:
        """Revert the dragged segment back to its original position."""
        if self._drag_origin_track is None or self._drag_origin_seg_snapshot is None:
            return
        src_track_idx = self._drag_origin_track
        snap = self._drag_origin_seg_snapshot
        if not (0 <= src_track_idx < len(self.project.tracks)):
            return
        src_track = self.project.tracks[src_track_idx]
        # Check if segment is still there (it might have been moved within the same track)
        for seg in src_track.segments:
            if math.isclose(seg.source_start, snap.source_start, abs_tol=1e-4):
                # Restore original position
                seg.start = snap.start
                seg.end = snap.end
                src_track.segments.sort(key=lambda s: s.start)
                src_track.ensure_full_segment()
                break
        self.project_changed.emit()

    # --------- Context menu & ops ---------

    def _show_context_menu(self, global_pos, local_pos: QPointF) -> None:
        menu = QMenu(self)
        track_index = self._find_track_at_y(local_pos.y())
        selection = None
        point_hit = self._find_point_at_pos(local_pos)
        segment_hit = self._find_segment_at_pos(local_pos)

        if point_hit:
            self.project.selected_point = point_hit
            self.project.selected_track = point_hit[0]
            self.project.selected_segment = None
            self.project.selected_segments = []
            self.project_changed.emit()
            self.update()

        if track_index is not None:
            menu.addAction('Add Automation Point', lambda: self._add_automation_point(track_index, local_pos))
            menu.addAction('Clear Automation', lambda: self.clear_automation(track_index))

        point_to_delete = point_hit or self.project.selected_point
        if point_to_delete is not None:
            menu.addAction('Delete Automation Point', lambda: self.delete_automation_point(point_to_delete))

        if selection and track_index is not None:
            menu.addAction('Delete Selection', lambda: self.delete_selection(track_index))
        if segment_hit:
            menu.addAction('Delete Segment', self.delete_selected)
        merge_candidates = self._mergeable_selected_segments()
        if len(merge_candidates) > 1:
            menu.addAction('Merge Selected Segments', self.merge_selected_segments)
        menu.exec(global_pos)

    def _mergeable_selected_segments(self) -> List[Tuple[int, int]]:
        selected = list(dict.fromkeys(self.project.selected_segments))
        if len(selected) < 2:
            return []
        track_indexes = {track_idx for track_idx, _ in selected}
        if len(track_indexes) != 1:
            return []
        track_idx = selected[0][0]
        track = self.project.tracks[track_idx]
        valid = []
        for _, seg_idx in selected:
            if 0 <= seg_idx < len(track.segments):
                valid.append((track_idx, seg_idx))
        if len(valid) < 2:
            return []
        ordered = sorted(valid, key=lambda item: track.segments[item[1]].start)
        return ordered

    def merge_selected_segments(self) -> None:
        selected = self._mergeable_selected_segments()
        if len(selected) < 2:
            self.status_changed.emit('Select 2+ segments on the same track to merge')
            return
        track_idx = selected[0][0]
        track = self.project.tracks[track_idx]
        segments = [track.segments[seg_idx] for _, seg_idx in selected]
        merged = TrackSegment(
            min(seg.start for seg in segments),
            max(seg.end for seg in segments),
            segments[0].source_start,
            source_audio=segments[0].source_audio,
            source_mipmaps=segments[0].source_mipmaps,
            source_sample_rate=segments[0].source_sample_rate,
        )
        selected_indexes = {seg_idx for _, seg_idx in selected}
        for i, other in enumerate(track.segments):
            if i in selected_indexes:
                continue
            if merged.start < other.end - 0.001 and merged.end > other.start + 0.001:
                self.status_changed.emit('Merge blocked: overlaps another segment in between')
                return
        self._begin_mutation()
        track.segments = [seg for i, seg in enumerate(track.segments) if i not in selected_indexes]
        track.segments.append(merged)
        track.segments.sort(key=lambda seg: seg.start)
        track.ensure_full_segment()
        merged_index = next(
            (i for i, seg in enumerate(track.segments)
             if math.isclose(seg.start, merged.start, abs_tol=1e-4)
             and math.isclose(seg.end, merged.end, abs_tol=1e-4)
             and math.isclose(seg.source_start, merged.source_start, abs_tol=1e-4)),
            None,
        )
        self.project.selected_track = track_idx
        self.project.selected_segment = (track_idx, merged_index) if merged_index is not None else None
        self.project.selected_segments = [self.project.selected_segment] if self.project.selected_segment else []
        self.project_changed.emit()
        self.status_changed.emit(f'Merged {len(selected)} segments')
        self.update()

    def delete_automation_point(self, point_ref: Optional[Tuple[int, int]] = None) -> None:
        point_ref = point_ref or self.project.selected_point
        if point_ref is None:
            return
        track_idx, point_idx = point_ref
        if not (0 <= track_idx < len(self.project.tracks)):
            return
        track = self.project.tracks[track_idx]
        if not (0 <= point_idx < len(track.automation_points)):
            return
        self._begin_mutation()
        del track.automation_points[point_idx]
        track._ensure_automation_bounds()
        self.project.selected_point = None
        self.project_changed.emit()
        self.status_changed.emit(f'Automation point deleted: {track.name}')
        self.update()

    def delete_selection(self, track_index: int) -> None:
        if not self.project.selection_range:
            return
        start, end = self.project.selection_range
        track = self.project.tracks[track_index]
        self._begin_mutation()
        if track.delete_selection(start, end):
            self.project.selected_segment = None
            self.project_changed.emit()
            self.status_changed.emit(f'Delete selection: {track.name}')
            self.update()

    def clear_automation(self, track_index: int) -> None:
        self._begin_mutation()
        self.project.tracks[track_index].clear_automation()
        self.project_changed.emit()
        self.status_changed.emit(f'Automation cleared: {self.project.tracks[track_index].name}')
        self.update()

    def set_zoom_from_slider(self, value: int) -> None:
        self.px_per_second = 30.0 + value * 8.8
        self._update_minimum_size()
        self.viewport_update()

    def delete_selected(self) -> None:
        self._begin_mutation()
        if self.project.selected_point:
            self.delete_automation_point(self.project.selected_point)
            return
        if self.project.selected_segment:
            track_idx, seg_idx = self.project.selected_segment
            track = self.project.tracks[track_idx]
            if 0 <= seg_idx < len(track.segments):
                del track.segments[seg_idx]
                track.ensure_full_segment()
                self.project.selected_segment = None
                self.project.selected_segments = []
                self.project_changed.emit()
                self.status_changed.emit('Segment deleted')
                self.update()
