from __future__ import annotations

import os
from typing import List, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)


_ICON_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'assets', 'icons', 'app.svg',
)


SHORTCUTS: List[Tuple[str, List[Tuple[str, str]]]] = [
    ('Transport', [
        ('Space', 'Play / Pause'),
        ('Shift + Space', 'Jump to next peak (right of playhead)'),
        ('Ctrl + Shift + Space', 'Jump to previous peak (left of playhead)'),
        ('Home', 'Jump to start'),
        ('End', 'Jump to end'),
        ('1 – 9', 'Solo track by number (press again to un-solo)'),
    ]),
    ('Editing', [
        ('C', 'Cut at playhead on the selected track'),
        ('M', 'Close gaps between selected segments'),
        ('L', 'Toggle segment lock on all tracks'),
        ('Delete', 'Delete selection / selected segment / automation point'),
        ('Ctrl + C', 'Copy selection or selected segment'),
        ('Ctrl + V', 'Paste at playhead'),
        ('Ctrl + Z', 'Undo'),
        ('Ctrl + Shift + Z / Ctrl + Y', 'Redo'),
    ]),
    ('View & Navigation', [
        ('Ctrl + Wheel', 'Zoom around playhead'),
        ('Shift + Wheel', 'Horizontal scroll (timeline)'),
        ('Wheel', 'Vertical scroll (tracks)'),
        ('0', 'Reset zoom'),
        ('F1', 'Open this Help dialog'),
    ]),
    ('Mouse', [
        ('Click on waveform', 'Move playhead'),
        ('Click + drag', 'Create time selection'),
        ('Ctrl + Click on track', 'Add automation point'),
        ('Drag automation point', 'Move automation point'),
        ('Shift + Click on segment', 'Add segment to multi-selection'),
        ('Double-click on segment', 'Select / deselect segment'),
        ('Drag segment body', 'Move segment along timeline (unlocked only)'),
        ('Drag segment edge', 'Trim segment (unlocked only)'),
        ('Right-click', 'Context menu'),
    ]),
    ('Project', [
        ('Ctrl + O', 'Open audio files'),
        ('Ctrl + Shift + O', 'Open project'),
        ('Ctrl + S', 'Save project'),
        ('Ctrl + Shift + S', 'Save project as...'),
    ]),
]


QUICK_START_TEXT = """
<h3 style='color:#FF8A3D;'>Getting started</h3>
<ol style='line-height:1.6;'>
  <li><b>Load audio.</b> <code>File → Open Tracks…</code> and pick WAV / MP3 / FLAC / OGG / M4A.
      Each file becomes a separate track stacked vertically.</li>
  <li><b>Play / navigate.</b> Click on the waveform to move the playhead, or press
      <code>Space</code> to play. Home / End jump to start / end. Use
      <code>Shift + Space</code> / <code>Ctrl + Shift + Space</code> to hop between
      loud peaks in the waveform.</li>
  <li><b>Select &amp; cut.</b> Drag on a track to select a time range, then press
      <code>Delete</code> to remove it. Press <code>C</code> to split a segment at the playhead.</li>
  <li><b>Rearrange.</b> Unlock segments first (lock icon on the track header or
      press <code>L</code>), then drag a segment's body to move it, or drag its
      left/right edge to trim. Hold <code>Shift</code> and click segments to
      multi-select, then press <code>M</code> to close the gaps between them
      (right segments slide flush against the left one).</li>
  <li><b>Automate volume.</b> <code>Ctrl + Click</code> on a track adds an automation point.
      Drag points up/down to change the gain along the timeline.</li>
  <li><b>Solo / Mute.</b> Click <b>S</b> or <b>M</b> on the track header. Numbers
      <code>1…9</code> solo the N-th track.</li>
  <li><b>Save.</b> <code>Ctrl + S</code> saves the project as <code>.ssproj</code>
      (references source audio files by path).</li>
</ol>

<h3 style='color:#FF8A3D;margin-top:18px;'>Segment lock</h3>
<p>The <b>lock icon</b> on each track header (and the global <code>L</code> shortcut)
toggles whether segments can be moved or trimmed. <b>Locked is the default</b> —
this prevents accidental edits while you navigate, select, and play back. Hand
and resize cursors are hidden over segments while locked. Unlock to rearrange,
then lock again when you're done.</p>

<h3 style='color:#FF8A3D;margin-top:14px;'>Peak jump</h3>
<p>The <b>peak-jump buttons</b> flank the Play button in the bottom bar, and
their shortcuts <code>Shift + Space</code> (next) and
<code>Ctrl + Shift + Space</code> (previous) snap the playhead to the nearest
loud peak in the waveform. Useful for quickly stepping between drum hits,
vocal onsets, or other transients without zooming in.</p>
"""


