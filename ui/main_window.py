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
        self._project_dirty = False
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
        self.setAcceptDrops(True)
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
        # Per-track lock is managed via TrackModel.locked (default=True)
        self._sync_ui(True)
        self.record_history()
        self._project_dirty = False  # Initial empty project is not dirty

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
        self.act_new_project = QAction('New Project', self)
        self.act_new_project.setShortcut(QKeySequence('Ctrl+N'))
        self.act_open_tracks = QAction('Open Tracks...', self)
        self.act_open_tracks.setShortcut(QKeySequence.Open)
        self.act_open_project = QAction('Open Project...', self)
        self.act_open_project.setShortcut(QKeySequence('Ctrl+Shift+O'))
        self.act_save_project = QAction('Save Project', self)
        self.act_save_project.setShortcut(QKeySequence.Save)
        self.act_save_project_as = QAction('Save Project As...', self)
        self.act_save_project_as.setShortcut(QKeySequence('Ctrl+Shift+S'))
        file_menu.addAction(self.act_new_project)
        file_menu.addSeparator()
        file_menu.addAction(self.act_open_tracks)
        file_menu.addAction(self.act_open_project)
        file_menu.addSeparator()
        self.recent_menu = file_menu.addMenu('Recent Projects')
        self._rebuild_recent_menu()
        file_menu.addSeparator()
        file_menu.addAction(self.act_save_project)
        file_menu.addAction(self.act_save_project_as)

        self.act_undo = QAction('Undo', self)
        self.act_undo.setShortcut(QKeySequence('Ctrl+Z'))
        self.act_undo.setShortcutContext(Qt.ApplicationShortcut)
        self.act_redo = QAction('Redo', self)
        self.act_redo.setShortcut(QKeySequence('Ctrl+Shift+Z'))
        self.act_redo.setShortcutContext(Qt.ApplicationShortcut)
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

        self.act_new_project.triggered.connect(self.new_project)
        self.act_open_tracks.triggered.connect(self.open_files)
        self.act_open_project.triggered.connect(self.open_project_dialog)
        self.act_save_project.triggered.connect(self.save_project)
        self.act_save_project_as.triggered.connect(self.save_project_as)
        self.act_undo.triggered.connect(self.undo)
        self.act_redo.triggered.connect(self.redo)
        self.act_delete.triggered.connect(self.canvas.delete_selected)
        self.act_merge.triggered.connect(self.merge_selected_segments)
        self.act_toggle_lock.triggered.connect(self.toggle_segments_lock)

    def _connect_signals(self) -> None:
        self.header_panel.solo_requested.connect(self.toggle_solo)
        self.header_panel.mute_requested.connect(self.toggle_mute)
        self.header_panel.reset_automation_requested.connect(self.clear_automation)
        self.header_panel.select_requested.connect(self.select_track)
        self.header_panel.lock_toggled.connect(self.set_track_locked)
        self.header_panel.track_reorder_requested.connect(self.reorder_tracks)
        self.header_panel.add_empty_track_requested.connect(self.add_empty_track)
        self.header_panel.remove_selected_track_requested.connect(self.remove_selected_track)

        self.canvas.project_changed.connect(lambda: self._sync_ui(False))
        self.canvas.playhead_clicked.connect(self.seek_playhead)
        self.canvas.track_selected.connect(self.select_track)
        self.canvas.status_changed.connect(self.status_label.setText)
        self.canvas.mutation_started.connect(self.record_history)
        self.canvas.files_dropped.connect(self._load_tracks_with_progress)
        self.canvas.audio_mix_changed.connect(self._refresh_audio_mix)

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

    def set_track_locked(self, track_index: int, locked: bool) -> None:
        if not (0 <= track_index < len(self.project.tracks)):
            return
        self.project.tracks[track_index].locked = bool(locked)
        self.header_panel.set_track_locked(track_index, locked)
        self.canvas.update()
        name = self.project.tracks[track_index].name
        self.status_label.setText(f'{name}: {"locked" if locked else "unlocked"}')

    def toggle_segments_lock(self) -> None:
        """L shortcut toggles the selected track's lock."""
        idx = self.project.selected_track
        if idx is not None and 0 <= idx < len(self.project.tracks):
            new_state = not self.project.tracks[idx].locked
            self.set_track_locked(idx, new_state)

    # --------- Peak navigation ---------

    def jump_to_next_peak(self) -> None:
        t = self.canvas.find_nearest_peak(+1)
        if t is None:
            self.status_label.setText('No peak found ahead')
            return
        self.set_playhead(t)
        self.scroll_timeline_to_time(t, align='center')
        self.status_label.setText(f'Jumped to next peak: {t:.3f}s')

    def jump_to_prev_peak(self) -> None:
        t = self.canvas.find_nearest_peak(-1)
        if t is None:
            self.status_label.setText('No peak found behind')
            return
        self.set_playhead(t)
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
        self._project_dirty = True
        self.history.record(self.project_to_dict(include_audio=False))

    def undo(self) -> None:
        state = self.history.undo()
        if state is None:
            self.status_label.setText('Nothing to undo')
            return
        current_playhead = self.project.playhead_time
        self.stop_playback()
        self._restore_project_from_dict(state)
        self.project.playhead_time = current_playhead
        self.status_label.setText('Undo')

    def redo(self) -> None:
        state = self.history.redo()
        if state is None:
            self.status_label.setText('Nothing to redo')
            return
        current_playhead = self.project.playhead_time
        self.stop_playback()
        self._restore_project_from_dict(state)
        self.project.playhead_time = current_playhead
        self.status_label.setText('Redo')

    # --------- Track sanitation ---------

    def _prune_placeholder_tracks(self) -> bool:
        # Only prune tracks that have a file_path set but no audio loaded (failed loads)
        kept_tracks: List[TrackModel] = []
        removed = False
        for track in self.project.tracks:
            has_audio = track.audio_data is not None and getattr(track.audio_data, 'size', 0) > 0
            has_file = bool(track.file_path)
            is_empty_intentional = not has_file  # Empty tracks added by user have no file_path
            if has_audio or is_empty_intentional or (has_file and track.duration > 0.001):
                if track.duration > 0:
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
            has_file = bool(track.file_path)
            is_empty_intentional = not has_file
            has_content = has_audio or is_empty_intentional or (has_file and track.duration > 0.01)
            if has_content:
                if track.duration > 0:
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
                    locked=bool(item.get('locked', True)),
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
                    TrackSegment(
                        max(0.0, s.start), min(track.duration + 120.0, s.end),
                        getattr(s, 'source_start', s.start),
                        source_audio=getattr(s, 'source_audio', None),
                        source_mipmaps=getattr(s, 'source_mipmaps', None),
                        source_sample_rate=getattr(s, 'source_sample_rate', None),
                    )
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

    def set_playhead(self, value: float) -> None:
        total_duration = self.project.duration()
        value = max(0.0, min(float(value), total_duration))
        end = total_duration
        self.project.playhead_time = value
        self.bottom_bar.update_time(value)
        self.canvas.update()
        if self.project.playing and self.audio_engine is not None:
            self.audio_engine.stop()
            self.play_start_anchor = value
            self.project.play_range_start = value
            self.project.play_range_end = end
            self.audio_engine.play(value, end, False)
            self.audio_poll_timer.start()

    def seek_playhead(self, value: float) -> None:
        self.set_playhead(value)

    def jump_to_start(self) -> None:
        self.set_playhead(0.0)
        self.scroll_timeline_to_time(0.0, align='left')

    def jump_to_end(self) -> None:
        end_time = self.project.duration()
        self.set_playhead(end_time)
        self.scroll_timeline_to_time(end_time, align='right')

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
            self._sync_ui(True)
            self.status_label.setText(f'Cut at {cut_time:.3f}s: {track.name}')

    def merge_selected_segments(self) -> None:
        # First check if a gap is selected — close the gap
        gap = self.project.selected_gap
        if gap is not None:
            track_idx, left_idx, right_idx = gap
            if 0 <= track_idx < len(self.project.tracks) and left_idx >= 0 and right_idx >= 0:
                track = self.project.tracks[track_idx]
                if left_idx < len(track.segments) and right_idx < len(track.segments):
                    left_seg = track.segments[left_idx]
                    right_seg = track.segments[right_idx]
                    gap_size = right_seg.start - left_seg.end
                    if gap_size > 0.001:
                        self.record_history()
                        # Slide the right segment to close the gap
                        right_seg.start -= gap_size
                        right_seg.end -= gap_size
                        track.segments.sort(key=lambda s: s.start)
                        track.ensure_full_segment()
                        self.project.selected_gap = None
                        self._sync_ui(True)
                        self.status_label.setText(f'Gap closed on {track.name}')
                        return
            self.status_label.setText('Cannot close this gap')
            return

        # Otherwise try to merge selected segments
        before = self.canvas._mergeable_selected_segments()
        if len(before) < 2:
            self.status_label.setText('Select 2+ segments on the same track to merge, or click a gap')
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
        self._refresh_audio_mix()
        self._sync_ui(False)
        self.canvas.update()  # Force full repaint so track highlight is visible immediately
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
            self._sync_ui(True)
            self.status_label.setText(f'Loaded {len(tracks)} track(s)')

    def _on_loader_failed(self, message: str) -> None:
        if self.progress_dialog:
            self.progress_dialog.close()
        QMessageBox.critical(self, 'Open Audio', message)

    # --------- Track management ---------

    def add_empty_track(self) -> None:
        self.record_history()
        track_id = len(self.project.tracks) + 1
        # Use the project's max duration so automation spans the full timeline
        project_duration = self.project.duration()
        track = TrackModel(
            track_id=track_id,
            name=f'Track {track_id}',
            file_path='',
            duration=project_duration,
            locked=False,
        )
        # Re-set automation to span the full project duration (not 0.1 fallback)
        if project_duration > 0:
            track.automation_points = [
                AutomationPoint(0.0, 0.5),
                AutomationPoint(project_duration, 0.5),
            ]
        # Empty track must have NO segments — segments represent audio content.
        # Having a segment spanning [0, duration] blocks cross-track drag-and-drop.
        track.segments = []
        self.project.tracks.append(track)
        self.project.selected_track = len(self.project.tracks) - 1
        self.header_panel.rebuild()
        self._sync_ui(True)
        self.status_label.setText(f'Added empty track: {track.name}')

    def remove_selected_track(self) -> None:
        idx = self.project.selected_track
        if idx is None or not (0 <= idx < len(self.project.tracks)):
            self.status_label.setText('No track selected')
            return
        self.remove_track(idx)

    def reorder_tracks(self, from_index: int, to_index: int) -> None:
        if from_index == to_index:
            return
        if not (0 <= from_index < len(self.project.tracks)):
            return
        if not (0 <= to_index < len(self.project.tracks)):
            return
        if self.project.playing:
            self.stop_playback(return_to_anchor=False)
        self.record_history()
        track = self.project.tracks.pop(from_index)
        self.project.tracks.insert(to_index, track)
        # Re-number track IDs
        for i, t in enumerate(self.project.tracks, start=1):
            t.track_id = i
        # Update selected_track to follow the moved track
        if self.project.selected_track == from_index:
            self.project.selected_track = to_index
        elif self.project.selected_track is not None:
            old_sel = self.project.selected_track
            if from_index < old_sel <= to_index:
                self.project.selected_track = old_sel - 1
            elif to_index <= old_sel < from_index:
                self.project.selected_track = old_sel + 1
        # Clear segment selections (indexes are invalidated)
        self.project.selected_segment = None
        self.project.selected_segments = []
        self.project.selected_point = None
        self.canvas.invalidate_waveform_cache()
        self.header_panel.rebuild()
        self._refresh_audio_mix()
        self._sync_ui(True)
        self.status_label.setText(f'Track moved: {track.name}')

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
        self.audio_engine.play(start, end, False)
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
                    'locked': track.locked,
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
            self._add_recent_project(path)
            self._project_dirty = False
            self.status_label.setText(f'Project saved: {os.path.basename(path)}')
            self._sync_ui(False)
        except Exception as exc:
            QMessageBox.critical(self, 'Save Project', f'Failed to save project.\n\n{exc}')

    def open_project_dialog(self) -> None:
        if not self._check_unsaved():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, 'Open Project', '', 'SimpleSound Project (*.ssproj)',
        )
        if path:
            self.load_project(path)

    def load_project(self, path: str) -> None:
        if not self._check_unsaved():
            return
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            data['project_path'] = path
            self.history.clear()
            self._restore_project_from_dict(data)
            self._add_recent_project(path)
            self._project_dirty = False
            self.record_history()
            self.status_label.setText(f'Project loaded: {os.path.basename(path)}')
        except Exception as exc:
            QMessageBox.critical(self, 'Open Project', f'Failed to open project.\n\n{exc}')

    def new_project(self) -> None:
        if not self._check_unsaved():
            return
        self.stop_playback()
        self.project = ProjectModel()
        self.header_panel.project = self.project
        self.canvas.project = self.project
        if self.audio_engine is not None:
            self.audio_engine.set_project(self.project)
        self.history.clear()
        self._project_dirty = False
        self.canvas.invalidate_waveform_cache()
        self.header_panel.rebuild()
        self._sync_ui(True)
        self.record_history()
        self.status_label.setText('New project created')

    def _check_unsaved(self) -> bool:
        """Returns True if it's safe to proceed (saved or user chose to discard)."""
        if not self._project_dirty:
            return True
        # Don't ask to save an empty project (no tracks)
        if not self.project.tracks:
            return True
        reply = QMessageBox.warning(
            self, 'Unsaved Changes',
            'The current project has unsaved changes.\nDo you want to save before continuing?',
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Save,
        )
        if reply == QMessageBox.Save:
            self.save_project()
            return not self._project_dirty  # False if save was cancelled
        elif reply == QMessageBox.Discard:
            return True
        else:
            return False

    def closeEvent(self, event) -> None:
        if not self._check_unsaved():
            event.ignore()
            return
        if self.audio_engine is not None:
            self.audio_engine.stop()
            if hasattr(self.audio_engine, '_stream') and self.audio_engine._stream is not None:
                try:
                    self.audio_engine._stream.close()
                except Exception:
                    pass
        super().closeEvent(event)

    # --------- Recent Projects ---------

    _RECENT_FILE = os.path.join(os.path.expanduser('~'), '.simplesound_recent.json')
    _MAX_RECENT = 5

    def _load_recent_projects(self) -> List[str]:
        try:
            if os.path.exists(self._RECENT_FILE):
                with open(self._RECENT_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                return [p for p in data if isinstance(p, str) and os.path.exists(p)][:self._MAX_RECENT]
        except Exception:
            pass
        return []

    def _save_recent_projects(self, paths: List[str]) -> None:
        try:
            with open(self._RECENT_FILE, 'w', encoding='utf-8') as f:
                json.dump(paths[:self._MAX_RECENT], f)
        except Exception:
            pass

    def _add_recent_project(self, path: str) -> None:
        recents = self._load_recent_projects()
        path = os.path.abspath(path)
        recents = [p for p in recents if os.path.abspath(p) != path]
        recents.insert(0, path)
        self._save_recent_projects(recents[:self._MAX_RECENT])
        self._rebuild_recent_menu()

    def _rebuild_recent_menu(self) -> None:
        self.recent_menu.clear()
        recents = self._load_recent_projects()
        if not recents:
            act = self.recent_menu.addAction('(no recent projects)')
            act.setEnabled(False)
            return
        for path in recents:
            name = os.path.basename(path)
            act = self.recent_menu.addAction(name)
            act.setToolTip(path)
            act.triggered.connect(lambda checked, p=path: self.load_project(p))

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

    # --------- Window-level Drag & Drop ---------

    _AUDIO_EXTS = {'.wav', '.mp3', '.flac', '.ogg', '.m4a', '.aac'}

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            if any(
                os.path.splitext(url.toLocalFile())[1].lower() in self._AUDIO_EXTS
                for url in event.mimeData().urls() if url.isLocalFile()
            ):
                event.acceptProposedAction()
                return
        event.ignore()

    def dragMoveEvent(self, event) -> None:
        event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        if not event.mimeData().hasUrls():
            return
        paths = []
        for url in event.mimeData().urls():
            if url.isLocalFile():
                path = url.toLocalFile()
                if os.path.splitext(path)[1].lower() in self._AUDIO_EXTS:
                    paths.append(path)
        if paths:
            self._load_tracks_with_progress(paths)
            event.acceptProposedAction()
