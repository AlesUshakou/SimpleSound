# Changelog

# 28.04.2026

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
- `mouseDoubleClickEvent`: doesn't change `selected_track` when solo active
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
- `_sync_ui()` + `_apply_audio_meters()`: force `selected_track = solo_idx` when solo active
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
