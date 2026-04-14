from __future__ import annotations

import os
import json
from dataclasses import asdict
from typing import Dict, List, Optional, Tuple

import numpy as np
from pydub import AudioSegment
from PySide6.QtCore import QEvent, Qt, QThread, QTimer
from PySide6.QtGui import QAction, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QScrollArea,
    QScrollBar,
    QVBoxLayout,
    QWidget,
)

from core.audio_engine import AudioBackendUnavailableError, PortAudioAudioEngine
from core.models import (
    TARGET_CHANNELS,
    TARGET_SAMPLE_RATE,
    TARGET_SAMPLE_WIDTH,
    AutomationPoint,
    ClipboardPayload,
    HistoryStack,
    ProjectModel,
    TrackModel,
    TrackSegment,
)
from ui.canvas import TimelineCanvas
from ui.help_dialog import HelpDialog
from ui.loaders import AudioFileLoader
from ui.widgets import BottomTransportBar, HorizontalMeter, TrackHeaderPanel


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.project = ProjectModel()
        self.history = HistoryStack()
        self._restoring_history = False
        self.clipboard_payload = ClipboardPayload()
        self.loader_thread: Optional[QThread] = None
        self.loader_worker: Optional[AudioFileLoader] = None
        self.progress_dialog: Optional[QProgressDialog] = None
        self.play_start_anchor = 0.0
        self.audio_cache: Dict[str, dict] = {}
        self.shortcuts: List[QShortcut] = []
        self.setWindowTitle('SimpleSound Multitrack Editor')
        self.resize(1720, 940)
        self.setMinimumSize(1320, 760)
        self.setStyleSheet('QMainWindow{background:#181A1F;}')
        self._build_ui()
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        self._build_menu()
        self._build_shortcuts()
        self.audio_engine: Optional[PortAudioAudioEngine] = None
        self.audio_backend_error: Optional[str] = None
        self.audio_poll_timer = QTimer(self)
        self.audio_poll_timer.setInterval(16)
        self.audio_poll_timer.timeout.connect(self._poll_audio_engine)
        self._init_audio_backend()
        self._drop_placeholder_tracks()
        # Применяем стартовое состояние lock (по умолчанию заблокировано)
        self.canvas.set_segments_locked(True)
        self.header_panel.set_segments_locked(True)
        self._sync_ui(True)
        self.record_history()

    # --------- UI ---------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.top_bar = self._create_top_bar()
        root.addWidget(self.top_bar)

        editor_wrap = QWidget()
        editor_layout = QHBoxLayout(editor_wrap)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(0)

        self.header_panel = TrackHeaderPanel(self.project)
        self.header_scroll = QScrollArea()
        self.header_scroll.setWidgetResizable(True)
        self.header_scroll.setMinimumWidth(360)
        self.header_scroll.setMaximumWidth(490)
        self.header_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.header_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.header_scroll.setFrameShape(QFrame.NoFrame)
        self.header_scroll.setWidget(self.header_panel)
        editor_layout.addWidget(self.header_scroll)

        self.canvas = TimelineCanvas(self.project)
        self.canvas_scroll = QScrollArea()
        self.canvas_scroll.setWidgetResizable(False)
        self.canvas_scroll.setFrameShape(QFrame.NoFrame)
        self.canvas_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.canvas_scroll.setWidget(self.canvas)
        editor_layout.addWidget(self.canvas_scroll, 1)
        root.addWidget(editor_wrap, 1)

        self.timeline_scrollbar = QScrollBar(Qt.Horizontal)
        self.timeline_scrollbar.setFixedHeight(14)
        self.timeline_scrollbar.setStyleSheet(
            'QScrollBar:horizontal{background:#1D2129;height:14px;}'
            'QScrollBar::handle:horizontal{background:#4A5260;min-width:24px;border-radius:6px;}'
            'QScrollBar::add-line:horizontal,QScrollBar::sub-line:horizontal{width:0px;}'
            'QScrollBar::add-page:horizontal,QScrollBar::sub-page:horizontal{background:#252B35;}'
        )
        root.addWidget(self.timeline_scrollbar)

        self.bottom_bar = BottomTransportBar()
        root.addWidget(self.bottom_bar)

        self.status_label = QLabel('Ready')
        self.statusBar().addWidget(self.status_label, 1)

        self.copyright_label = QLabel(
            '<a href="https://www.linkedin.com/in/ales-ushakou" '
            'style="color:#9BA6B2; text-decoration:none;">© Aleš Ushakou, 2026</a>'
        )
        self.copyright_label.setOpenExternalLinks(True)
        self.copyright_label.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.copyright_label.setCursor(Qt.PointingHandCursor)
        self.copyright_label.setStyleSheet(
            'QLabel { padding: 0 12px; font-size: 11px; background: transparent; }'
        )
        # Hover-эффект меняем через enterEvent/leaveEvent, т.к. QSS :hover на <a> в QLabel не работает
        def _on_enter(_e, label=self.copyright_label):
            label.setText(
                '<a href="https://www.linkedin.com/in/ales-ushakou" '
                'style="color:#FF8A3D; text-decoration:none;">© Aleš Ushakou, 2026</a>'
            )
        def _on_leave(_e, label=self.copyright_label):
            label.setText(
                '<a href="https://www.linkedin.com/in/ales-ushakou" '
                'style="color:#9BA6B2; text-decoration:none;">© Aleš Ushakou, 2026</a>'
            )
        self.copyright_label.enterEvent = _on_enter
        self.copyright_label.leaveEvent = _on_leave
        self.statusBar().addPermanentWidget(self.copyright_label)

        self.statusBar().setStyleSheet(
            'QStatusBar{background:#20242C;color:#9BA6B2;border-top:1px solid #303643;}'
        )
        self._connect_signals()

    def _create_top_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName('TopBar')
        bar.setFixedHeight(58)
        bar.setStyleSheet('background:#20242C;border-bottom:1px solid #303643;')
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(12)

        left_wrap = QWidget()
        left_layout = QHBoxLayout(left_wrap)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(12)
        self.project_label = QLabel('No project')
        self.project_label.setStyleSheet('color:#EAECEF;font-weight:700;font-size:13px;')
        self.mode_label = QLabel('Mode: All Tracks')
        self.mode_label.setStyleSheet('color:#9BA6B2;font-weight:600;')
        left_layout.addWidget(self.project_label)
        left_layout.addWidget(self.mode_label)
        left_layout.addStretch(1)

        right_wrap = QWidget()
        right_layout = QHBoxLayout(right_wrap)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(10)
        self.master_meter = HorizontalMeter(compact=False, tall=True)
        self.master_meter.setFixedWidth(320)
        self.master_meter.setToolTip('Master level meter')
        right_layout.addWidget(self.master_meter, 0, Qt.AlignRight | Qt.AlignVCenter)

        layout.addWidget(left_wrap, 1)
        layout.addWidget(right_wrap, 1)
        return bar

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu('File')
        edit_menu = self.menuBar().addMenu('Edit')
        help_menu = self.menuBar().addMenu('Help')
        self.act_open_tracks = QAction('Open Tracks...', self)
        self.act_open_tracks.setShortcut(QKeySequence.Open)
        self.act_open_project = QAction('Open Project...', self)
        self.act_open_project.setShortcut(QKeySequence('Ctrl+Shift+O'))
        self.act_save_project = QAction('Save Project', self)
        self.act_save_project.setShortcut(QKeySequence.Save)
        self.act_save_project_as = QAction('Save Project As...', self)
        self.act_save_project_as.setShortcut(QKeySequence('Ctrl+Shift+S'))
        file_menu.addAction(self.act_open_tracks)
        file_menu.addAction(self.act_open_project)
        file_menu.addSeparator()
        file_menu.addAction(self.act_save_project)
        file_menu.addAction(self.act_save_project_as)

        self.act_undo = QAction('Undo', self)
        self.act_undo.setShortcut(QKeySequence('Ctrl+Z'))
        self.act_undo.setShortcutContext(Qt.ApplicationShortcut)
        self.act_redo = QAction('Redo', self)
        self.act_redo.setShortcut(QKeySequence('Ctrl+Shift+Z'))
        self.act_redo.setShortcutContext(Qt.ApplicationShortcut)
        self.act_copy = QAction('Copy', self)
        self.act_copy.setShortcut(QKeySequence.Copy)
        self.act_copy.setShortcutContext(Qt.ApplicationShortcut)
        self.act_paste = QAction('Paste', self)
        self.act_paste.setShortcut(QKeySequence.Paste)
        self.act_paste.setShortcutContext(Qt.ApplicationShortcut)
        self.act_delete = QAction('Delete', self)
        self.act_delete.setShortcut(QKeySequence.Delete)
        self.act_delete.setShortcutContext(Qt.ApplicationShortcut)
        self.act_merge = QAction('Merge Selected Segments', self)
        self.act_merge.setShortcut(QKeySequence('M'))
        self.act_merge.setShortcutContext(Qt.ApplicationShortcut)
        self.act_toggle_lock = QAction('Toggle Segment Lock', self)
        self.act_toggle_lock.setShortcut(QKeySequence('L'))
        self.act_toggle_lock.setShortcutContext(Qt.ApplicationShortcut)
        edit_menu.addAction(self.act_undo)
        edit_menu.addAction(self.act_redo)
        edit_menu.addSeparator()
        edit_menu.addAction(self.act_copy)
        edit_menu.addAction(self.act_paste)
        edit_menu.addAction(self.act_delete)
        edit_menu.addAction(self.act_merge)
        edit_menu.addSeparator()
        edit_menu.addAction(self.act_toggle_lock)

        self.act_help = QAction('Help && Shortcuts', self)
        self.act_help.setShortcut(QKeySequence('F1'))
        self.act_help.triggered.connect(self.show_help_dialog)
        help_menu.addAction(self.act_help)

        help_menu.addSeparator()

        self.act_about = QAction('About SimpleSound', self)
        self.act_about.triggered.connect(self.show_about_dialog)
        help_menu.addAction(self.act_about)

        self.act_open_tracks.triggered.connect(self.open_files)
        self.act_open_project.triggered.connect(self.open_project_dialog)
        self.act_save_project.triggered.connect(self.save_project)
        self.act_save_project_as.triggered.connect(self.save_project_as)
        self.act_undo.triggered.connect(self.undo)
        self.act_redo.triggered.connect(self.redo)
        self.act_copy.triggered.connect(self.copy_clipboard)
        self.act_paste.triggered.connect(self.paste_clipboard)
        self.act_delete.triggered.connect(self.canvas.delete_selected)
        self.act_merge.triggered.connect(self.merge_selected_segments)
        self.act_toggle_lock.triggered.connect(self.toggle_segments_lock)

    def _connect_signals(self) -> None:
        self.header_panel.remove_requested.connect(self.remove_track)
        self.header_panel.solo_requested.connect(self.toggle_solo)
        self.header_panel.mute_requested.connect(self.toggle_mute)
        self.header_panel.reset_automation_requested.connect(self.clear_automation)
        self.header_panel.select_requested.connect(self.select_track)
        self.header_panel.lock_toggled.connect(self.set_segments_locked)

        self.canvas.project_changed.connect(lambda: self._sync_ui(False))
        self.canvas.selection_changed.connect(lambda: self._sync_ui(False))
        self.canvas.playhead_clicked.connect(self.seek_playhead)
        self.canvas.track_selected.connect(self.select_track)
        self.canvas.status_changed.connect(self.status_label.setText)
        self.canvas.mutation_started.connect(self.record_history)

        self.bottom_bar.jump_start_requested.connect(self.jump_to_start)
        self.bottom_bar.play_pause_requested.connect(self.toggle_play_pause)
        self.bottom_bar.jump_end_requested.connect(self.jump_to_end)
        self.bottom_bar.cut_requested.connect(self.cut_at_playhead)
        self.bottom_bar.merge_requested.connect(self.merge_selected_segments)
        self.bottom_bar.zoom_changed.connect(self._set_zoom_from_bottom_slider)
        self.bottom_bar.zoom_reset_requested.connect(self.reset_zoom)
        self.bottom_bar.peak_prev_requested.connect(self.jump_to_prev_peak)
        self.bottom_bar.peak_next_requested.connect(self.jump_to_next_peak)

        self.header_scroll.verticalScrollBar().valueChanged.connect(
            self.canvas_scroll.verticalScrollBar().setValue
        )
        self.canvas_scroll.verticalScrollBar().valueChanged.connect(
            self.header_scroll.verticalScrollBar().setValue
        )
        self.canvas_scroll.horizontalScrollBar().rangeChanged.connect(self._sync_timeline_scrollbar)
        self.canvas_scroll.horizontalScrollBar().valueChanged.connect(self._sync_timeline_scrollbar_value)
        self.timeline_scrollbar.valueChanged.connect(self._apply_timeline_scrollbar)

    def _build_shortcuts(self) -> None:
        def add_shortcut(sequence, handler) -> None:
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.ApplicationShortcut)
            shortcut.activated.connect(handler)
            self.shortcuts.append(shortcut)

        add_shortcut(Qt.Key_Space, self.toggle_play_pause)
        add_shortcut('Shift+Space', self.jump_to_next_peak)
        add_shortcut('Ctrl+Shift+Space', self.jump_to_prev_peak)
        add_shortcut('Ctrl+Y', self.redo)
        add_shortcut('C', self.cut_at_playhead)
        add_shortcut('M', self.merge_selected_segments)
        add_shortcut('L', self.toggle_segments_lock)
        add_shortcut(Qt.Key_Home, self.jump_to_start)
        add_shortcut(Qt.Key_End, self.jump_to_end)
        for index in range(1, 10):
            add_shortcut(str(index), lambda idx=index - 1: self.solo_track_by_number(idx))

    # --------- Help ---------

    def show_help_dialog(self) -> None:
        dlg = HelpDialog(self)
        dlg.exec()

    def show_about_dialog(self) -> None:
        dlg = HelpDialog(self, initial_tab=HelpDialog.TAB_ABOUT)
        dlg.exec()

    # --------- Segments lock ---------

    def set_segments_locked(self, locked: bool) -> None:
        locked = bool(locked)
        self.canvas.set_segments_locked(locked)
        self.header_panel.set_segments_locked(locked)
        self.status_label.setText('Segments locked' if locked else 'Segments unlocked')

    def toggle_segments_lock(self) -> None:
        self.set_segments_locked(not self.canvas.segments_locked)

    # --------- Peak navigation ---------

    def jump_to_next_peak(self) -> None:
        t = self.canvas.find_nearest_peak(+1)
        if t is None:
            self.status_label.setText('No peak found ahead')
            return
        self.set_playhead(t, preserve_selection=False)
        self.scroll_timeline_to_time(t, align='center')
        self.status_label.setText(f'Jumped to next peak: {t:.3f}s')

    def jump_to_prev_peak(self) -> None:
        t = self.canvas.find_nearest_peak(-1)
        if t is None:
            self.status_label.setText('No peak found behind')
            return
        self.set_playhead(t, preserve_selection=False)
        self.scroll_timeline_to_time(t, align='center')
        self.status_label.setText(f'Jumped to previous peak: {t:.3f}s')

    # --------- Audio backend ---------

    def _init_audio_backend(self) -> None:
        try:
            self.audio_engine = PortAudioAudioEngine(self.project)
            self.audio_backend_error = None
        except AudioBackendUnavailableError as exc:
            self.audio_engine = None
            self.audio_backend_error = str(exc)
            self.status_label.setText(self.audio_backend_error)

    def _poll_audio_engine(self) -> None:
        if self.audio_engine is None:
            return
        snapshot = self.audio_engine.snapshot()
        self._apply_audio_meters(snapshot.track_levels, snapshot.master)
        self._on_playhead_changed(snapshot.playhead_time)
        if snapshot.finished:
            self._on_engine_stopped(self.play_start_anchor)

    # --------- History ---------

    def record_history(self) -> None:
        if self._restoring_history:
            return
        self.history.record(self.project_to_dict(include_audio=False))

    def undo(self) -> None:
        state = self.history.undo()
        if state is None:
            self.status_label.setText('Nothing to undo')
            return
        self.stop_playback()
        self._restore_project_from_dict(state)
        self.status_label.setText('Undo')

    def redo(self) -> None:
        state = self.history.redo()
        if state is None:
            self.status_label.setText('Nothing to redo')
            return
        self.stop_playback()
        self._restore_project_from_dict(state)
        self.status_label.setText('Redo')

    # --------- Track sanitation ---------

    def _prune_placeholder_tracks(self) -> bool:
        kept_tracks: List[TrackModel] = []
        removed = False
        for track in self.project.tracks:
            has_audio = track.audio_data is not None and getattr(track.audio_data, 'size', 0) > 0
            has_file = bool(track.file_path)
            has_duration = track.duration > 0.001
            if has_audio or (has_file and has_duration):
                track.ensure_full_segment()
                kept_tracks.append(track)
            else:
                removed = True
        if not removed:
            return False
        self.project.tracks = kept_tracks
        for i, track in enumerate(self.project.tracks, start=1):
            track.track_id = i
        if self.project.selected_track is not None and self.project.selected_track >= len(self.project.tracks):
            self.project.selected_track = len(self.project.tracks) - 1 if self.project.tracks else None
        if self.project.selected_segment and self.project.selected_segment[0] >= len(self.project.tracks):
            self.project.selected_segment = None
        self.project.selected_segments = [seg for seg in self.project.selected_segments if seg[0] < len(self.project.tracks)]
        if self.project.selected_point and self.project.selected_point[0] >= len(self.project.tracks):
            self.project.selected_point = None
        return True

    def _drop_placeholder_tracks(self) -> None:
        cleaned: List[TrackModel] = []
        for track in self.project.tracks:
            has_audio = track.audio_data is not None and track.audio_data.size > 0
            has_content = has_audio or (bool(track.file_path) and track.duration > 0.01)
            if has_content:
                track.ensure_full_segment()
                cleaned.append(track)
        self.project.tracks = cleaned
        for i, track in enumerate(self.project.tracks, start=1):
            track.track_id = i
            track.ensure_full_segment()
        if not self.project.tracks:
            self.project.selected_track = None
            self.project.selected_segment = None
            self.project.selected_segments = []
            self.project.selection_range = None
        elif self.project.selected_track is None or self.project.selected_track >= len(self.project.tracks):
            self.project.selected_track = min(len(self.project.tracks) - 1, 0)

    # --------- Audio cache & restore ---------

    def _load_audio_cache_entry(self, file_path: str) -> Optional[dict]:
        if not file_path:
            return None
        cache_entry = self.audio_cache.get(file_path)
        if cache_entry is not None:
            return cache_entry
        if not os.path.exists(file_path):
            return None
        try:
            segment = AudioSegment.from_file(file_path)
            segment = (
                segment.set_frame_rate(TARGET_SAMPLE_RATE)
                .set_channels(TARGET_CHANNELS)
                .set_sample_width(TARGET_SAMPLE_WIDTH)
            )
            raw = np.array(segment.get_array_of_samples(), dtype=np.int16)
            audio = raw.reshape((-1, TARGET_CHANNELS)).astype(np.float32) / 32768.0
            cache_entry = {
                'audio_data': np.ascontiguousarray(audio),
                'sample_rate': TARGET_SAMPLE_RATE,
                'waveform_peaks': None,
                'waveform_peak_resolution': 0,
            }
            self.audio_cache[file_path] = cache_entry
            return cache_entry
        except Exception:
            return None

    def _restore_project_from_dict(self, data: dict) -> None:
        self._restoring_history = True
        try:
            project = ProjectModel()
            project.playhead_time = float(data.get('playhead_time', 0.0))
            project.selection_range = tuple(data.get('selection_range')) if data.get('selection_range') else None
            project.loop_range = tuple(data.get('loop_range')) if data.get('loop_range') else None
            project.play_range_start = float(data.get('play_range_start', 0.0))
            project.play_range_end = data.get('play_range_end')
            project.selected_track = data.get('selected_track')
            project.selected_point = tuple(data.get('selected_point')) if data.get('selected_point') else None
            project.selected_segment = tuple(data.get('selected_segment')) if data.get('selected_segment') else None
            project.selected_segments = [tuple(x) for x in data.get('selected_segments', [])]
            project.project_path = data.get('project_path')
            for item in data.get('tracks', []):
                track = TrackModel(
                    track_id=int(item.get('track_id', len(project.tracks) + 1)),
                    name=item.get('name', f'Track {len(project.tracks) + 1}'),
                    file_path=item.get('file_path', ''),
                    duration=float(item.get('duration', 0.0)),
                    solo=bool(item.get('solo', False)),
                    mute=bool(item.get('mute', False)),
                    automation_points=[AutomationPoint(**p) for p in item.get('automation_points', [])],
                    segments=[TrackSegment(**s) for s in item.get('segments', [])],
                )
                cache_entry = self._load_audio_cache_entry(track.file_path)
                if item.get('audio_data') is not None:
                    audio = np.array(item['audio_data'], dtype=np.float32)
                    track.set_audio_data(audio, int(item.get('sample_rate', TARGET_SAMPLE_RATE)))
                    self.audio_cache[track.file_path] = {
                        'audio_data': track.audio_data,
                        'sample_rate': track.sample_rate,
                        'waveform_peaks': track.waveform_peaks,
                        'waveform_peak_resolution': track.waveform_peak_resolution,
                    }
                elif cache_entry is not None:
                    track.set_audio_data(cache_entry['audio_data'], int(cache_entry.get('sample_rate', TARGET_SAMPLE_RATE)))
                    track.waveform_peaks = cache_entry.get('waveform_peaks')
                    track.waveform_peak_resolution = int(cache_entry.get('waveform_peak_resolution', 0))
                else:
                    track.waveform_peaks = item.get('waveform_peaks')
                    track.waveform_peak_resolution = int(item.get('waveform_peak_resolution', 0))
                track.segments = [
                    TrackSegment(max(0.0, s.start), min(track.duration + 120.0, s.end), getattr(s, 'source_start', s.start))
                    for s in track.segments
                ] or [TrackSegment(0.0, track.duration, 0.0)]
                track.ensure_full_segment()
                project.add_track(track)
            self.project = project
            self._drop_placeholder_tracks()
            self.header_panel.project = self.project
            self.canvas.project = self.project
            if self.audio_engine is not None:
                self.audio_engine.set_project(self.project)
            self.header_panel.rebuild()
            self._sync_ui(True)
        finally:
            self._restoring_history = False

    # --------- Time formatting ---------

    @staticmethod
    def _format_time_display(seconds: float) -> str:
        seconds = max(0.0, seconds)
        mins = int(seconds // 60)
        secs = int(seconds % 60)
        millis = int((seconds - int(seconds)) * 1000)
        return f'{mins:02d}:{secs:02d}.{millis:03d}'

    # --------- Zoom ---------

    def reset_zoom(self) -> None:
        self.bottom_bar.zoom_slider.setValue(0)

    def scroll_timeline_to_time(self, time_value: float, align: str = 'center') -> None:
        scroll = self.canvas_scroll.horizontalScrollBar()
        viewport_width = self.canvas_scroll.viewport().width()
        target_x = self.canvas.time_to_x(max(0.0, float(time_value)))
        if align == 'left':
            desired = target_x - 24.0
        elif align == 'right':
            desired = target_x - viewport_width + 24.0
        else:
            desired = target_x - viewport_width * 0.5
        scroll.setValue(int(max(scroll.minimum(), min(scroll.maximum(), desired))))

    def _set_zoom_from_bottom_slider(self, value: int) -> None:
        scroll = self.canvas_scroll.horizontalScrollBar()
        viewport_center_x = self.canvas_scroll.viewport().width() / 2.0
        time_anchor = self.project.playhead_time
        self.canvas.set_zoom_from_slider(value)
        new_scene_center_x = self.canvas.time_to_x(time_anchor)
        scroll.setValue(int(max(0.0, new_scene_center_x - viewport_center_x)))
        self._sync_timeline_scrollbar()
        self._sync_ui(False)

    # --------- Scrollbar sync ---------

    def _sync_timeline_scrollbar(self, *_args) -> None:
        src = self.canvas_scroll.horizontalScrollBar()
        self.timeline_scrollbar.blockSignals(True)
        self.timeline_scrollbar.setRange(src.minimum(), src.maximum())
        self.timeline_scrollbar.setPageStep(src.pageStep())
        self.timeline_scrollbar.setSingleStep(src.singleStep())
        self.timeline_scrollbar.setValue(src.value())
        self.timeline_scrollbar.blockSignals(False)

    def _sync_timeline_scrollbar_value(self, value: int) -> None:
        self.timeline_scrollbar.blockSignals(True)
        self.timeline_scrollbar.setValue(value)
        self.timeline_scrollbar.blockSignals(False)

    def _apply_timeline_scrollbar(self, value: int) -> None:
        self.canvas_scroll.horizontalScrollBar().setValue(value)

    # --------- UI sync ---------

    def _sync_ui(self, refresh_canvas: bool = True) -> None:
        removed_placeholder = self._prune_placeholder_tracks()
        if not self.project.tracks:
            self.project.selected_track = None
            self.project.selected_segment = None
            self.project.selected_segments = []
            self.project.selection_range = None
        for track in self.project.tracks:
            track.ensure_full_segment()
        self.bottom_bar.update_time(self.project.playhead_time)
        self.bottom_bar.update_play_button(self.project.playing)
        self.master_meter.set_levels(
            self.project.master_l, self.project.master_r,
            self.project.master_peak_l, self.project.master_peak_r,
        )
        self.header_panel.set_row_height(self.canvas.track_height)
        target_header_width = 360 if not self.project.tracks else 490
        self.header_scroll.setMinimumWidth(target_header_width)
        self.header_scroll.setMaximumWidth(target_header_width)
        self.header_panel.setFixedWidth(target_header_width)
        self.header_panel.refresh()
        self.project_label.setText(
            os.path.basename(self.project.project_path) if self.project.project_path else 'No project'
        )
        solo_idx = next((i for i, t in enumerate(self.project.tracks) if t.solo), None)
        self.mode_label.setText(
            f'Mode: Solo Track {solo_idx + 1}' if solo_idx is not None else 'Mode: All Tracks'
        )
        slider_value = int((self.canvas.px_per_second - 30.0) / 8.8)
        self.bottom_bar.zoom_slider.blockSignals(True)
        self.bottom_bar.zoom_slider.setValue(
            max(self.bottom_bar.zoom_slider.minimum(), min(self.bottom_bar.zoom_slider.maximum(), slider_value))
        )
        self.bottom_bar.zoom_slider.blockSignals(False)
        if removed_placeholder:
            self.header_panel.rebuild()
        if refresh_canvas:
            self.canvas._update_minimum_size()
            self._sync_timeline_scrollbar()
            self.canvas.update()

    def _apply_audio_meters(self, levels: dict, master: dict) -> None:
        for i, track in enumerate(self.project.tracks):
            track_data = levels.get(i)
            if track_data:
                track.meter_l, track.meter_r = track_data.get('level', (-60.0, -60.0))
                track.meter_peak_l, track.meter_peak_r = track_data.get('peak', (track.meter_l, track.meter_r))
            else:
                track.meter_l = -60.0
                track.meter_r = -60.0
                track.meter_peak_l = -60.0
                track.meter_peak_r = -60.0
        self.project.master_l, self.project.master_r = master.get('level', (-60.0, -60.0))
        self.project.master_peak_l, self.project.master_peak_r = master.get(
            'peak', (self.project.master_l, self.project.master_r)
        )
        self.header_panel.refresh()
        self.master_meter.set_levels(
            self.project.master_l, self.project.master_r,
            self.project.master_peak_l, self.project.master_peak_r,
        )

    def _on_playhead_changed(self, value: float) -> None:
        previous_time = self.project.playhead_time
        self.project.playhead_time = value
        self.bottom_bar.update_time(value)
        old_x = self.canvas.time_to_x(previous_time)
        new_x = self.canvas.time_to_x(value)
        if self.project.playing:
            scroll = self.canvas_scroll.horizontalScrollBar()
            viewport_width = self.canvas_scroll.viewport().width()
            follow_threshold = scroll.value() + viewport_width * 0.5
            if new_x >= follow_threshold:
                scroll.setValue(int(max(0.0, new_x - viewport_width * 0.5)))
        dirty_left = int(min(old_x, new_x) - 6)
        dirty_width = int(abs(new_x - old_x) + 12)
        self.canvas.update(dirty_left, 0, max(16, dirty_width), self.canvas.height())

    def _on_engine_stopped(self, restart_pos: float) -> None:
        self.audio_poll_timer.stop()
        self.project.playing = False
        self.project.playhead_time = restart_pos
        if self.audio_engine is not None:
            self.audio_engine.stop()
        for track in self.project.tracks:
            track.meter_l = -60.0
            track.meter_r = -60.0
            track.meter_peak_l = -60.0
            track.meter_peak_r = -60.0
        self.project.master_l = -60.0
        self.project.master_r = -60.0
        self.project.master_peak_l = -60.0
        self.project.master_peak_r = -60.0
        self.bottom_bar.update_play_button(False)
        self.bottom_bar.update_time(restart_pos)
        self.header_panel.refresh()
        self.master_meter.set_levels(-60.0, -60.0, -60.0, -60.0)
        self.canvas.update()

    # --------- Playhead control ---------

    def set_playhead(self, value: float, preserve_selection: bool = False) -> None:
        total_duration = self.project.duration()
        value = max(0.0, min(float(value), total_duration))
        if preserve_selection and self.project.selection_range:
            sel_start, sel_end = self.project.selection_range
            if value < sel_start or value >= sel_end:
                value = sel_start
            end = sel_end
        else:
            end = total_duration
        self.project.playhead_time = value
        self.bottom_bar.update_time(value)
        self.canvas.update()
        if self.project.playing and self.audio_engine is not None:
            self.audio_engine.stop()
            self.play_start_anchor = value
            self.project.play_range_start = value
            self.project.play_range_end = end
            self.audio_engine.play(value, end, bool(preserve_selection and self.project.selection_range))
            self.audio_poll_timer.start()

    def seek_playhead(self, value: float) -> None:
        self.set_playhead(value, preserve_selection=True)

    def jump_to_start(self) -> None:
        self.set_playhead(0.0, preserve_selection=False)
        self.scroll_timeline_to_time(0.0, align='left')

    def jump_to_end(self) -> None:
        end_time = self.project.duration()
        self.set_playhead(end_time, preserve_selection=False)
        self.scroll_timeline_to_time(end_time, align='right')

    # --------- Clipboard ---------

    def _track_payload_source(self) -> Optional[Tuple[int, ClipboardPayload]]:
        if self.project.selected_track is None:
            return None
        track_index = self.project.selected_track
        track = self.project.tracks[track_index]
        if self.project.selection_range:
            start, end = self.project.selection_range
            copied: List[TrackSegment] = []
            for seg in track.segments:
                overlap_start = max(seg.start, start)
                overlap_end = min(seg.end, end)
                if overlap_end > overlap_start:
                    copied.append(TrackSegment(overlap_start, overlap_end))
            if copied:
                return track_index, ClipboardPayload(duration=end - start, segments=copied)
        if self.project.selected_segment and self.project.selected_segment[0] == track_index:
            seg = track.segments[self.project.selected_segment[1]]
            return track_index, ClipboardPayload(duration=seg.duration, segments=[TrackSegment(seg.start, seg.end)])
        return None

    def copy_clipboard(self) -> None:
        src = self._track_payload_source()
        if not src:
            self.status_label.setText('Nothing to copy')
            return
        _, payload = src
        self.clipboard_payload = ClipboardPayload(
            payload.duration,
            [TrackSegment(s.start, s.end) for s in payload.segments],
        )
        self.status_label.setText(f'Copied {len(payload.segments)} segment(s)')

    def paste_clipboard(self) -> None:
        if self.project.selected_track is None or not self.clipboard_payload.segments:
            self.status_label.setText('Nothing to paste')
            return
        self.record_history()
        track = self.project.tracks[self.project.selected_track]
        insert_time = self.project.playhead_time
        payload = self.clipboard_payload
        offset = payload.segments[0].start
        new_segments = [
            TrackSegment(insert_time + (s.start - offset), insert_time + (s.end - offset))
            for s in payload.segments
        ]
        for moved in new_segments:
            for seg in track.segments:
                if moved.start < seg.end - 0.001 and moved.end > seg.start + 0.001:
                    self.status_label.setText('Paste blocked: overlaps another segment')
                    return
        track.segments.extend(new_segments)
        track.segments.sort(key=lambda s: s.start)
        self._sync_ui(True)

    # --------- Edit ops ---------

    def _best_track_for_edit(self) -> Optional[int]:
        if self.project.selected_track is not None and 0 <= self.project.selected_track < len(self.project.tracks):
            return self.project.selected_track
        soloed = [i for i, track in enumerate(self.project.tracks) if track.solo]
        if soloed:
            return soloed[0]
        return 0 if self.project.tracks else None

    def cut_at_playhead(self) -> None:
        track_index = self._best_track_for_edit()
        if track_index is None:
            self.status_label.setText('No track selected for cut')
            return
        track = self.project.tracks[track_index]
        cut_time = max(0.0, min(self.project.playhead_time, track.duration))
        if cut_time <= 0.0 or cut_time >= track.duration:
            self.status_label.setText('Cut ignored: playhead is at track boundary')
            return
        if not any(seg.start < cut_time < seg.end for seg in track.segments):
            self.status_label.setText('Cut ignored: nothing to split at playhead')
            return
        self.record_history()
        if track.cut_at(cut_time):
            self.project.selected_track = track_index
            self.project.selected_segment = None
            self.project.selected_segments = []
            self.project.selection_range = None
            self._sync_ui(True)
            self.status_label.setText(f'Cut at {cut_time:.3f}s: {track.name}')

    def merge_selected_segments(self) -> None:
        before = self.canvas._mergeable_selected_segments()
        if len(before) < 2:
            self.status_label.setText('Select 2+ segments on the same track to merge')
            return
        self.canvas.merge_selected_segments()
        self._sync_ui(True)

    def solo_track_by_number(self, index: int) -> None:
        if not (0 <= index < len(self.project.tracks)):
            return
        target = self.project.tracks[index]
        only_this_track = target.solo and sum(1 for track in self.project.tracks if track.solo) == 1
        for i, track in enumerate(self.project.tracks):
            track.solo = False if only_this_track else (i == index)
        self.project.selected_track = index
        self.project.selected_point = None
        self.project.selected_segment = None
        self.project.selected_segments = []
        self.project.selection_range = None
        self._refresh_audio_mix()
        self._sync_ui(False)
        self.status_label.setText('Solo cleared' if only_this_track else f'Solo: Track {index + 1}')

    # --------- File loading ---------

    def open_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, 'Open Audio Files', '',
            'Audio Files (*.wav *.mp3 *.flac *.ogg *.m4a *.aac);;All Files (*.*)',
        )
        if not files:
            return
        self._load_tracks_with_progress(files)

    def _load_tracks_with_progress(self, paths: List[str]) -> None:
        self.progress_dialog = QProgressDialog('Loading tracks...', 'Cancel', 0, len(paths), self)
        self.progress_dialog.setWindowTitle('Loading Audio')
        self.progress_dialog.setWindowModality(Qt.WindowModal)
        self.progress_dialog.setMinimumDuration(0)
        self.loader_thread = QThread(self)
        self.loader_worker = AudioFileLoader(paths)
        self.loader_worker.moveToThread(self.loader_thread)
        self.loader_thread.started.connect(self.loader_worker.run)
        self.loader_worker.progress.connect(self._on_loader_progress)
        self.loader_worker.finished.connect(self._on_loader_finished)
        self.loader_worker.failed.connect(self._on_loader_failed)
        self.progress_dialog.canceled.connect(self.loader_worker.cancel)
        self.loader_worker.finished.connect(self.loader_thread.quit)
        self.loader_worker.failed.connect(self.loader_thread.quit)
        self.loader_thread.finished.connect(self.loader_thread.deleteLater)
        self.loader_thread.start()

    def _on_loader_progress(self, value: int, total: int, name: str) -> None:
        if self.progress_dialog:
            self.progress_dialog.setMaximum(total)
            self.progress_dialog.setValue(value)
            self.progress_dialog.setLabelText(f'Loading: {name}')

    def _on_loader_finished(self, tracks: List[TrackModel]) -> None:
        if self.progress_dialog:
            self.progress_dialog.setValue(self.progress_dialog.maximum())
            self.progress_dialog.close()
        tracks = [
            track for track in tracks
            if track.audio_data is not None and track.audio_data.size > 0 and track.duration > 0.0
        ]
        self._drop_placeholder_tracks()
        self.record_history()
        start_id = len(self.project.tracks) + 1
        for offset, track in enumerate(tracks):
            track.track_id = start_id + offset
            track.ensure_full_segment()
            self.audio_cache[track.file_path] = {
                'audio_data': track.audio_data,
                'sample_rate': track.sample_rate,
                'waveform_peaks': track.waveform_peaks,
                'waveform_peak_resolution': track.waveform_peak_resolution,
            }
            self.project.add_track(track)
        if tracks:
            self.project.selected_track = len(self.project.tracks) - 1
            self.project.playhead_time = 0.0
            self.header_panel.rebuild()
            # Новый трек тоже должен знать текущее состояние lock
            self.header_panel.set_segments_locked(self.canvas.segments_locked)
            self._sync_ui(True)
            self.status_label.setText(f'Loaded {len(tracks)} track(s)')

    def _on_loader_failed(self, message: str) -> None:
        if self.progress_dialog:
            self.progress_dialog.close()
        QMessageBox.critical(self, 'Open Audio', message)

    # --------- Track management ---------

    def remove_track(self, index: int) -> None:
        if not (0 <= index < len(self.project.tracks)):
            return
        track_name = self.project.tracks[index].name
        reply = QMessageBox.question(
            self, 'Delete Track', f'Delete track "{track_name}"?',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        if self.project.playing:
            self.stop_playback(return_to_anchor=False)
        self.record_history()
        self.project.tracks.pop(index)
        for i, track in enumerate(self.project.tracks, start=1):
            track.track_id = i
        self.project.selected_track = (
            min(index, len(self.project.tracks) - 1) if self.project.tracks else None
        )
        self.project.selected_segment = None
        self.project.selected_segments = []
        self.header_panel.rebuild()
        self.header_panel.set_segments_locked(self.canvas.segments_locked)
        self._sync_ui(True)

    def toggle_solo(self, index: int) -> None:
        if not (0 <= index < len(self.project.tracks)):
            return
        active = self.project.tracks[index].solo
        if active:
            for track in self.project.tracks:
                track.solo = False
        else:
            for i, track in enumerate(self.project.tracks):
                track.solo = (i == index)
        self._refresh_audio_mix()
        self._sync_ui(False)

    def toggle_mute(self, index: int) -> None:
        if not (0 <= index < len(self.project.tracks)):
            return
        self.project.tracks[index].mute = not self.project.tracks[index].mute
        self._refresh_audio_mix()
        self._sync_ui(False)

    def clear_automation(self, index: int) -> None:
        if not (0 <= index < len(self.project.tracks)):
            return
        self.record_history()
        self.project.tracks[index].clear_automation()
        self._sync_ui(True)

    def select_track(self, index: int) -> None:
        if 0 <= index < len(self.project.tracks):
            self.project.selected_track = index
            if self.project.selected_point and self.project.selected_point[0] != index:
                self.project.selected_point = None
            if self.project.selected_segment and self.project.selected_segment[0] != index:
                self.project.selected_segment = None
                self.project.selected_segments = []
            self._sync_ui(False)

    def _refresh_audio_mix(self) -> None:
        if self.audio_engine is None:
            return
        try:
            self.audio_engine.refresh_mix()
        except Exception:
            try:
                self.audio_engine.refresh_render_tracks()
            except Exception:
                pass

    # --------- Playback ---------

    def _current_playback_range(self) -> Tuple[float, Optional[float]]:
        total_duration = self.project.duration()
        current = max(0.0, min(self.project.playhead_time, total_duration))
        if self.project.selection_range:
            sel_start, sel_end = self.project.selection_range
            if sel_end <= sel_start:
                return current, total_duration
            if current < sel_start or current >= sel_end:
                current = sel_start
            return current, sel_end
        return current, total_duration

    def toggle_play_pause(self) -> None:
        if not self.project.tracks:
            return
        if self.audio_engine is None:
            QMessageBox.critical(
                self, 'Audio Backend',
                self.audio_backend_error or 'Audio backend is unavailable.',
            )
            self.status_label.setText(self.audio_backend_error or 'Audio backend is unavailable.')
            return
        if self.project.playing:
            self.stop_playback(return_to_anchor=False)
            return
        start, end = self._current_playback_range()
        self.play_start_anchor = start
        self.project.play_range_start = start
        self.project.play_range_end = end
        self.project.playhead_time = start
        self.project.playing = True
        self.audio_engine.play(start, end, bool(self.project.selection_range))
        self.audio_poll_timer.start()
        self._sync_ui(False)

    def stop_playback(self, return_to_anchor: bool = True) -> None:
        self.audio_poll_timer.stop()
        if self.audio_engine is not None:
            self.audio_engine.stop()
        self.project.playing = False
        if return_to_anchor:
            self.project.playhead_time = self.play_start_anchor
        self.project.play_range_start = self.play_start_anchor
        self.project.play_range_end = None
        for track in self.project.tracks:
            track.meter_l = -60.0
            track.meter_r = -60.0
            track.meter_peak_l = -60.0
            track.meter_peak_r = -60.0
        self.project.master_l = -60.0
        self.project.master_r = -60.0
        self.project.master_peak_l = -60.0
        self.project.master_peak_r = -60.0
        self._sync_ui(True)

    # --------- Save / Load ---------

    def project_to_dict(self, include_audio: bool = False) -> dict:
        return {
            'playhead_time': self.project.playhead_time,
            'selection_range': list(self.project.selection_range) if self.project.selection_range else None,
            'loop_range': list(self.project.loop_range) if self.project.loop_range else None,
            'play_range_start': self.project.play_range_start,
            'play_range_end': self.project.play_range_end,
            'selected_track': self.project.selected_track,
            'selected_point': list(self.project.selected_point) if self.project.selected_point else None,
            'selected_segment': list(self.project.selected_segment) if self.project.selected_segment else None,
            'selected_segments': [list(x) for x in self.project.selected_segments],
            'project_path': self.project.project_path,
            'tracks': [
                {
                    'track_id': track.track_id,
                    'name': track.name,
                    'file_path': track.file_path,
                    'duration': track.duration,
                    'solo': track.solo,
                    'mute': track.mute,
                    'automation_points': [asdict(p) for p in track.automation_points],
                    'segments': [asdict(s) for s in track.segments],
                    'sample_rate': track.sample_rate,
                    'audio_data': track.audio_data.tolist() if include_audio and track.audio_data is not None else None,
                    'waveform_peaks': track.waveform_peaks if include_audio else None,
                    'waveform_peak_resolution': track.waveform_peak_resolution if include_audio else 0,
                }
                for track in self.project.tracks
            ],
        }

    def save_project(self) -> None:
        if not self.project.project_path:
            self.save_project_as()
            return
        self._write_project(self.project.project_path)

    def save_project_as(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, 'Save Project', self.project.project_path or '',
            'SimpleSound Project (*.ssproj)',
        )
        if not path:
            return
        if not path.lower().endswith('.ssproj'):
            path += '.ssproj'
        self.project.project_path = path
        self._write_project(path)

    def _write_project(self, path: str) -> None:
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(self.project_to_dict(include_audio=False), f, ensure_ascii=False, indent=2)
            self.status_label.setText(f'Project saved: {os.path.basename(path)}')
            self._sync_ui(False)
        except Exception as exc:
            QMessageBox.critical(self, 'Save Project', f'Failed to save project.\n\n{exc}')

    def open_project_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, 'Open Project', '', 'SimpleSound Project (*.ssproj)',
        )
        if path:
            self.load_project(path)

    def load_project(self, path: str) -> None:
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            data['project_path'] = path
            self.history.clear()
            self._restore_project_from_dict(data)
            self.header_panel.set_segments_locked(self.canvas.segments_locked)
            self.record_history()
            self.status_label.setText(f'Project loaded: {os.path.basename(path)}')
        except Exception as exc:
            QMessageBox.critical(self, 'Open Project', f'Failed to open project.\n\n{exc}')

    # --------- Lifecycle ---------

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.KeyPress, QEvent.KeyRelease, QEvent.ShortcutOverride):
            self.canvas._update_hover_cursor()
        return super().eventFilter(obj, event)

    def closeEvent(self, event) -> None:
        self.audio_poll_timer.stop()
        if self.audio_engine is not None:
            try:
                self.audio_engine.close()
            except Exception:
                pass
        super().closeEvent(event)
