# Changelog

# 28 April 2026 — Session 2

## export_dialog.py *(new file — `ui/`)*
- Full export dialog: WAV, MP3, OGG, FLAC, M4A with per-format parameters (sample rate, bit depth, bitrate)
- Offline renderer in QThread — same mix logic as audio engine (segments, cross-track audio, automation gain)
- Progress bar, cancel support, Browse path picker, default path from project name
- Dark-themed UI matching the app style (orange accent, grouped parameters)

## models.py
- `__post_init__`: default segment only created when `self.file_path` is set — empty tracks stay segment-free
- `ensure_full_segment()`: preserves `source_audio`, `source_mipmaps`, `source_sample_rate` on rebuilt segments
- `cut_at()`: preserves cross-track audio references on both halves
- `delete_selection()`: preserves cross-track audio references on remaining pieces
- `move_segment()`: preserves cross-track audio references
- `trim_segment()`: preserves cross-track audio references (both left and right edge)
- `move_selection()`: preserves cross-track audio references on moved and untouched pieces
- `merge_segments()`: preserves cross-track audio references on merged result

## canvas.py
- `_draw_automation_line()`: added `painter.setBrush(Qt.NoBrush)` before `drawPath` — fixes white fill artifact on empty tracks caused by leftover brush from segment drawing
- `mouseDoubleClickEvent`: removed `has_solo` guard — track selection now works freely even when another track is solo'd
- `merge_selected_segments()`: preserves cross-track audio references on merged segment

## main_window.py
- Added `File → Export Audio…` (`Ctrl+E`) — opens `ExportDialog`
- Added window-level drag & drop (`dragEnterEvent`/`dropEvent` on `MainWindow`) — audio files can be dropped anywhere in the window, not just on the canvas
- `__init__`: `_project_dirty = False` after initial `record_history()` — new empty project is not marked dirty
- `_check_unsaved()`: skips save prompt when project has no tracks (empty project)
- `_sync_ui()`: removed forced `selected_track = solo_idx` — solo no longer hijacks track selection
- `_apply_audio_meters()`: removed forced `selected_track = solo_idx` — same fix during playback polling
- `solo_track_by_number()`: added `canvas.update()` — track highlight repaints immediately when pressing 1–9
- `add_empty_track()`: uses `project.duration()` for track duration and automation span; clears segments explicitly
- `_restore_project_from_dict()`: fallback segment only created for tracks with `file_path`; preserves cross-track audio references

---

# 28 April 2026 — Session 1

## models.py
- `TrackModel.locked` default `True` → `False` (unlocked by default)
- `TrackSegment`: added `source_audio`, `source_mipmaps`, `source_sample_rate` fields for cross-track audio
- `TrackSegment`: added `effective_audio()`, `effective_mipmaps()`, `effective_sample_rate()` helpers
- `TrackModel`: added `can_accept_segment()`, `find_nearest_gap()` for cross-track validation
- `TrackModel.move_segment()`: rewritten with delta clamping (snap to neighbors)
- `ProjectModel`: removed `selection_range` (blue selection removed)
- `ProjectModel`: added `selected_gap` field `(track_idx, left_seg_idx, right_seg_idx)` for gap selection

