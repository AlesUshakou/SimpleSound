from __future__ import annotations

import os
from typing import List, Optional

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen
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


# ---------- helpers ----------

_ICONS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'assets', 'icons')


def icon(name: str) -> QIcon:
    path = os.path.join(_ICONS_DIR, f'{name}.svg')
    return QIcon(path) if os.path.exists(path) else QIcon()


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
    remove_requested = Signal(int)
    solo_requested = Signal(int)
    mute_requested = Signal(int)
    reset_automation_requested = Signal(int)
    select_requested = Signal(int)

    def __init__(self, track_index: int, track: TrackModel, row_height: int = 128, parent: Optional[QWidget] = None):
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

        self.btn_solo = QPushButton()
        self.btn_mute = QPushButton()
        self.btn_reset = QPushButton()
        self.btn_delete = QPushButton()
        self.btn_solo.setIcon(icon('solo'))
        self.btn_mute.setIcon(icon('mute'))
        self.btn_reset.setIcon(icon('automation'))
        self.btn_delete.setIcon(icon('delete'))
        for btn in (self.btn_solo, self.btn_mute, self.btn_reset, self.btn_delete):
            btn.setIconSize(QSize(16, 16))
        self.btn_solo.setToolTip('Solo track')
        self.btn_mute.setToolTip('Mute track')
        self.btn_reset.setToolTip('Reset automation')
        self.btn_delete.setToolTip('Delete track')
        for btn in (self.btn_solo, self.btn_mute, self.btn_reset, self.btn_delete):
            btn.setFixedSize(30, 30)
            btn.setCursor(Qt.PointingHandCursor)
        self.btn_solo.clicked.connect(lambda: self.solo_requested.emit(self.track_index))
        self.btn_mute.clicked.connect(lambda: self.mute_requested.emit(self.track_index))
        self.btn_reset.clicked.connect(lambda: self.reset_automation_requested.emit(self.track_index))
        self.btn_delete.clicked.connect(lambda: self.remove_requested.emit(self.track_index))
        for btn in (self.btn_solo, self.btn_mute, self.btn_reset, self.btn_delete):
            top.addWidget(btn)
        layout.addLayout(top)

        self.file_label = QLabel(os.path.basename(track.file_path) if track.file_path else 'Empty Track')
        self.file_label.setStyleSheet('color:#9BA6B2;font-size:11px;')
        layout.addWidget(self.file_label)

        self.meter = HorizontalMeter(compact=False, tall=True)
        layout.addWidget(self.meter)
        self.refresh()

    def refresh(self) -> None:
        self.btn_solo.setStyleSheet(self._button_style(self.track.solo, '#FF8A3D'))
        self.btn_mute.setStyleSheet(self._button_style(self.track.mute, '#8894A7'))
        self.btn_reset.setStyleSheet(self._button_style(False, '#8894A7'))
        self.btn_delete.setStyleSheet(self._button_style(False, '#E05F5F'))
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

    def __init__(self, project: ProjectModel, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.project = project
        self.row_height = 128
        self.setFixedWidth(250)
        self.layout_main = QVBoxLayout(self)
        self.layout_main.setContentsMargins(0, 0, 0, 0)
        self.layout_main.setSpacing(0)
        # Spacer соответствует высоте линейки канваса (RULER_HEIGHT = 38)
        self.layout_main.addSpacing(38)
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
            row.remove_requested.connect(self.remove_requested)
            row.solo_requested.connect(self.solo_requested)
            row.mute_requested.connect(self.mute_requested)
            row.reset_automation_requested.connect(self.reset_automation_requested)
            row.select_requested.connect(self.select_requested)
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

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName('BottomBar')
        self.setFixedHeight(72)
        self.setStyleSheet('background:#20242C;border-top:1px solid #303643;')
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)

        controls_wrap = QWidget()
        controls = QHBoxLayout(controls_wrap)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(10)

        self.btn_jump_start = QToolButton()
        self.btn_jump_start.setIcon(icon('jump_start'))
        self.btn_jump_start.setToolTip('Jump to start (Home)')

        self.btn_play_pause = QToolButton()
        self.btn_play_pause.setObjectName('PlayButton')
        self._icon_play = icon('play')
        self._icon_pause = icon('pause')
        self.btn_play_pause.setIcon(self._icon_play)
        self.btn_play_pause.setToolTip('Play / Pause (Space)')

        self.btn_jump_end = QToolButton()
        self.btn_jump_end.setIcon(icon('jump_end'))
        self.btn_jump_end.setToolTip('Jump to end (End)')

        self.btn_cut = QToolButton()
        self.btn_cut.setIcon(icon('cut'))
        self.btn_cut.setToolTip('Cut at playhead (C)')

        self.btn_merge = QToolButton()
        self.btn_merge.setIcon(icon('merge'))
        self.btn_merge.setToolTip('Merge selected segments (M)')

        button_font = QFont('Segoe UI', 10)
        base_style = (
            'QToolButton{background:#262B35;border:1px solid #343B48;border-radius:8px;'
            'color:#EAECEF;font-weight:700;}'
            'QToolButton:hover{background:#2F3642;border:1px solid #4A5260;}'
        )
        for btn in (self.btn_jump_start, self.btn_jump_end, self.btn_cut, self.btn_merge):
            btn.setFixedSize(44, 34)
            btn.setIconSize(QSize(18, 18))
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFont(button_font)
            btn.setStyleSheet(base_style)
        self.btn_play_pause.setFixedSize(58, 42)
        self.btn_play_pause.setIconSize(QSize(22, 22))
        self.btn_play_pause.setCursor(Qt.PointingHandCursor)
        self.btn_play_pause.setFont(button_font)
        self.btn_play_pause.setStyleSheet(
            'QToolButton{background:#FF8A3D;border:1px solid #FF9D59;border-radius:10px;'
            'color:#14181F;font-weight:800;}'
            'QToolButton:hover{background:#FF9A54;border:1px solid #FFB27C;}'
        )

        self.btn_jump_start.clicked.connect(lambda: self.jump_start_requested.emit())
        self.btn_play_pause.clicked.connect(lambda: self.play_pause_requested.emit())
        self.btn_jump_end.clicked.connect(lambda: self.jump_end_requested.emit())
        self.btn_cut.clicked.connect(lambda: self.cut_requested.emit())
        self.btn_merge.clicked.connect(lambda: self.merge_requested.emit())

        controls.addWidget(self.btn_jump_start)
        controls.addWidget(self.btn_play_pause)
        controls.addWidget(self.btn_cut)
        controls.addWidget(self.btn_merge)
        controls.addWidget(self.btn_jump_end)

        # Зум
        self.btn_zoom_reset = QToolButton()
        self.btn_zoom_reset.setIcon(icon('zoom_reset'))
        self.btn_zoom_reset.setIconSize(QSize(18, 18))
        self.btn_zoom_reset.setToolTip('Reset zoom (0)')
        self.btn_zoom_reset.setFixedSize(44, 34)
        self.btn_zoom_reset.setCursor(Qt.PointingHandCursor)
        self.btn_zoom_reset.setFont(button_font)
        self.btn_zoom_reset.setStyleSheet(base_style)
        self.zoom_slider = QSlider(Qt.Horizontal)
        self.zoom_slider.setRange(0, 360)
        self.zoom_slider.setValue(0)
        self.zoom_slider.setFixedWidth(250)
        self.zoom_slider.setStyleSheet(
            'QSlider::groove:horizontal { height: 6px; background: #2D3340; border-radius: 3px; }'
            'QSlider::handle:horizontal { width: 14px; margin: -4px 0; background: #FF8A3D; border-radius: 7px; }'
        )
        self.btn_zoom_reset.clicked.connect(lambda: self.zoom_reset_requested.emit())
        self.zoom_slider.valueChanged.connect(lambda value: self.zoom_changed.emit(value))

        zoom_wrap = QWidget()
        zoom_layout = QHBoxLayout(zoom_wrap)
        zoom_layout.setContentsMargins(0, 0, 0, 0)
        zoom_layout.setSpacing(8)
        zoom_layout.addWidget(self.btn_zoom_reset)
        zoom_layout.addWidget(self.zoom_slider)
        zoom_wrap.setFixedWidth(316)

        # Убрал пустой left_placeholder. Используем stretch для центровки.
        layout.addStretch(1)
        layout.addWidget(controls_wrap, 0, Qt.AlignCenter)
        layout.addStretch(1)
        layout.addWidget(zoom_wrap, 0, Qt.AlignRight | Qt.AlignVCenter)

    def update_time(self, seconds: float) -> None:
        return None

    def update_play_button(self, playing: bool) -> None:
        self.btn_play_pause.setIcon(self._icon_pause if playing else self._icon_play)