ABOUT_TEXT = """
<h2 style='color:#FF8A3D;margin:0;'>SimpleSound</h2>
<p style='color:#9BA6B2;margin:2px 0 14px 0;'>Multitrack audio editor</p>

<p>A lightweight multitrack editor for quickly aligning, cutting and mixing audio files.
Built with Python + PySide6, using PortAudio for low-latency playback.</p>

<p style='margin-top:14px;'>
  <b>Author:</b> Aleš Ushakou<br>
  <b>Year:</b> 2026<br>
  <b>License:</b> MIT
</p>

<p style='margin-top:14px;'>
  <a href='https://www.linkedin.com/in/ales-ushakou' style='color:#FF8A3D;'>LinkedIn</a>
</p>
"""


class HelpDialog(QDialog):
    TAB_SHORTCUTS = 'shortcuts'
    TAB_QUICK_START = 'quick_start'
    TAB_ABOUT = 'about'

    def __init__(self, parent=None, initial_tab: str = TAB_SHORTCUTS):
        super().__init__(parent)
        self.setWindowTitle('SimpleSound — Help')
        self.setMinimumSize(640, 560)
        self.setStyleSheet(
            'QDialog { background:#181A1F; }'
            'QTabWidget::pane { border:1px solid #303643; background:#20242C; border-radius:6px; }'
            'QTabBar::tab { background:#262B35; color:#9BA6B2; padding:8px 18px;'
            '  border:1px solid #303643; border-bottom:none; '
            '  border-top-left-radius:6px; border-top-right-radius:6px; margin-right:2px; }'
            'QTabBar::tab:selected { background:#20242C; color:#FF8A3D; }'
            'QLabel { color:#EAECEF; }'
            'QPushButton { background:#262B35; color:#EAECEF; border:1px solid #343B48;'
            '  border-radius:7px; padding:7px 22px; font-weight:700; }'
            'QPushButton:hover { background:#2F3642; border-color:#4A5260; }'
            'QScrollArea { border:none; background:transparent; }'
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 14)
        root.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(14)
        icon_label = QLabel()
        if os.path.exists(_ICON_PATH):
            pix = QIcon(_ICON_PATH).pixmap(56, 56)
            icon_label.setPixmap(pix)
        icon_label.setFixedSize(60, 60)
        header.addWidget(icon_label, 0, Qt.AlignTop)

        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title = QLabel('SimpleSound')
        title.setStyleSheet('color:#EAECEF;font-size:20px;font-weight:800;')
        subtitle = QLabel('Multitrack audio editor — Help & Reference')
        subtitle.setStyleSheet('color:#9BA6B2;font-size:12px;')
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        title_box.addStretch(1)
        header.addLayout(title_box, 1)
        root.addLayout(header)

        self.tabs = QTabWidget()
        # Индексы табов фиксируем по порядку добавления
        self._tab_indexes = {
            self.TAB_SHORTCUTS: self.tabs.addTab(self._build_shortcuts_tab(), 'Shortcuts'),
            self.TAB_QUICK_START: self.tabs.addTab(self._build_text_tab(QUICK_START_TEXT), 'Quick Start'),
            self.TAB_ABOUT: self.tabs.addTab(self._build_text_tab(ABOUT_TEXT), 'About'),
        }
        root.addWidget(self.tabs, 1)

        # Устанавливаем стартовую вкладку
        if initial_tab in self._tab_indexes:
            self.tabs.setCurrentIndex(self._tab_indexes[initial_tab])

        footer = QHBoxLayout()
        footer.addStretch(1)
        close_btn = QPushButton('Close')
        close_btn.clicked.connect(self.accept)
        close_btn.setCursor(Qt.PointingHandCursor)
        footer.addWidget(close_btn)
        root.addLayout(footer)

    def _build_shortcuts_tab(self) -> QWidget:
        container = QWidget()
        outer = QVBoxLayout(container)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(10)

        for section_title, rows in SHORTCUTS:
            header = QLabel(section_title)
            header.setStyleSheet(
                'color:#FF8A3D;font-size:13px;font-weight:800;'
                'padding:8px 0 4px 0;letter-spacing:0.5px;'
            )
            outer.addWidget(header)
            for key, desc in rows:
                row = QHBoxLayout()
                row.setSpacing(12)
                k = QLabel(key)
                k.setStyleSheet(
                    'color:#EAECEF;background:#262B35;border:1px solid #343B48;'
                    'border-radius:5px;padding:3px 9px;font-family:Consolas,monospace;'
                    'font-size:11px;font-weight:700;'
                )
                k.setMinimumWidth(170)
                k.setMaximumWidth(220)
                d = QLabel(desc)
                d.setStyleSheet('color:#B9C2CE;font-size:12px;')
                d.setWordWrap(True)
                row.addWidget(k, 0, Qt.AlignTop)
                row.addWidget(d, 1)
                outer.addLayout(row)
        outer.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(container)
        return scroll

    def _build_text_tab(self, html: str) -> QWidget:
        label = QLabel(html.strip())
        label.setWordWrap(True)
        label.setTextFormat(Qt.RichText)
        label.setOpenExternalLinks(True)
        label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        label.setStyleSheet('color:#EAECEF;font-size:13px;padding:14px 18px;')
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(label)
        return scroll
