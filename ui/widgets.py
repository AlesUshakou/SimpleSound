from __future__ import annotations

import os
from typing import List, Optional

from PySide6.QtCore import QByteArray, QPointF, QRectF, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core.models import ProjectModel, TrackModel
from core.theme import Theme


# ---------- SVG icon helpers ----------

# Все SVG — с параметром {c} для цвета штриха/заливки, чтобы можно было перекрашивать.
SVG_PLAY = '''<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
<path d="M7 5.5 L7 18.5 L18.5 12 Z" fill="{c}"/></svg>'''

SVG_PAUSE = '''<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
<rect x="6.5" y="5.5" width="4" height="13" rx="1" fill="{c}"/>
<rect x="13.5" y="5.5" width="4" height="13" rx="1" fill="{c}"/></svg>'''

SVG_JUMP_START = '''<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
<rect x="4" y="5" width="2.2" height="14" rx="0.8" fill="{c}"/>
<path d="M20 5 L20 19 L8.5 12 Z" fill="{c}"/></svg>'''

SVG_JUMP_END = '''<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
<rect x="17.8" y="5" width="2.2" height="14" rx="0.8" fill="{c}"/>
<path d="M4 5 L4 19 L15.5 12 Z" fill="{c}"/></svg>'''

SVG_CUT = '''<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" fill="none" stroke="{c}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
<circle cx="6.5" cy="17" r="2.8"/>
<circle cx="6.5" cy="7" r="2.8"/>
<line x1="20" y1="4" x2="8.5" y2="15.5"/>
<line x1="20" y1="20" x2="8.5" y2="8.5"/>
<line x1="14.5" y1="14.5" x2="20" y2="20" stroke-width="1.4"/></svg>'''

SVG_MERGE = '''<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" fill="none" stroke="{c}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
<path d="M4 7 L10 7 L12 12 L10 17 L4 17"/>
<path d="M20 7 L14 7 L12 12 L14 17 L20 17"/></svg>'''

# Замок: закрытый (locked)
SVG_LOCK_CLOSED = '''<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" fill="none" stroke="{c}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
<rect x="5" y="10.5" width="14" height="9.5" rx="2"/>
<path d="M8 10.5 V7.5 a4 4 0 0 1 8 0 V10.5"/>
<circle cx="12" cy="15" r="1.2" fill="{c}" stroke="none"/></svg>'''

# Замок: открытый (unlocked) — дужка откинута вбок
SVG_LOCK_OPEN = '''<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" fill="none" stroke="{c}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
<rect x="5" y="10.5" width="14" height="9.5" rx="2"/>
<path d="M8 10.5 V7.5 a4 4 0 0 1 7.5 -1.8"/>
<circle cx="12" cy="15" r="1.2" fill="{c}" stroke="none"/></svg>'''

# Прыжок к ближайшему пику слева: волна + стрелка влево + метка пика
SVG_PEAK_PREV = '''<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" fill="none" stroke="{c}" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
<path d="M20 17 L17 17 L15.5 9 L14 15 L12.5 11 L11 17 L8.5 17"/>
<path d="M7.5 12 L4 12 M4 12 L6 10 M4 12 L6 14"/></svg>'''

# Прыжок к ближайшему пику справа
SVG_PEAK_NEXT = '''<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" fill="none" stroke="{c}" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
<path d="M4 17 L7 17 L8.5 9 L10 15 L11.5 11 L13 17 L15.5 17"/>
<path d="M16.5 12 L20 12 M20 12 L18 10 M20 12 L18 14"/></svg>'''


def make_svg_icon(svg_template: str, color: str, size: int = 22) -> QIcon:
    svg = svg_template.replace('{c}', color)
    renderer = QSvgRenderer(QByteArray(svg.encode('utf-8')))
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    p = QPainter(pixmap)
    try:
        p.setRenderHint(QPainter.Antialiasing)
        renderer.render(p)
    finally:
        p.end()
    return QIcon(pixmap)


# ---------- helpers ----------

def db_to_meter_ratio(db: float) -> float:
    db = max(-60.0, min(6.0, float(db)))
    if db <= -60.0:
        return 0.0
    return (db + 60.0) / 66.0


