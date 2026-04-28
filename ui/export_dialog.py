"""Export dialog — renders the project mix to an audio file.

Supports WAV, MP3, OGG, FLAC, M4A with per-format parameters.
Rendering reuses the same mix logic as the real-time audio engine.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

TARGET_SAMPLE_RATE = 48000
TARGET_CHANNELS = 2

# ---------------------------------------------------------------------------
#  Format descriptors
# ---------------------------------------------------------------------------

FORMATS: Dict[str, Dict[str, Any]] = {
    'WAV': {
        'ext': '.wav',
        'filter': 'WAV Audio (*.wav)',
        'params': {
            'sample_rate': {'label': 'Sample Rate (Hz)', 'type': 'combo',
                            'options': ['22050', '44100', '48000', '96000'], 'default': '48000'},
            'bit_depth': {'label': 'Bit Depth', 'type': 'combo',
                          'options': ['16', '24', '32'], 'default': '24'},
        },
    },
    'MP3': {
        'ext': '.mp3',
        'filter': 'MP3 Audio (*.mp3)',
        'params': {
            'bitrate': {'label': 'Bitrate (kbps)', 'type': 'combo',
                        'options': ['128', '192', '256', '320'], 'default': '320'},
            'sample_rate': {'label': 'Sample Rate (Hz)', 'type': 'combo',
                            'options': ['22050', '44100', '48000'], 'default': '44100'},
        },
    },
    'OGG': {
        'ext': '.ogg',
        'filter': 'OGG Vorbis (*.ogg)',
        'params': {
            'bitrate': {'label': 'Bitrate (kbps)', 'type': 'combo',
                        'options': ['96', '128', '192', '256', '320'], 'default': '192'},
            'sample_rate': {'label': 'Sample Rate (Hz)', 'type': 'combo',
                            'options': ['22050', '44100', '48000'], 'default': '44100'},
        },
    },
    'FLAC': {
        'ext': '.flac',
        'filter': 'FLAC Audio (*.flac)',
        'params': {
            'sample_rate': {'label': 'Sample Rate (Hz)', 'type': 'combo',
                            'options': ['44100', '48000', '96000'], 'default': '48000'},
            'bit_depth': {'label': 'Bit Depth', 'type': 'combo',
                          'options': ['16', '24'], 'default': '24'},
        },
    },
    'M4A': {
        'ext': '.m4a',
        'filter': 'AAC / M4A (*.m4a)',
        'params': {
            'bitrate': {'label': 'Bitrate (kbps)', 'type': 'combo',
                        'options': ['128', '192', '256', '320'], 'default': '256'},
            'sample_rate': {'label': 'Sample Rate (Hz)', 'type': 'combo',
                            'options': ['44100', '48000'], 'default': '44100'},
        },
    },
}

FORMAT_NAMES = list(FORMATS.keys())

# ---------------------------------------------------------------------------
#  Offline renderer (runs in QThread)
# ---------------------------------------------------------------------------

class _RenderWorker(QObject):
    progress = Signal(int)       # percent 0-100
    finished = Signal(str)       # path on success
    failed = Signal(str)         # error message

    def __init__(self, project: Any, fmt: str, params: Dict[str, str], path: str) -> None:
        super().__init__()
        self.project = project
        self.fmt = fmt
        self.params = params
        self.path = path
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    # -- build render data (same logic as audio_engine._build_render_tracks) --

    @staticmethod
    def _build_render_tracks(project: Any) -> List[dict]:
        """Build lightweight render-track dicts from the project model."""
        tracks: List[dict] = []
        all_indexes = set(range(len(project.tracks)))
        for index, track in enumerate(project.tracks):
            if index not in all_indexes:
                continue
            audio = getattr(track, 'audio_data', None)
            duration = float(getattr(track, 'duration', 0.0))
            if track.mute:
                continue
            raw_segments = getattr(track, 'segments', None) or []

            has_any_audio = (audio is not None and getattr(audio, 'size', 0) > 0)
            if not has_any_audio:
                has_any_audio = any(
                    getattr(seg, 'source_audio', None) is not None
                    and getattr(getattr(seg, 'source_audio', None), 'size', 0) > 0
                    for seg in raw_segments
                )
            if not has_any_audio:
                continue

            segments: List[Tuple[float, float, float, np.ndarray]] = []
            if not raw_segments:
                if duration > 0.0 and audio is not None:
                    segments = [(0.0, duration, 0.0, audio)]
            else:
                for seg in raw_segments:
                    s = max(0.0, float(getattr(seg, 'start', 0.0)))
                    e = min(duration + 120.0, float(getattr(seg, 'end', duration)))
                    ss = max(0.0, float(getattr(seg, 'source_start', s)))
                    seg_audio = getattr(seg, 'source_audio', None)
                    if seg_audio is None:
                        seg_audio = audio
                    if seg_audio is None:
                        continue
                    if not (isinstance(seg_audio, np.ndarray) and seg_audio.dtype == np.float32):
                        seg_audio = np.ascontiguousarray(np.asarray(seg_audio, dtype=np.float32))
                    if e > s:
                        segments.append((s, e, ss, seg_audio))
            if not segments:
                continue

            points = sorted(getattr(track, 'automation_points', []),
                            key=lambda p: getattr(p, 'time', 0.0))
            if not points:
                atimes = np.array([0.0, duration], dtype=np.float32)
                avals = np.array([0.5, 0.5], dtype=np.float32)
            else:
                atimes = np.array([float(getattr(p, 'time', 0.0)) for p in points], dtype=np.float32)
                avals = np.array([float(getattr(p, 'value', 0.5)) for p in points], dtype=np.float32)
                if atimes[0] > 0.0:
                    atimes = np.insert(atimes, 0, 0.0)
                    avals = np.insert(avals, 0, avals[0])
                if duration > 0.0 and atimes[-1] < duration:
                    atimes = np.append(atimes, duration)
                    avals = np.append(avals, avals[-1])

            tracks.append({
                'segments': segments,
                'atimes': atimes,
                'avals': avals,
            })
        return tracks

    # -- mix one block (same logic as audio_engine._mix_track) --

    @staticmethod
    def _mix_block(render_tracks: List[dict], start: float, frames: int,
                   sr: int, channels: int) -> np.ndarray:
        out = np.zeros((frames, channels), dtype=np.float32)
        end_time = start + frames / float(sr)
        for rt in render_tracks:
            sample_times = start + np.arange(frames, dtype=np.float32) / float(sr)
            gains = np.interp(sample_times, rt['atimes'], rt['avals']).astype(np.float32).reshape(-1, 1)
            for seg_start, seg_end, source_start, seg_audio in rt['segments']:
                if seg_audio is None or seg_audio.size == 0:
                    continue
                ov_start = max(start, seg_start)
                ov_end = min(end_time, seg_end)
                if ov_end <= ov_start:
                    continue
                dst = int(round((ov_start - start) * sr))
                take = int(round((ov_end - ov_start) * sr))
                take = min(take, frames - dst)
                src = int(round(source_start * sr)) + int(round((ov_start - seg_start) * sr))
                if src < 0 or src >= seg_audio.shape[0]:
                    continue
                take = min(take, seg_audio.shape[0] - src)
                if take <= 0:
                    continue
                sl = seg_audio[src: src + take]
                # Handle mono source into stereo output
                if sl.ndim == 1:
                    sl = sl.reshape(-1, 1)
                if sl.shape[1] < channels:
                    sl = np.broadcast_to(sl, (sl.shape[0], channels)).copy()
                out[dst: dst + take] += sl[:, :channels] * gains[dst: dst + take]
        return np.clip(out, -1.0, 1.0)

    # -- main render entry --

    def run(self) -> None:
        try:
            from pydub import AudioSegment
        except ImportError:
            self.failed.emit('pydub is not installed.')
            return

        try:
            project = self.project
            duration = project.duration()
            if duration <= 0.0:
                self.failed.emit('Project is empty — nothing to export.')
                return

            sr = int(self.params.get('sample_rate', str(TARGET_SAMPLE_RATE)))
            channels = TARGET_CHANNELS
            render_tracks = self._build_render_tracks(project)
            if not render_tracks:
                self.failed.emit('No audible tracks to export (all muted or empty).')
                return

            total_frames = int(round(duration * sr))
            block_size = sr  # 1-second blocks
            result = np.zeros((total_frames, channels), dtype=np.float32)
            written = 0

            while written < total_frames:
                if self._cancel:
                    self.failed.emit('Export cancelled.')
                    return
                take = min(block_size, total_frames - written)
                start_sec = written / float(sr)
                block = self._mix_block(render_tracks, start_sec, take, sr, channels)
                result[written: written + take] = block
                written += take
                self.progress.emit(int(100 * written / total_frames))

            # Convert float32 [-1,1] → int samples for pydub
            bit_depth = int(self.params.get('bit_depth', '16'))
            sample_width = bit_depth // 8
            if bit_depth == 32:
                # pydub supports up to 32-bit, store as int32
                pcm = (result * 2147483647.0).astype(np.int32)
            elif bit_depth == 24:
                # pydub 24-bit: store as int32, set sample_width=3
                pcm = (result * 8388607.0).astype(np.int32)
                sample_width = 3
            else:
                pcm = (result * 32767.0).astype(np.int16)
                sample_width = 2

            seg = AudioSegment(
                data=pcm.tobytes(),
                sample_width=sample_width,
                frame_rate=sr,
                channels=channels,
            )

            fmt = self.fmt.upper()
            export_params: Dict[str, Any] = {}
            if fmt == 'MP3':
                export_params['format'] = 'mp3'
                export_params['bitrate'] = f'{self.params.get("bitrate", "320")}k'
            elif fmt == 'OGG':
                export_params['format'] = 'ogg'
                export_params['codec'] = 'libvorbis'
                export_params['bitrate'] = f'{self.params.get("bitrate", "192")}k'
            elif fmt == 'FLAC':
                export_params['format'] = 'flac'
            elif fmt == 'M4A':
                export_params['format'] = 'ipod'
                export_params['codec'] = 'aac'
                export_params['bitrate'] = f'{self.params.get("bitrate", "256")}k'
            else:
                export_params['format'] = 'wav'

            seg.export(self.path, **export_params)
            self.progress.emit(100)
            self.finished.emit(self.path)

        except Exception as exc:
            self.failed.emit(str(exc))


# ---------------------------------------------------------------------------
#  Export Dialog
# ---------------------------------------------------------------------------

_DIALOG_STYLE = """
QDialog {
    background: #1E222A;
    color: #EAECEF;
}
QLabel {
    color: #EAECEF;
    background: transparent;
}
QGroupBox {
    color: #EAECEF;
    border: 1px solid #303643;
    border-radius: 8px;
    margin-top: 14px;
    padding: 22px 14px 16px 14px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 14px;
    padding: 0 6px;
    color: #FF8A3D;
}
QComboBox, QSpinBox, QLineEdit {
    background: #262B35;
    color: #EAECEF;
    border: 1px solid #3A4050;
    border-radius: 6px;
    padding: 5px 10px;
    min-height: 26px;
}
QComboBox:hover, QSpinBox:hover, QLineEdit:hover {
    border-color: #FF8A3D;
}
QComboBox::drop-down {
    border: none;
    width: 24px;
}
QComboBox QAbstractItemView {
    background: #262B35;
    color: #EAECEF;
    selection-background-color: #FF8A3D;
    border: 1px solid #3A4050;
}
QPushButton {
    background: #2E3440;
    color: #EAECEF;
    border: 1px solid #3A4050;
    border-radius: 6px;
    padding: 7px 18px;
    font-weight: 600;
    min-height: 28px;
}
QPushButton:hover {
    background: #3A4050;
    border-color: #FF8A3D;
}
QPushButton#export_btn {
    background: #FF8A3D;
    color: #1E222A;
    border: none;
    font-size: 13px;
    padding: 8px 32px;
}
QPushButton#export_btn:hover {
    background: #FFa060;
}
QPushButton#export_btn:disabled {
    background: #4A4A4A;
    color: #888;
}
QProgressBar {
    background: #262B35;
    border: 1px solid #303643;
    border-radius: 6px;
    text-align: center;
    color: #EAECEF;
    min-height: 22px;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #FF8A3D, stop:1 #FFa864);
    border-radius: 5px;
}
"""


class ExportDialog(QDialog):
    """Modal dialog for exporting the project mix to an audio file."""

    def __init__(self, project: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.project = project
        self._thread: Optional[QThread] = None
        self._worker: Optional[_RenderWorker] = None
        self.setWindowTitle('Export Audio')
        self.setFixedSize(520, 460)
        self.setStyleSheet(_DIALOG_STYLE)
        self._build_ui()

    # ---- UI ----

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(12)
        root.setContentsMargins(20, 16, 20, 16)

        # Title
        title = QLabel('Export Audio')
        title.setFont(QFont('Segoe UI', 16, QFont.Bold))
        title.setStyleSheet('color: #EAECEF;')
        root.addWidget(title)

        # Format selector
        fmt_row = QHBoxLayout()
        fmt_row.setSpacing(10)
        fmt_label = QLabel('Format:')
        fmt_label.setFixedWidth(60)
        self.fmt_combo = QComboBox()
        self.fmt_combo.addItems(FORMAT_NAMES)
        self.fmt_combo.setCurrentText('WAV')
        self.fmt_combo.currentTextChanged.connect(self._on_format_changed)
        fmt_row.addWidget(fmt_label)
        fmt_row.addWidget(self.fmt_combo, 1)
        root.addLayout(fmt_row)

        # Stacked parameter panels (one per format)
        self.params_group = QGroupBox('Parameters')
        params_layout = QVBoxLayout(self.params_group)
        self.param_stack = QStackedWidget()
        self.param_widgets: Dict[str, Dict[str, QComboBox]] = {}

        for fmt_name in FORMAT_NAMES:
            page = QWidget()
            page_layout = QVBoxLayout(page)
            page_layout.setContentsMargins(0, 4, 0, 4)
            page_layout.setSpacing(12)
            widgets: Dict[str, QComboBox] = {}
            for key, spec in FORMATS[fmt_name]['params'].items():
                row = QHBoxLayout()
                lbl = QLabel(spec['label'])
                lbl.setFixedWidth(160)
                combo = QComboBox()
                combo.addItems(spec['options'])
                combo.setCurrentText(spec['default'])
                row.addWidget(lbl)
                row.addWidget(combo, 1)
                page_layout.addLayout(row)
                widgets[key] = combo
            page_layout.addStretch(1)
            self.param_widgets[fmt_name] = widgets
            self.param_stack.addWidget(page)

        params_layout.addWidget(self.param_stack)
        self.param_stack.setMinimumHeight(90)
        root.addWidget(self.params_group)

        # Output path
        path_group = QGroupBox('Output')
        path_layout = QHBoxLayout(path_group)
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText('Choose output file...')
        self.path_edit.setReadOnly(True)
        browse_btn = QPushButton('Browse…')
        browse_btn.setFixedWidth(90)
        browse_btn.clicked.connect(self._browse_path)
        path_layout.addWidget(self.path_edit, 1)
        path_layout.addWidget(browse_btn)
        root.addWidget(path_group)

        # Progress bar (hidden initially)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        root.addWidget(self.progress_bar)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.cancel_btn = QPushButton('Cancel')
        self.cancel_btn.clicked.connect(self._on_cancel)
        self.export_btn = QPushButton('Export')
        self.export_btn.setObjectName('export_btn')
        self.export_btn.clicked.connect(self._on_export)
        btn_row.addWidget(self.cancel_btn)
        btn_row.addWidget(self.export_btn)
        root.addLayout(btn_row)

        # Set default path suggestion
        self._suggest_default_path()

    # ---- Slots ----

    def _on_format_changed(self, fmt_name: str) -> None:
        idx = FORMAT_NAMES.index(fmt_name) if fmt_name in FORMAT_NAMES else 0
        self.param_stack.setCurrentIndex(idx)
        # Update file extension in path
        current = self.path_edit.text()
        if current:
            base, _ = os.path.splitext(current)
            self.path_edit.setText(base + FORMATS[fmt_name]['ext'])

    def _browse_path(self) -> None:
        fmt_name = self.fmt_combo.currentText()
        fmt_info = FORMATS.get(fmt_name, FORMATS['WAV'])
        default_path = self.path_edit.text() or ''
        path, _ = QFileDialog.getSaveFileName(
            self, 'Export Audio', default_path,
            fmt_info['filter'],
        )
        if path:
            ext = fmt_info['ext']
            if not path.lower().endswith(ext):
                path += ext
            self.path_edit.setText(path)

    def _suggest_default_path(self) -> None:
        project_path = getattr(self.project, 'project_path', None)
        if project_path:
            base = os.path.splitext(project_path)[0]
        else:
            base = os.path.join(os.path.expanduser('~'), 'export')
        fmt_name = self.fmt_combo.currentText()
        ext = FORMATS.get(fmt_name, FORMATS['WAV'])['ext']
        self.path_edit.setText(base + ext)

    def _collect_params(self) -> Dict[str, str]:
        fmt_name = self.fmt_combo.currentText()
        widgets = self.param_widgets.get(fmt_name, {})
        return {key: combo.currentText() for key, combo in widgets.items()}

    def _on_export(self) -> None:
        path = self.path_edit.text().strip()
        if not path:
            QMessageBox.warning(self, 'Export', 'Please choose an output file path.')
            return
        if not self.project.tracks:
            QMessageBox.warning(self, 'Export', 'Project is empty — nothing to export.')
            return

        fmt_name = self.fmt_combo.currentText()
        params = self._collect_params()

        # Disable controls during export
        self.export_btn.setEnabled(False)
        self.fmt_combo.setEnabled(False)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)

        self._thread = QThread(self)
        self._worker = _RenderWorker(self.project, fmt_name, params, path)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def _on_progress(self, percent: int) -> None:
        self.progress_bar.setValue(percent)

    def _on_finished(self, path: str) -> None:
        self.progress_bar.setValue(100)
        self.export_btn.setEnabled(True)
        self.fmt_combo.setEnabled(True)
        QMessageBox.information(
            self, 'Export Complete',
            f'Audio exported successfully.\n\n{path}',
        )
        self.accept()

    def _on_failed(self, message: str) -> None:
        self.progress_bar.setVisible(False)
        self.export_btn.setEnabled(True)
        self.fmt_combo.setEnabled(True)
        QMessageBox.critical(self, 'Export Failed', message)

    def _on_cancel(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
        self.reject()
