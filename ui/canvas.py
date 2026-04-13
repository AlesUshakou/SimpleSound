from __future__ import annotations

import math
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
    selection_changed = Signal()
    project_changed = Signal()
    track_selected = Signal(int)
    status_changed = Signal(str)
    mutation_started = Signal()
    playhead_clicked = Signal(float)

    RULER_HEIGHT = 38
    LEFT_PADDING = 14
    POINT_RADIUS = 5

    def __init__(self, project: ProjectModel, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.project = project
        self.px_per_second = 30.0
        self.track_height = 128
        self.dragging_point: Optional[Tuple[int, int]] = None
        self.dragging_selection = False
        self.dragging_playhead = False
        self.dragging_segment: Optional[Tuple[int, int]] = None
        self.dragging_segment_mode: Optional[str] = None
        self.pending_segment_hit: Optional[Tuple[int, int, str]] = None
        self.dragging_existing_selection = False
        self.selection_anchor_x: Optional[float] = None
        self.mouse_press_pos: Optional[QPointF] = None
        self.drag_origin_time: float = 0.0
        self.drag_origin_selection: Optional[Tuple[float, float]] = None
        self.drag_origin_segment_start: Optional[float] = None
        self._mutating = False
        self.waveform_cache = WaveformRenderCache()
        self.waveform_mode = 'signed'
        self.setMouseTracking(True)
        self.setMinimumWidth(1200)
        self._update_minimum_size()

    # --------- Geometry ---------

    def set_track_height(self, value: int) -> None:
        self.track_height = max(88, min(260, value))
        self._update_minimum_size()
        self.update()

    def _update_minimum_size(self) -> None:
        content_w = int(max(1000, self.LEFT_PADDING * 2 + self.project.duration() * self.px_per_second + 120))
        content_h = self.RULER_HEIGHT + len(self.project.tracks) * self.track_height
        min_h = self.RULER_HEIGHT + 2 if not self.project.tracks else content_h + 2
        self.setMinimumSize(content_w, min_h)
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
            self._draw_ruler(painter)
            if not self.project.tracks:
                self._draw_background_pattern(painter)
                self._draw_playhead(painter)
                return
            for i, track in enumerate(self.project.tracks):
                self._draw_track(painter, i, track)
            self._draw_background_pattern(painter)
            self._draw_selection_overlay(painter)
            self._draw_playhead(painter)
        finally:
            painter.end()

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
            and self.project.selection_range is None
        )
        visible_left, visible_right = self.visible_x_range()
        for seg_index, seg in enumerate(track.segments):
            x1 = self.time_to_x(seg.start)
            x2 = self.time_to_x(seg.end)
            if x2 < visible_left - 32 or x1 > visible_right + 32:
                continue
            seg_rect = QRectF(x1, lane.top(), max(1.0, x2 - x1), lane.height())
            selected = self.project.selected_segment == (track_index, seg_index) or (track_index, seg_index) in selected_set
            fill = QColor(255, 138, 61, 55) if selected else (
                QColor(255, 255, 255, 18) if track_selected else QColor(255, 255, 255, 12)
            )
            border = Theme.SEGMENT_SELECTED if selected else (
                QColor('#FFB27C') if track_selected else QColor(255, 255, 255, 18)
            )
            painter.setBrush(fill)
            painter.setPen(QPen(border, 1.4))
            painter.drawRoundedRect(seg_rect, 6, 6)

    def _draw_waveform(self, painter: QPainter, rect: QRectF, track: TrackModel, track_index: int) -> None:
        del track_index
        if track.duration <= 0.0 or track.audio_data is None or track.audio_data.size == 0:
            return
        lane = rect.adjusted(12, 18, -12, -18)
        visible_left, visible_right = self.visible_x_range()
        painter.save()
        painter.setClipRect(lane)
        color = QColor(210, 225, 245, 170)
        for seg in track.segments:
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

        samples_per_px = track.sample_rate / self.px_per_second
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

        if best_factor == 1 or best_factor not in track.mipmaps:
            mono = track.mipmaps.get(1)
            if mono is None:
                return
            for x in range(start_x, end_x):
                t = self.x_to_time(x)
                idx = int((seg.source_start + (t - seg.start)) * track.sample_rate)
                if 0 <= idx < len(mono):
                    val = mono[idx]
                    y = int(center_y - val * amplitude)
                    painter.drawLine(x, int(center_y), x, y)
        else:
            data = track.mipmaps[best_factor]
            max_vals = data['max']
            min_vals = data['min']
            ratio = track.sample_rate / best_factor
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

    def _selection_rect_for_track(self, track_index: int) -> Optional[QRectF]:
        if not self.project.selection_range:
            return None
        start, end = self.project.selection_range
        rect = self.track_rect(track_index).adjusted(8, 8, -8, -8)
        x1 = self.time_to_x(start)
        x2 = self.time_to_x(end)
        return QRectF(min(x1, x2), rect.top(), abs(x2 - x1), rect.height())

    def _draw_selection_overlay(self, painter: QPainter) -> None:
        if not self.project.selection_range or self.project.selected_track is None:
            return
        sel_rect = self._selection_rect_for_track(self.project.selected_track)
        if sel_rect:
            painter.fillRect(sel_rect, Theme.SELECTION)

    def _draw_playhead(self, painter: QPainter) -> None:
        x = self.time_to_x(self.project.playhead_time)
        painter.setPen(QPen(Theme.PLAYHEAD, 2))
        painter.drawLine(int(x), 0, int(x), self.height())

    # --------- Hit testing ---------

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

    def _selection_contains_pos(self, pos: QPointF) -> bool:
        if self.project.selected_track is None:
            return False
        rect = self._selection_rect_for_track(self.project.selected_track)
        return bool(rect and rect.contains(pos))

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
        self.project.selection_range = None
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
            self.dragging_point = point_hit
            self.track_selected.emit(point_hit[0])
            self.project_changed.emit()
            self.update()
            return

        segment_hit_info = self._segment_hit_info(pos)
        segment_hit = (segment_hit_info[0], segment_hit_info[1]) if segment_hit_info else None
        click_time = self.x_to_time(pos.x())
        clicked_in_selection = self._selection_contains_pos(pos)
        self.project.playhead_time = click_time
        self.playhead_clicked.emit(click_time)
        self.dragging_playhead = True

        if event.modifiers() & Qt.ShiftModifier and segment_hit is not None:
            self.project.selected_point = None
            self.project.selection_range = None
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

        if segment_hit_info is not None:
            self.pending_segment_hit = segment_hit_info
            self.dragging_playhead = False

        if self.project.selection_range and not clicked_in_selection and point_hit is None:
            self.project.selection_range = None
            self.selection_changed.emit()

        if point_hit is None and segment_hit is None and not clicked_in_selection:
            self.project.selected_point = None
            self.project.selected_segment = None
            self.project.selected_segments = []
            self.project.selection_range = None
            self.project_changed.emit()
            self.update()
            return

        if clicked_in_selection and self.project.selection_range and self.project.selected_track == track_index:
            self._begin_mutation()
            self.dragging_existing_selection = True
            self.dragging_playhead = False
            self.drag_origin_time = click_time
            self.drag_origin_selection = self.project.selection_range
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
        self.dragging_selection = False
        self.mouse_press_pos = None
        self.selection_anchor_x = None
        self.pending_segment_hit = None
        self.project.selected_point = None
        if segment_hit:
            already_selected = self.project.selected_segment == segment_hit or segment_hit in self.project.selected_segments
            self.project.selected_track = segment_hit[0]
            self.project.selection_range = None
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
                and self.project.selection_range is None
            ):
                self.project.selected_track = None
                self.project.selected_segments = []
            else:
                self.project.selected_track = track_index
                self.project.selected_segment = None
                self.project.selected_segments = []
                self.project.selection_range = None
                self.track_selected.emit(track_index)
        else:
            self.project.selected_track = None
            self.project.selected_segment = None
            self.project.selected_segments = []
            self.project.selection_range = None
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
        hit = self._segment_hit_info(pos)
        if hit is None:
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
                delta = current - self.drag_origin_time
                original_start = self.drag_origin_segment_start
                current_index = next(
                    (i for i, seg in enumerate(track.segments)
                     if math.isclose(seg.start, original_start, abs_tol=0.001)),
                    seg_index,
                )
                if track.move_segment(current_index, delta):
                    self.drag_origin_time = current
                    self.drag_origin_segment_start = original_start + delta
                    self.project_changed.emit()
                    self.update()
            else:
                edge = 'left' if self.dragging_segment_mode == 'left' else 'right'
                if track.trim_segment(seg_index, edge, current):
                    self.project_changed.emit()
                    self.update()
            self._update_hover_cursor(pos)
            return
        if self.dragging_existing_selection and self.drag_origin_selection and self.project.selected_track is not None:
            track = self.project.tracks[self.project.selected_track]
            current = self.x_to_time(pos.x())
            delta = current - self.drag_origin_time
            start, end = self.drag_origin_selection
            if track.move_selection(start, end, delta):
                self.project.selection_range = (start + delta, end + delta)
                self.drag_origin_time = current
                self.drag_origin_selection = self.project.selection_range
                self.project_changed.emit()
                self.selection_changed.emit()
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
                self.drag_origin_segment_start = self.project.tracks[track_index].segments[seg_index].start
                self.pending_segment_hit = None
                self._update_hover_cursor(pos)
                return
        if self.mouse_press_pos is not None and self.dragging_playhead:
            delta = (pos - self.mouse_press_pos).manhattanLength()
            if delta > 8:
                self._begin_mutation()
                self.dragging_playhead = False
                self.dragging_selection = True
                self.selection_anchor_x = self.mouse_press_pos.x()
                a = self.x_to_time(self.selection_anchor_x)
                b = self.x_to_time(pos.x())
                self.project.selection_range = (min(a, b), max(a, b))
                self.project.selected_segment = None
                self.project_changed.emit()
                self.selection_changed.emit()
                self.update()
                self._update_hover_cursor(pos)
                return
        if self.dragging_playhead:
            self.project.playhead_time = self.x_to_time(pos.x())
            self.project_changed.emit()
            self.update()
            self._update_hover_cursor(pos)
            return
        if self.dragging_selection and self.selection_anchor_x is not None:
            a = self.x_to_time(self.selection_anchor_x)
            b = self.x_to_time(pos.x())
            self.project.selection_range = (min(a, b), max(a, b))
            self.selection_changed.emit()
            self.project_changed.emit()
            self.update()
            self._update_hover_cursor(pos)
            return

        self._update_hover_cursor(pos)

    def mouseReleaseEvent(self, event) -> None:
        release_pos = event.position()
        if self.dragging_selection and self.project.selection_range:
            start, end = self.project.selection_range
            if math.isclose(start, end, abs_tol=0.01):
                self.project.selection_range = None
        self.dragging_playhead = False
        self.dragging_point = None
        self.dragging_selection = False
        self.dragging_segment = None
        self.dragging_segment_mode = None
        self.pending_segment_hit = None
        self.dragging_existing_selection = False
        self.selection_anchor_x = None
        self.mouse_press_pos = None
        self.drag_origin_selection = None
        self.drag_origin_segment_start = None
        self.project_changed.emit()
        self.update()
        self._update_hover_cursor(release_pos)
        self._end_drag_mutation()

    def wheelEvent(self, event: QWheelEvent) -> None:
        scroll = self._find_scroll_area()
        if event.modifiers() & Qt.ControlModifier:
            # Ctrl+wheel — зум вокруг playhead
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
                # Shift+wheel — горизонтальная прокрутка (по таймлайну)
                step = int(event.angleDelta().y() * -0.8)
                bar = scroll.horizontalScrollBar()
                bar.setValue(bar.value() + step)
            else:
                # Обычное колесо — вертикальная прокрутка (по трекам)
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

    # --------- Context menu & ops ---------

    def _show_context_menu(self, global_pos, local_pos: QPointF) -> None:
        menu = QMenu(self)
        track_index = self._find_track_at_y(local_pos.y())
        selection = self.project.selection_range
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
        """Возвращает выделенные сегменты одного трека, пригодные для слияния.
        Допускаются любые 2+ сегмента одного трека — разрывы на таймлайне
        при слиянии просто закрываются (результирующий сегмент покрывает
        диапазон от min(start) до max(end)).
        """
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
        )
        # Проверяем, что результирующий сегмент не перекрывает невыделенные сегменты
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
        if self.project.selection_range and self.project.selected_track is not None:
            self.delete_selection(self.project.selected_track)
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