def linear_to_db(value: float) -> float:
    import math
    value = float(value)
    if value <= 1e-6:
        return -60.0
    return max(-60.0, min(6.0, 20.0 * math.log10(value)))


# ---------- Meter ----------

class HorizontalMeter(QWidget):
    def __init__(self, compact: bool = False, tall: bool = False, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.left_level = -60.0
        self.right_level = -60.0
        self.left_peak = -60.0
        self.right_peak = -60.0
        height = 14 if compact else (42 if tall else 24)
        self.setMinimumHeight(height)
        self.setMaximumHeight(height)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_levels(
        self,
        left: float,
        right: float,
        peak_left: Optional[float] = None,
        peak_right: Optional[float] = None,
    ) -> None:
        self.left_level = max(-60.0, min(6.0, left))
        self.right_level = max(-60.0, min(6.0, right))
        self.left_peak = self.left_level if peak_left is None else max(-60.0, min(6.0, peak_left))
        self.right_peak = self.right_level if peak_right is None else max(-60.0, min(6.0, peak_right))
        self.setToolTip(
            f'L: {self.left_level:.1f} dB (peak {self.left_peak:.1f} dB)\n'
            f'R: {self.right_level:.1f} dB (peak {self.right_peak:.1f} dB)'
        )
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.Antialiasing)
            painter.fillRect(event.rect(), Theme.BG)
            pad = 4.0
            gap = 3.0
            total_h = max(1.0, float(self.height()) - pad * 2.0)
            lane_h = max(4.0, (total_h - gap) * 0.5)
            top_rect = QRectF(pad, pad, max(1.0, float(self.width()) - pad * 2.0), lane_h)
            bottom_rect = QRectF(pad, pad + lane_h + gap, max(1.0, float(self.width()) - pad * 2.0), lane_h)
            self._draw_lane(painter, top_rect, self.left_level, self.left_peak)
            self._draw_lane(painter, bottom_rect, self.right_level, self.right_peak)
            painter.setPen(Theme.TEXT_DIM)
            font = painter.font()
            font.setPixelSize(max(8, min(11, int(lane_h) - 1)))
            painter.setFont(font)
            painter.drawText(
                QRectF(pad + 4.0, top_rect.top(), 18.0, top_rect.height()),
                Qt.AlignVCenter | Qt.AlignLeft, 'L',
            )
            painter.drawText(
                QRectF(pad + 4.0, bottom_rect.top(), 18.0, bottom_rect.height()),
                Qt.AlignVCenter | Qt.AlignLeft, 'R',
            )
        finally:
            painter.end()

    def _draw_lane(self, painter: QPainter, rect: QRectF, level: float, peak: float) -> None:
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor('#232832'))
        painter.drawRoundedRect(rect, 3, 3)
        fill_w = rect.width() * db_to_meter_ratio(level)
        if fill_w <= 0:
            return
        green_limit = rect.width() * 0.82
        green_w = min(fill_w, green_limit)
        if green_w > 0:
            painter.setBrush(Theme.METER_GREEN)
            painter.drawRoundedRect(QRectF(rect.left(), rect.top(), green_w, rect.height()), 3, 3)
        if fill_w > green_limit:
            painter.setBrush(Theme.METER_RED)
            painter.drawRoundedRect(
                QRectF(rect.left() + green_limit, rect.top(), fill_w - green_limit, rect.height()),
                2, 2,
            )
        peak_x = rect.left() + rect.width() * db_to_meter_ratio(peak)
        peak_x = max(rect.left(), min(rect.right(), peak_x))
        painter.setPen(QPen(QColor('#F7F9FB'), 1))
        painter.drawLine(QPointF(peak_x, rect.top()), QPointF(peak_x, rect.bottom()))


# ---------- Track header row ----------

