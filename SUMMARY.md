# SimpleSound — File Summaries

## `models.py` — Data Models & Business Logic
Core data structures for the multitrack editor. Contains `TrackSegment` (time range + source offset), `TrackModel` (per-track state: audio data, segments, automation, mip-maps, per-track lock), `ProjectModel` (track list, playhead, selected items), and `HistoryStack` (undo/redo with deep-copy snapshots, max 200 states). Key operations: `move_segment` with neighbor-clamping (blocks snap flush against neighbors), `trim_segment`, `cut_at`, `merge_segments`, `can_accept_segment` / `find_nearest_gap` (for cross-track drag validation), and `interpolate_gain` (vectorized automation via numpy interp). Audio loaded via `set_audio_data` with automatic MIP-map generation at 10x/100x/1000x factors for efficient waveform rendering.

## `canvas.py` — Timeline Canvas (Waveform + Interaction)
QWidget-based timeline renderer and mouse interaction handler. Draws ruler, vertical grid, per-track waveforms (from MIP-maps), segment rectangles, automation curves/points, and playhead. Supports: segment body drag with neighbor-clamping, edge trimming, cross-track drag with green/red ghost preview, automation point drag, playhead scrub. Per-track lock check (`_is_track_locked`) gates all drag operations. Drag-and-drop of audio files onto the timeline emits `files_dropped` signal. Context menu for automation add/delete, segment delete/merge. Zoom via Ctrl+Wheel with playhead anchoring.

## `widgets.py` — UI Components (Track Headers, Transport Bar, Meters)
`TrackHeaderRow`: per-track strip with Solo/Mute/Automation-reset/Lock buttons. Lock is per-track (signal `lock_toggled(int, bool)`). `TrackHeaderPanel`: scrollable container of track rows with toolbar (+/trash buttons, "Tracks" label). Supports drag-to-reorder tracks (visual indicators: orange border on target, dashed border on source). `BottomTransportBar`: transport capsule (jump-start, peak-prev, play/pause, peak-next, jump-end), edit capsule (cut, merge), zoom slider with 1:1 reset, centered time display. `HorizontalMeter`: dual-channel level meter with green/red zones and peak markers.

## `main_window.py` — Application Window & Orchestration
Main QMainWindow tying everything together. Manages: menu bar (File with recent projects submenu storing last 5, Edit, Help), keyboard shortcuts, audio engine lifecycle, file loading with progress dialog, project save/load (.ssproj JSON), undo/redo (playhead position preserved across undo), track management (add empty, remove selected, reorder), audio metering poll (16ms timer), playhead auto-scroll during playback. Drag-and-drop audio files connected from canvas. Per-track lock toggled via `L` shortcut on selected track.

## `audio_engine.py` — PortAudio Playback Engine
Thread-safe audio backend using sounddevice (PortAudio). Stream pre-opened at init for zero-latency playback start. Callback renders mix from `SnapshotTrack` list with vectorized gain automation. Stop keeps stream warm (outputs silence), play just flips `_playing` flag. Per-track RMS/peak metering computed in callback and exposed via `snapshot()`. Supports loop playback over time ranges.

## `help_dialog.py` — Help & Shortcuts Dialog
Tabbed dialog (Shortcuts, Quick Start, About) with dark theme. Documents all keyboard shortcuts, mouse interactions (including cross-track drag, track reorder, drag-and-drop files), per-track lock behavior, peak jump feature, and recent projects. Styled with orange accent (#FF8A3D).

## `waveform_cache.py` — Waveform Render Cache
LRU-style cache for rendered waveform pixel data, keyed by track revision + zoom level. Invalidation per-track or full clear.

## `loaders.py` — Background Audio File Loader
QObject worker for loading audio files in a QThread with progress signals. Converts via pydub to 48kHz stereo int16, then to float32 numpy arrays.

## `theme.py` — Color Constants
Centralized dark-theme color palette: backgrounds, grids, text, accent orange, meter green/red, playhead, segment borders.