## canvas.py
- Removed blue selection: `selection_changed` signal, drag creation, overlay drawing, selection contains check
- Added `files_dropped` signal + drag-n-drop handlers for audio files
- Added `audio_mix_changed` signal (emitted on cross-track drop)
- Added `_is_track_locked()` — per-track lock check
- Added cross-track drag: ghost preview (green=fits, red=doesn't), dim source segment, snapshot with audio refs
- `_draw_segments()`: draws gap highlight (orange dashed) when `selected_gap` is set
- `_draw_waveform()`/`_draw_segment_waveform()`: uses `seg.effective_*()` for cross-track waveform
- `mousePressEvent`: clicking empty area/gap selects the track + detects gap; clears gap on segment/point clicks
- `setAcceptDrops(True)` enabled
- `segments_locked` default → `False`
- Added `_find_gap_at()` method for gap detection

## widgets.py
- `TrackHeaderRow`: removed delete button (`✕`); `lock_toggled` → `Signal(int, bool)` per-track
- `TrackHeaderPanel`: added toolbar with `+` (orange) and trash buttons; `add_empty_track_requested`, `remove_selected_track_requested` signals
- `TrackHeaderPanel`: drag-to-reorder checks per-track `locked`
- `TrackHeaderPanel`: replaced `ruler_spacer` with toolbar; `set_segments_locked` → `set_track_locked`

## main_window.py
- Added `File → New Project` (Ctrl+N): creates empty project with unsaved check
- Added `File → Recent Projects` submenu (last 5 in `~/.simplesound_recent.json`)
- Added `_project_dirty` flag: set on `record_history`, cleared on save/new/load
- Added `_check_unsaved()`: warns before New/Open/Close if dirty; Save/Discard/Cancel dialog
- Added `closeEvent()`: checks unsaved, cleanly closes audio stream
- `add_empty_track()`: creates track with `locked=False` (was `True`)
- `merge_selected_segments()`: if gap selected → closes gap by sliding right segment left; otherwise merges segments
- Removed clipboard (copy/paste), blue selection
- `set_segments_locked` → `set_track_locked` (per-track); `toggle_segments_lock` toggles selected track
- Undo/Redo preserves playhead position
- Connected `canvas.files_dropped`, `canvas.audio_mix_changed`

## audio_engine.py
- Stream pre-opened at init for zero-latency playback; `stop()` keeps stream warm
- `SnapshotTrack.segments`: `List[Tuple[float,float,float,np.ndarray]]` — per-segment audio
- `_build_render_tracks()`: doesn't skip tracks with no own audio if cross-track segments exist
- `_mix_track()`: uses per-segment audio from tuple

## help_dialog.py
- Updated all shortcuts and Quick Start for: per-track lock, cross-track drag, drag-n-drop, track reorder, add/remove buttons, recent projects, gap selection
- Removed blue selection and copy/paste references

---

# 14 April 2026

**UI pass: redesigned bottom transport bar, segment lock, peak jump, empty-state canvas.**

#### New features

- **Segment lock.** Each track header now has a lock button (SVG icon). Locked by default to prevent accidental segment moves or trims while navigating. Press `L` to toggle lock on all tracks at once. While locked, hand and resize cursors are hidden over segments, and drag / trim are disabled.
- **Peak jump.** Two new buttons flank the Play button: jump to the previous / next loud peak in the waveform. Shortcuts: `Shift + Space` (next), `Ctrl + Shift + Space` (previous). Works across all loaded tracks, using mipmap data with an adaptive 92nd-percentile threshold and distance-weighted scoring.
- **Empty state.** With no tracks loaded, the timeline ruler and playhead are hidden. The canvas shows a subtle grid and a centred *"Drag & drop audio files here"* badge instead.
- **Separate About entry.** Help menu now has two items: **Help & Shortcuts** (`F1`) and **About SimpleSound**. Both open the same dialog; About opens it directly on the About tab.

#### Redesign

- **Bottom transport bar.** Redesigned in a studio style with three dark capsule groups, gradient backgrounds, and accent glow on hover. New layout: `[transport + edit] ← stretch → [time display] ← stretch → [zoom]`.
- **Play button** squared off (10 px radius), sized to match the transport row.
- **Time display** moved from the top bar to the bottom centre, styled as an LCD-style readout.
- **Zoom slider** with gradient sub-page and glowing handle, plus a `1:1` reset button.
- **All transport icons** replaced with inline SVG — crisp at any DPI, easily recolourable.
- **Copyright link** in the status bar: fixed stray underline that Qt's rich-text renderer added despite the stylesheet.

#### New shortcuts

| Shortcut | Action |
| --- | --- |
| `Shift + Space` | Jump to next peak |
| `Ctrl + Shift + Space` | Jump to previous peak |
| `L` | Toggle segment lock on all tracks |