class TrackHeaderRow(QFrame):
    solo_requested = Signal(int)
    mute_requested = Signal(int)
    reset_automation_requested = Signal(int)
    select_requested = Signal(int)
    lock_toggled = Signal(int, bool)  # per-track: (track_index, locked)

    def __init__(self, track_index: int, track: TrackModel, row_height: int = 128,
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.track_index = track_index
        self.track = track
        self.setObjectName('TrackHeaderRow')
        self.setFixedHeight(row_height)
        self.setMinimumWidth(250)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.num_label = QLabel(str(track.track_id))
        self.num_label.setFixedWidth(24)
        self.num_label.setStyleSheet('color:#FF8A3D;font-weight:700;font-size:14px;')
        self.name_label = QLabel(track.name)
        self.name_label.setStyleSheet('color:#EAECEF;font-weight:700;font-size:13px;')
        top.addWidget(self.num_label)
        top.addWidget(self.name_label, 1)

        self.btn_solo = QPushButton('S')
        self.btn_mute = QPushButton('M')
        self.btn_reset = QPushButton('A')
        self.btn_lock = QPushButton()
        self.btn_lock.setCheckable(True)
        self.btn_lock.setChecked(track.locked)
        self.btn_solo.setToolTip('Solo track')
        self.btn_mute.setToolTip('Mute track')
        self.btn_reset.setToolTip('Reset automation')
        self._refresh_lock_visuals()
        for btn in (self.btn_solo, self.btn_mute, self.btn_reset, self.btn_lock):
            btn.setFixedSize(30, 30)
            btn.setCursor(Qt.PointingHandCursor)
        self.btn_solo.clicked.connect(lambda: self.solo_requested.emit(self.track_index))
        self.btn_mute.clicked.connect(lambda: self.mute_requested.emit(self.track_index))
        self.btn_reset.clicked.connect(lambda: self.reset_automation_requested.emit(self.track_index))
        self.btn_lock.clicked.connect(self._on_lock_clicked)
        for btn in (self.btn_solo, self.btn_mute, self.btn_reset, self.btn_lock):
            top.addWidget(btn)
        layout.addLayout(top)

        self.file_label = QLabel(os.path.basename(track.file_path) if track.file_path else 'Empty Track')
        self.file_label.setStyleSheet('color:#9BA6B2;font-size:11px;')
        layout.addWidget(self.file_label)

        self.meter = HorizontalMeter(compact=False, tall=True)
        layout.addWidget(self.meter)
        self.refresh()

    def set_track_locked(self, locked: bool) -> None:
        self.btn_lock.blockSignals(True)
        self.btn_lock.setChecked(locked)
        self.btn_lock.blockSignals(False)
        self._refresh_lock_visuals()

    def _on_lock_clicked(self) -> None:
        locked = self.btn_lock.isChecked()
        self._refresh_lock_visuals()
        self.lock_toggled.emit(self.track_index, locked)

    def _refresh_lock_visuals(self) -> None:
        locked = self.btn_lock.isChecked()
        if locked:
            self.btn_lock.setIcon(make_svg_icon(SVG_LOCK_CLOSED, '#FF8A3D', 18))
            self.btn_lock.setToolTip(
                'Track locked — click to unlock.\n'
                'Locked: segments cannot be moved, trimmed or dragged.\n'
                'Unlocked: drag segment body to move, drag edges to trim.'
            )
        else:
            self.btn_lock.setIcon(make_svg_icon(SVG_LOCK_OPEN, '#EAECEF', 18))
            self.btn_lock.setToolTip(
                'Track unlocked — click to lock.\n'
                'Unlocked: drag segment body to move, drag edges to trim.'
            )
        self.btn_lock.setIconSize(QSize(18, 18))

    def refresh(self) -> None:
        self.btn_solo.setStyleSheet(self._button_style(self.track.solo, '#FF8A3D'))
        self.btn_mute.setStyleSheet(self._button_style(self.track.mute, '#8894A7'))
        self.btn_reset.setStyleSheet(self._button_style(False, '#8894A7'))
        self.btn_lock.setStyleSheet(self._button_style(self.track.locked, '#FF8A3D'))
        self.meter.set_levels(
            self.track.meter_l, self.track.meter_r,
            self.track.meter_peak_l, self.track.meter_peak_r,
        )

    @staticmethod
    def _button_style(active: bool, accent: str) -> str:
        bg = '#4B2D1D' if active else '#2A2F39'
        border = accent if active else '#3C4452'
        return f'''
            QPushButton {{
                background:{bg};
                color:#EAECEF;
                border:1px solid {border};
                border-radius:7px;
                font-weight:700;
            }}
            QPushButton:hover {{ background:#353C49; }}
        '''

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.select_requested.emit(self.track_index)
        super().mousePressEvent(event)


# ---------- Track header panel ----------

class TrackHeaderPanel(QWidget):
    remove_requested = Signal(int)
    solo_requested = Signal(int)
    mute_requested = Signal(int)
    reset_automation_requested = Signal(int)
    select_requested = Signal(int)
    lock_toggled = Signal(int, bool)  # per-track: (track_index, locked)
    track_reorder_requested = Signal(int, int)  # from_index, to_index
    add_empty_track_requested = Signal()
    remove_selected_track_requested = Signal()

    # SVG icons for toolbar
    SVG_ADD_TRACK = '''<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" fill="none" stroke="{c}" stroke-width="2" stroke-linecap="round">
<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>'''

    SVG_DELETE_TRACK = '''<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" fill="none" stroke="{c}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
<polyline points="3 6 5 6 21 6"/>
<path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
<line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>'''

    TOOLBAR_BTN_STYLE = (
        'QToolButton {'
        '  background: #252A34;'
        '  border: 1px solid #3C4452;'
        '  border-radius: 7px;'
        '  color: #EAECEF;'
        '}'
        'QToolButton:hover {'
        '  background: #333B47;'
        '  border: 1px solid #FF8A3D;'
        '}'
        'QToolButton:pressed {'
        '  background: #1A1E26;'
        '}'
    )

    def __init__(self, project: ProjectModel, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.project = project
        self.row_height = 128
        self._drag_source_index: Optional[int] = None
        self._drag_start_pos: Optional[QPointF] = None
        self._drag_indicator_index: Optional[int] = None
        self.setFixedWidth(250)
        self.layout_main = QVBoxLayout(self)
        self.layout_main.setContentsMargins(0, 0, 0, 0)
        self.layout_main.setSpacing(0)

        # Toolbar: Add Track + Delete Selected Track
        self.toolbar = QWidget()
        self.toolbar.setFixedHeight(38)
        self.toolbar.setStyleSheet('background: #1C2028; border-bottom: 1px solid #303643;')
        tb_layout = QHBoxLayout(self.toolbar)
        tb_layout.setContentsMargins(8, 4, 8, 4)
        tb_layout.setSpacing(6)

        self.btn_add_track = QToolButton()
        self.btn_add_track.setFixedSize(30, 28)
        self.btn_add_track.setCursor(Qt.PointingHandCursor)
        self.btn_add_track.setToolTip('Add empty track')
        self.btn_add_track.setIcon(make_svg_icon(self.SVG_ADD_TRACK, '#FF8A3D', 18))
        self.btn_add_track.setIconSize(QSize(18, 18))
        self.btn_add_track.setStyleSheet(self.TOOLBAR_BTN_STYLE)
        self.btn_add_track.clicked.connect(lambda: self.add_empty_track_requested.emit())

        self.btn_remove_track = QToolButton()
        self.btn_remove_track.setFixedSize(30, 28)
        self.btn_remove_track.setCursor(Qt.PointingHandCursor)
        self.btn_remove_track.setToolTip('Delete selected track')
        self.btn_remove_track.setIcon(make_svg_icon(self.SVG_DELETE_TRACK, '#E05F5F', 18))
        self.btn_remove_track.setIconSize(QSize(18, 18))
        self.btn_remove_track.setStyleSheet(self.TOOLBAR_BTN_STYLE)
        self.btn_remove_track.clicked.connect(lambda: self.remove_selected_track_requested.emit())

        tb_label = QLabel('Tracks')
        tb_label.setStyleSheet('color: #9BA6B2; font-weight: 700; font-size: 11px; background: transparent; border: none;')

        tb_layout.addWidget(tb_label)
        tb_layout.addStretch(1)
        tb_layout.addWidget(self.btn_add_track)
        tb_layout.addWidget(self.btn_remove_track)

        self.layout_main.addWidget(self.toolbar)

        self.empty_label = QLabel('No tracks loaded')
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setStyleSheet('color:#7F8A98;font-size:13px;font-weight:600;padding:24px 12px;')
        self.layout_main.addWidget(self.empty_label, 0, Qt.AlignTop)
        self.rows_container = QWidget()
        self.rows_layout = QVBoxLayout(self.rows_container)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(1)
        self.layout_main.addWidget(self.rows_container, 0, Qt.AlignTop)
        self.layout_main.addStretch(1)
        self.rows: List[TrackHeaderRow] = []
        self.setMouseTracking(True)

    # --- Drag-to-reorder tracks ---

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            idx = self._row_index_at_y(event.position().y())
            if idx is not None and 0 <= idx < len(self.project.tracks) and not self.project.tracks[idx].locked:
                self._drag_start_pos = event.position()
                self._drag_source_index = idx
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if (
            self._drag_source_index is not None
            and self._drag_start_pos is not None
        ):
            delta = (event.position() - self._drag_start_pos).manhattanLength()
            if delta > 8:
                target = self._row_index_at_y(event.position().y())
                if target is not None and target != self._drag_indicator_index:
                    self._drag_indicator_index = target
                    self._update_drag_indicator()
                self.setCursor(Qt.ClosedHandCursor)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._drag_source_index is not None:
            target = self._row_index_at_y(event.position().y())
            if target is not None and target != self._drag_source_index:
                self.track_reorder_requested.emit(self._drag_source_index, target)
        self._drag_source_index = None
        self._drag_start_pos = None
        self._drag_indicator_index = None
        self._clear_drag_indicator()
        self.unsetCursor()
        super().mouseReleaseEvent(event)

    def _row_index_at_y(self, y: float) -> Optional[int]:
        toolbar_h = self.toolbar.height() if self.toolbar.isVisible() else 0
        rel_y = y - toolbar_h
        if rel_y < 0:
            return 0 if self.rows else None
        idx = int(rel_y / max(1, self.row_height))
        if idx >= len(self.rows):
            return len(self.rows) - 1 if self.rows else None
        return idx

    def _update_drag_indicator(self) -> None:
        for i, row in enumerate(self.rows):
            if i == self._drag_indicator_index and i != self._drag_source_index:
                row.setStyleSheet(self._row_style_drag_target())
            elif i == self._drag_source_index:
                row.setStyleSheet(self._row_style_drag_source())
            else:
                track = self.project.tracks[i]
                row.setStyleSheet(self._row_style(self.project.selected_track == i, track.solo))

    def _clear_drag_indicator(self) -> None:
        for i, row in enumerate(self.rows):
            if i < len(self.project.tracks):
                track = self.project.tracks[i]
                row.setStyleSheet(self._row_style(self.project.selected_track == i, track.solo))

    @staticmethod
    def _row_style_drag_target() -> str:
        return '''
            QFrame#TrackHeaderRow {
                background: #2A3240;
                border: 2px solid #FF8A3D;
            }
        '''

    @staticmethod
    def _row_style_drag_source() -> str:
        return '''
            QFrame#TrackHeaderRow {
                background: #1A1E26;
                border: 1px dashed #4A5260;
            }
        '''

    def set_track_locked(self, track_index: int, locked: bool) -> None:
        if 0 <= track_index < len(self.rows):
            self.rows[track_index].set_track_locked(locked)

    def set_row_height(self, value: int) -> None:
        self.row_height = value
        for row in self.rows:
            row.setFixedHeight(value)
        self._update_container_height()

    def _update_container_height(self) -> None:
        total_height = 0 if not self.rows else len(self.rows) * self.row_height + max(0, len(self.rows) - 1)
        self.rows_container.setFixedHeight(total_height)
        self.empty_label.setVisible(not self.rows)

    def rebuild(self) -> None:
        while self.rows_layout.count():
            item = self.rows_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        self.rows.clear()
        for i, track in enumerate(self.project.tracks):
            row = TrackHeaderRow(i, track, self.row_height)
            row.solo_requested.connect(self.solo_requested)
            row.mute_requested.connect(self.mute_requested)
            row.reset_automation_requested.connect(self.reset_automation_requested)
            row.select_requested.connect(self.select_requested)
            row.lock_toggled.connect(self.lock_toggled)
            row.setStyleSheet(self._row_style(self.project.selected_track == i, track.solo))
            self.rows_layout.addWidget(row)
            self.rows.append(row)
        self._update_container_height()

    def refresh(self) -> None:
        if len(self.rows) != len(self.project.tracks):
            self.rebuild()
            return
        for i, row in enumerate(self.rows):
            track = self.project.tracks[i]
            row.track_index = i
            row.track = track
            row.num_label.setText(str(track.track_id))
            row.name_label.setText(track.name)
            row.file_label.setText(os.path.basename(track.file_path) if track.file_path else 'Empty Track')
            row.set_track_locked(track.locked)
            row.setStyleSheet(self._row_style(self.project.selected_track == i, track.solo))
            row.refresh()

    @staticmethod
    def _row_style(selected: bool, solo: bool) -> str:
        color = '#2A3240' if solo else ('#242A34' if selected else '#20242C')
        return f'''
            QFrame#TrackHeaderRow {{
                background: {color};
                border: 1px solid #2F3540;
            }}
        '''


# ---------- Bottom transport bar ----------

class BottomTransportBar(QWidget):
    jump_start_requested = Signal()
    play_pause_requested = Signal()
    jump_end_requested = Signal()
    cut_requested = Signal()
    merge_requested = Signal()
    zoom_changed = Signal(int)
    zoom_reset_requested = Signal()
    peak_prev_requested = Signal()
    peak_next_requested = Signal()

    # Стили: капсулы с лёгким свечением на ховер
    CAPSULE_STYLE = (
        'QToolButton {'
        '  background: qlineargradient(x1:0,y1:0,x2:0,y2:1,'
        '                              stop:0 #2A313C, stop:1 #20252E);'
        '  border: 1px solid #363E4C;'
        '  border-radius: 10px;'
        '  color: #EAECEF;'
        '}'
        'QToolButton:hover {'
        '  background: qlineargradient(x1:0,y1:0,x2:0,y2:1,'
        '                              stop:0 #333B47, stop:1 #262C36);'
        '  border: 1px solid #FF8A3D;'
        '}'
        'QToolButton:pressed {'
        '  background: #1A1E26;'
        '}'
    )

    PLAY_STYLE = (
        'QToolButton {'
        '  background: qradialgradient(cx:0.5, cy:0.5, radius:0.8,'
        '                              stop:0 #FFA363, stop:1 #FF7A2F);'
        '  border: 1px solid #FFB27C;'
        '  border-radius: 10px;'
        '  color: #14181F;'
        '}'
        'QToolButton:hover {'
        '  background: qradialgradient(cx:0.5, cy:0.5, radius:0.8,'
        '                              stop:0 #FFB57D, stop:1 #FF8A3D);'
        '  border: 1px solid #FFC79A;'
        '}'
        'QToolButton:pressed {'
        '  background: #E56A22;'
        '}'
    )

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName('BottomBar')
        self.setFixedHeight(72)
        self.setStyleSheet(
            'QWidget#BottomBar {'
            '  background: qlineargradient(x1:0,y1:0,x2:0,y2:1,'
            '                              stop:0 #22262F, stop:1 #1C2028);'
            '  border-top: 1px solid #303643;'
            '}'
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(14)

        # === Transport: Jump | PeakPrev | Play | PeakNext | Jump ===
        transport_wrap = self._make_capsule_group()
        transport_layout = transport_wrap.layout()
        transport_layout.setSpacing(2)

        self.btn_jump_start = self._make_icon_button(
            SVG_JUMP_START,
            'Jump to start (Home)',
        )
        self.btn_peak_prev = self._make_icon_button(
            SVG_PEAK_PREV,
            'Jump to previous peak (Ctrl+Shift+Space)\n'
            'Moves playhead to the nearest louder peak to the LEFT.\n'
            'Useful for stepping between loud transients.',
        )
        self.btn_play_pause = QToolButton()
        self.btn_play_pause.setObjectName('PlayButton')
        self.btn_play_pause.setToolTip('Play / Pause (Space)')
        self.btn_play_pause.setFixedSize(44, 34)
        self.btn_play_pause.setCursor(Qt.PointingHandCursor)
        self.btn_play_pause.setStyleSheet(self.PLAY_STYLE)
        self.btn_play_pause.setIcon(make_svg_icon(SVG_PLAY, '#14181F', 20))
        self.btn_play_pause.setIconSize(QSize(20, 20))
        self.btn_peak_next = self._make_icon_button(
            SVG_PEAK_NEXT,
            'Jump to next peak (Shift+Space)\n'
            'Moves playhead to the nearest louder peak to the RIGHT.\n'
            'Useful for stepping between loud transients.',
        )
        self.btn_jump_end = self._make_icon_button(
            SVG_JUMP_END,
            'Jump to end (End)',
        )

        transport_layout.addWidget(self.btn_jump_start)
        transport_layout.addWidget(self.btn_peak_prev)
        transport_layout.addWidget(self.btn_play_pause)
        transport_layout.addWidget(self.btn_peak_next)
        transport_layout.addWidget(self.btn_jump_end)

        # === Edit: Cut | Merge ===
        edit_wrap = self._make_capsule_group()
        edit_layout = edit_wrap.layout()
        edit_layout.setSpacing(2)
        self.btn_cut = self._make_icon_button(SVG_CUT, 'Cut at playhead (C)')
        self.btn_merge = self._make_icon_button(SVG_MERGE, 'Merge selected segments (M)')
        edit_layout.addWidget(self.btn_cut)
        edit_layout.addWidget(self.btn_merge)

        # === Zoom: Reset + Slider ===
        zoom_wrap = self._make_capsule_group()
        zoom_layout = zoom_wrap.layout()
        zoom_layout.setContentsMargins(10, 6, 10, 6)
        zoom_layout.setSpacing(10)

        zoom_icon = QLabel('−')
        zoom_icon.setFixedWidth(14)
        zoom_icon.setAlignment(Qt.AlignCenter)
        zoom_icon.setStyleSheet('color:#8894A7;font-size:16px;font-weight:700;background:transparent;border:none;')

        self.zoom_slider = QSlider(Qt.Horizontal)
        self.zoom_slider.setRange(0, 360)
        self.zoom_slider.setValue(0)
        self.zoom_slider.setFixedWidth(220)
        self.zoom_slider.setCursor(Qt.PointingHandCursor)
        self.zoom_slider.setStyleSheet(
            'QSlider { background: transparent; border: none; }'
            'QSlider::groove:horizontal {'
            '  height: 4px;'
            '  background: #14171E;'
            '  border-radius: 2px;'
            '}'
            'QSlider::sub-page:horizontal {'
            '  background: qlineargradient(x1:0,y1:0,x2:1,y2:0,'
            '                              stop:0 #FF7A2F, stop:1 #FFB27C);'
            '  border-radius: 2px;'
            '}'
            'QSlider::handle:horizontal {'
            '  width: 14px; height: 14px;'
            '  margin: -6px 0;'
            '  background: qradialgradient(cx:0.5, cy:0.5, radius:0.8,'
            '                              stop:0 #FFE1C7, stop:0.6 #FF8A3D, stop:1 #E56A22);'
            '  border: 1px solid #2A1A0F;'
            '  border-radius: 7px;'
            '}'
            'QSlider::handle:horizontal:hover {'
            '  background: qradialgradient(cx:0.5, cy:0.5, radius:0.8,'
            '                              stop:0 #FFF0DC, stop:0.6 #FFA363, stop:1 #FF7A2F);'
            '}'
        )

        zoom_plus = QLabel('+')
        zoom_plus.setFixedWidth(14)
        zoom_plus.setAlignment(Qt.AlignCenter)
        zoom_plus.setStyleSheet('color:#8894A7;font-size:16px;font-weight:700;background:transparent;border:none;')

        self.btn_zoom_reset = QToolButton()
        self.btn_zoom_reset.setText('1:1')
        self.btn_zoom_reset.setToolTip('Reset zoom (0)')
        self.btn_zoom_reset.setFixedSize(40, 28)
        self.btn_zoom_reset.setCursor(Qt.PointingHandCursor)
        f = QFont('Segoe UI', 9)
        f.setBold(True)
        self.btn_zoom_reset.setFont(f)
        self.btn_zoom_reset.setStyleSheet(
            'QToolButton {'
            '  background: transparent;'
            '  border: 1px solid #3C4452;'
            '  border-radius: 6px;'
            '  color: #9BA6B2;'
            '}'
            'QToolButton:hover {'
            '  color: #FF8A3D;'
            '  border: 1px solid #FF8A3D;'
            '}'
        )

        zoom_layout.addWidget(zoom_icon)
        zoom_layout.addWidget(self.zoom_slider)
        zoom_layout.addWidget(zoom_plus)
        zoom_layout.addWidget(self.btn_zoom_reset)

        # --- Connects ---
        self.btn_jump_start.clicked.connect(lambda: self.jump_start_requested.emit())
        self.btn_play_pause.clicked.connect(lambda: self.play_pause_requested.emit())
        self.btn_jump_end.clicked.connect(lambda: self.jump_end_requested.emit())
        self.btn_cut.clicked.connect(lambda: self.cut_requested.emit())
        self.btn_merge.clicked.connect(lambda: self.merge_requested.emit())
        self.btn_zoom_reset.clicked.connect(lambda: self.zoom_reset_requested.emit())
        self.zoom_slider.valueChanged.connect(lambda value: self.zoom_changed.emit(value))
        self.btn_peak_prev.clicked.connect(lambda: self.peak_prev_requested.emit())
        self.btn_peak_next.clicked.connect(lambda: self.peak_next_requested.emit())

        # --- Таймер в центре нижней панели ---
        self.time_label = QLabel('00:00.000')
        self.time_label.setAlignment(Qt.AlignCenter)
        self.time_label.setStyleSheet(
            'QLabel {'
            '  background: #181B22;'
            '  border: 1px solid #2A303B;'
            '  border-radius: 14px;'
            '  padding: 8px 24px;'
            '  color: #EAECEF;'
            '  font-family: "Segoe UI", "Consolas", monospace;'
            '  font-size: 18px;'
            '  font-weight: 800;'
            '  letter-spacing: 1px;'
            '}'
        )
        self.time_label.setToolTip('Playhead position')

        # --- Layout: [transport | edit]  ...stretch...  [TIMER]  ...stretch...  [zoom] ---
        layout.addWidget(transport_wrap, 0, Qt.AlignVCenter)
        layout.addWidget(edit_wrap, 0, Qt.AlignVCenter)
        layout.addStretch(1)
        layout.addWidget(self.time_label, 0, Qt.AlignCenter)
        layout.addStretch(1)
        layout.addWidget(zoom_wrap, 0, Qt.AlignRight | Qt.AlignVCenter)

    def _make_capsule_group(self) -> QWidget:
        """Контейнер-капсула: тёмный фон + скруглённая рамка для группы кнопок."""
        wrap = QWidget()
        wrap.setStyleSheet(
            'QWidget {'
            '  background: #181B22;'
            '  border: 1px solid #2A303B;'
            '  border-radius: 14px;'
            '}'
        )
        lay = QHBoxLayout(wrap)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)
        return wrap

    def _make_icon_button(self, svg: str, tooltip: str) -> QToolButton:
        btn = QToolButton()
        btn.setFixedSize(38, 34)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setStyleSheet(self.CAPSULE_STYLE)
        btn.setToolTip(tooltip)
        btn.setIcon(make_svg_icon(svg, '#D8DDE6', 20))
        btn.setIconSize(QSize(20, 20))
        return btn

    def update_time(self, seconds: float) -> None:
        seconds = max(0.0, float(seconds))
        mins = int(seconds // 60)
        secs = int(seconds % 60)
        millis = int((seconds - int(seconds)) * 1000)
        self.time_label.setText(f'{mins:02d}:{secs:02d}.{millis:03d}')

    def update_play_button(self, playing: bool) -> None:
        icon_svg = SVG_PAUSE if playing else SVG_PLAY
        self.btn_play_pause.setIcon(make_svg_icon(icon_svg, '#14181F', 20))
