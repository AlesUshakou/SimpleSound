from __future__ import annotations
import contextlib
import copy
import math
import os
import wave
from array import array
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

TARGET_SAMPLE_RATE = 48000
TARGET_CHANNELS = 2
TARGET_SAMPLE_WIDTH = 2  # int16
CENTER_SNAP_TOLERANCE = 0.035


@dataclass
class AutomationPoint:
    time: float
    value: float


@dataclass
class TrackSegment:
    start: float
    end: float
    source_start: float = 0.0
    # Cross-track audio reference: set when segment came from another track.
    # None means use the owning track's audio_data / mipmaps / sample_rate.
    source_audio: Optional[np.ndarray] = field(default=None, repr=False, compare=False)
    source_mipmaps: Optional[Dict] = field(default=None, repr=False, compare=False)
    source_sample_rate: Optional[int] = field(default=None, repr=False, compare=False)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def source_end(self) -> float:
        return self.source_start + self.duration

    def effective_audio(self, track: 'TrackModel') -> Optional[np.ndarray]:
        """Return audio data for this segment — own source or track's."""
        return self.source_audio if self.source_audio is not None else track.audio_data

    def effective_mipmaps(self, track: 'TrackModel') -> Dict:
        """Return mipmaps for this segment — own source or track's."""
        return self.source_mipmaps if self.source_mipmaps is not None else track.mipmaps

    def effective_sample_rate(self, track: 'TrackModel') -> int:
        """Return sample rate for this segment — own source or track's."""
        return self.source_sample_rate if self.source_sample_rate is not None else track.sample_rate


@dataclass
class ClipboardPayload:
    duration: float = 0.0
    segments: List[TrackSegment] = field(default_factory=list)


@dataclass
class TrackModel:
    track_id: int
    name: str
    file_path: str
    duration: float
    solo: bool = False
    mute: bool = False
    locked: bool = False
    meter_l: float = -60.0
    meter_r: float = -60.0
    meter_peak_l: float = -60.0
    meter_peak_r: float = -60.0
    automation_points: List[AutomationPoint] = field(default_factory=list)
    segments: List[TrackSegment] = field(default_factory=list)
    waveform_peaks: Optional[List[float]] = None
    waveform_peak_resolution: int = 0
    waveform_cache_revision: int = 0
    sample_rate: int = TARGET_SAMPLE_RATE
    channels: int = TARGET_CHANNELS
    audio_data: Optional[np.ndarray] = None
    mipmaps: Dict[int, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.automation_points:
            self.automation_points = [
                AutomationPoint(0.0, 0.5),
                AutomationPoint(max(0.1, self.duration), 0.5),
            ]
        # Only create a default segment for tracks with audio content (file_path set).
        # Intentionally empty tracks (no file_path) must stay segment-free.
        if not self.segments and self.duration > 0 and self.file_path:
            self.segments = [TrackSegment(0.0, self.duration, 0.0)]
        self.ensure_full_segment()

    def clear_automation(self) -> None:
        self.automation_points = [
            AutomationPoint(0.0, 0.5),
            AutomationPoint(max(0.1, self.duration), 0.5),
        ]

    def ensure_full_segment(self) -> None:
        if self.duration <= 0.0:
            return
        if not self.segments:
            return
        cleaned: List[TrackSegment] = []
        for s in self.segments:
            start = max(0.0, float(s.start))
            end = min(self.duration + 120.0, float(s.end))
            if end - start <= 0.0001:
                continue
            source_start = max(0.0, float(getattr(s, 'source_start', start)))
            max_source_start = max(0.0, self.duration - (end - start))
            source_start = min(source_start, max_source_start)
            # Preserve cross-track audio references
            cleaned.append(TrackSegment(
                start, end, source_start,
                source_audio=getattr(s, 'source_audio', None),
                source_mipmaps=getattr(s, 'source_mipmaps', None),
                source_sample_rate=getattr(s, 'source_sample_rate', None),
            ))
        self.segments = sorted(cleaned, key=lambda s: s.start)

    def set_audio_data(self, audio: np.ndarray, sample_rate: int) -> None:
        self.audio_data = np.ascontiguousarray(audio.astype(np.float32, copy=False))
        self.sample_rate = sample_rate
        self.duration = self.audio_data.shape[0] / float(sample_rate)

        # ВАЖНО: создаем сегмент, иначе трек будет невидимым
        if not self.segments:
            self.segments = [TrackSegment(0.0, self.duration, 0.0)]

        # Пересоздаем дефолтную автоматизацию по реальной длительности:
        # ровно 2 точки — одна в начале, одна в конце.
        # Это нужно потому что в __post_init__ duration был 0 и точки легли в (0.0, 0.1).
        if len(self.automation_points) <= 2 and all(
            math.isclose(p.value, 0.5, abs_tol=1e-6) for p in self.automation_points
        ):
            self.automation_points = [
                AutomationPoint(0.0, 0.5),
                AutomationPoint(self.duration, 0.5),
            ]

        # MIP-карты для быстрой отрисовки waveform
        mono = np.mean(self.audio_data, axis=1) if self.audio_data.ndim == 2 else self.audio_data.astype(np.float32)
        self.mipmaps = {1: mono}
        for factor in [10, 100, 1000]:
            reduced_len = len(mono) // factor
            if reduced_len > 0:
                reshaped = mono[:reduced_len * factor].reshape(-1, factor)
                self.mipmaps[factor] = {
                    'max': np.max(reshaped, axis=1),
                    'min': np.min(reshaped, axis=1),
                }

        self.waveform_cache_revision += 1

    def _build_waveform_peaks(self, target_points: int) -> Optional[List[float]]:
        if self.audio_data is None or self.audio_data.size == 0:
            return None
        mono = np.max(np.abs(self.audio_data), axis=1) if self.audio_data.ndim == 2 else np.abs(self.audio_data)
        buckets = max(64, target_points)
        step = max(1, math.ceil(len(mono) / buckets))
        peaks: List[float] = []
        for i in range(0, len(mono), step):
            peaks.append(float(np.max(mono[i:i + step])) if i < len(mono) else 0.0)
        return peaks

    def ensure_waveform_peaks(self, target_points: int = 2048) -> None:
        if self.waveform_peaks is not None and self.waveform_peak_resolution >= target_points:
            return
        if self.audio_data is not None:
            self.waveform_peaks = self._build_waveform_peaks(target_points)
            self.waveform_peak_resolution = len(self.waveform_peaks or [])
            return
        peaks = self._read_waveform_peaks_from_file(target_points)
        if peaks:
            self.waveform_peaks = peaks
            self.waveform_peak_resolution = len(peaks)

    def _read_waveform_peaks_from_file(self, target_points: int) -> Optional[List[float]]:
        if not self.file_path or not os.path.exists(self.file_path):
            return None
        if not self.file_path.lower().endswith(".wav"):
            return None
        try:
            with contextlib.closing(wave.open(self.file_path, "rb")) as wf:
                frame_count = wf.getnframes()
                channels = wf.getnchannels()
                sampwidth = wf.getsampwidth()
                if frame_count <= 0 or channels <= 0 or sampwidth not in (1, 2, 4):
                    return None
                step_frames = max(1, math.ceil(frame_count / float(max(64, target_points))))
                peaks: List[float] = []
                max_amp = float((1 << (8 * sampwidth - 1)) - 1) if sampwidth > 1 else 255.0
                while True:
                    chunk = wf.readframes(step_frames)
                    if not chunk:
                        break
                    if sampwidth == 1:
                        values = [abs(b - 128) / 127.0 for b in chunk]
                    elif sampwidth == 2:
                        ints = array('h')
                        ints.frombytes(chunk[: len(chunk) - (len(chunk) % 2)])
                        values = [abs(v) / max_amp for v in ints]
                    else:
                        values = []
                        for i in range(0, len(chunk) - 3, 4):
                            sample = int.from_bytes(chunk[i:i + 4], byteorder='little', signed=True)
                            values.append(abs(sample) / max_amp)
                    if channels > 1 and values:
                        merged = []
                        for i in range(0, len(values), channels):
                            merged.append(max(values[i:i + channels]))
                        values = merged
                    peaks.append(max(values) if values else 0.0)
                return peaks if peaks else None
        except Exception:
            return None

    def cut_at(self, time_pos: float) -> bool:
        time_pos = max(0.0, min(self.duration + 120.0, time_pos))
        for i, seg in enumerate(self.segments):
            if seg.start < time_pos < seg.end:
                source_cut = seg.source_start + (time_pos - seg.start)
                self.segments[i:i + 1] = [
                    TrackSegment(seg.start, time_pos, seg.source_start,
                                 source_audio=seg.source_audio,
                                 source_mipmaps=seg.source_mipmaps,
                                 source_sample_rate=seg.source_sample_rate),
                    TrackSegment(time_pos, seg.end, source_cut,
                                 source_audio=seg.source_audio,
                                 source_mipmaps=seg.source_mipmaps,
                                 source_sample_rate=seg.source_sample_rate),
                ]
                return True
        return False

    def delete_selection(self, start: float, end: float) -> bool:
        if end <= start:
            return False
        changed = False
        new_segments: List[TrackSegment] = []
        for seg in self.segments:
            if seg.end <= start or seg.start >= end:
                new_segments.append(seg)
                continue
            changed = True
            overlap_start = max(seg.start, start)
            overlap_end = min(seg.end, end)
            if seg.start < overlap_start:
                new_segments.append(TrackSegment(
                    seg.start, overlap_start, seg.source_start,
                    source_audio=seg.source_audio,
                    source_mipmaps=seg.source_mipmaps,
                    source_sample_rate=seg.source_sample_rate,
                ))
            if seg.end > overlap_end:
                right_source_start = seg.source_start + (overlap_end - seg.start)
                new_segments.append(TrackSegment(
                    overlap_end, seg.end, right_source_start,
                    source_audio=seg.source_audio,
                    source_mipmaps=seg.source_mipmaps,
                    source_sample_rate=seg.source_sample_rate,
                ))
        self.segments = [seg for seg in new_segments if seg.duration > 0.001]
        self.ensure_full_segment()
        self.automation_points = [p for p in self.automation_points if not (start < p.time < end)]
        self._ensure_automation_bounds()
        return changed

    def move_segment(self, seg_index: int, delta: float) -> bool:
        if not (0 <= seg_index < len(self.segments)) or math.isclose(delta, 0.0, abs_tol=1e-6):
            return False
        seg = self.segments[seg_index]
        duration = seg.end - seg.start

        # Clamp delta to avoid overlaps with neighbors and boundaries
        min_start = 0.0
        max_end = self.duration + 120.0
        for i, other in enumerate(self.segments):
            if i == seg_index:
                continue
            if other.end <= seg.start + 0.001:
                # neighbor to the left — can't go further left than its end
                min_start = max(min_start, other.end)
            if other.start >= seg.end - 0.001:
                # neighbor to the right — can't go further right than its start
                max_end = min(max_end, other.start)

        desired_start = seg.start + delta
        clamped_start = max(min_start, min(desired_start, max_end - duration))
        clamped_delta = clamped_start - seg.start

        if math.isclose(clamped_delta, 0.0, abs_tol=1e-6):
            return False

        new_seg = TrackSegment(
            seg.start + clamped_delta, seg.end + clamped_delta, seg.source_start,
            source_audio=seg.source_audio,
            source_mipmaps=seg.source_mipmaps,
            source_sample_rate=seg.source_sample_rate,
        )
        self.segments[seg_index] = new_seg
        self.segments.sort(key=lambda s: s.start)
        return True

    def can_accept_segment(self, seg_start: float, seg_end: float, exclude_seg: Optional['TrackSegment'] = None) -> bool:
        """Check if a segment of given time range can fit on this track without overlaps."""
        seg_dur = seg_end - seg_start
        if seg_dur <= 0:
            return False
        for existing in self.segments:
            if exclude_seg is not None and existing is exclude_seg:
                continue
            if seg_start < existing.end - 0.001 and seg_end > existing.start + 0.001:
                return False
        return True

    def find_nearest_gap(self, seg_start: float, seg_duration: float) -> Optional[float]:
        """Find the nearest position where a segment of given duration can fit.
        Returns the start time, or None if no gap exists."""
        # Try the requested position first
        if self.can_accept_segment(seg_start, seg_start + seg_duration):
            return seg_start

        # Collect all gaps between segments
        sorted_segs = sorted(self.segments, key=lambda s: s.start)
        gaps: List[Tuple[float, float]] = []
        # Gap before first segment
        if sorted_segs:
            if sorted_segs[0].start >= seg_duration:
                gaps.append((0.0, sorted_segs[0].start))
        else:
            return max(0.0, seg_start)  # No segments at all

        # Gaps between segments
        for i in range(len(sorted_segs) - 1):
            gap_start = sorted_segs[i].end
            gap_end = sorted_segs[i + 1].start
            if gap_end - gap_start >= seg_duration - 0.001:
                gaps.append((gap_start, gap_end))

        # Gap after last segment
        gaps.append((sorted_segs[-1].end, self.duration + 120.0))

        # Find closest gap
        best_pos: Optional[float] = None
        best_dist = float('inf')
        for gap_start, gap_end in gaps:
            if gap_end - gap_start < seg_duration - 0.001:
                continue
            # Clamp desired position within this gap
            clamped = max(gap_start, min(seg_start, gap_end - seg_duration))
            dist = abs(clamped - seg_start)
            if dist < best_dist:
                best_dist = dist
                best_pos = clamped
        return best_pos

    def trim_segment(self, seg_index: int, edge: str, new_time: float) -> bool:
        if not (0 <= seg_index < len(self.segments)):
            return False
        seg = self.segments[seg_index]
        min_dur = 0.01
        prev_end = self.segments[seg_index - 1].end if seg_index > 0 else 0.0
        next_start = self.segments[seg_index + 1].start if seg_index < len(self.segments) - 1 else self.duration + 120.0
        if edge == 'left':
            earliest_by_source = seg.start - seg.source_start
            new_start = max(prev_end, earliest_by_source, min(float(new_time), seg.end - min_dur))
            if math.isclose(new_start, seg.start, abs_tol=1e-6):
                return False
            seg.source_start += (new_start - seg.start)
            seg.start = new_start
            self.segments[seg_index] = TrackSegment(
                seg.start, seg.end, seg.source_start,
                source_audio=seg.source_audio,
                source_mipmaps=seg.source_mipmaps,
                source_sample_rate=seg.source_sample_rate,
            )
            return True
        if edge == 'right':
            latest_by_source = seg.start + (self.duration - seg.source_start)
            new_end = min(next_start, latest_by_source, max(float(new_time), seg.start + min_dur))
            if math.isclose(new_end, seg.end, abs_tol=1e-6):
                return False
            self.segments[seg_index] = TrackSegment(
                seg.start, new_end, seg.source_start,
                source_audio=seg.source_audio,
                source_mipmaps=seg.source_mipmaps,
                source_sample_rate=seg.source_sample_rate,
            )
            return True
        return False

    def move_selection(self, start: float, end: float, delta: float) -> bool:
        if end <= start or math.isclose(delta, 0.0, abs_tol=1e-6):
            return False
        selected_segments: List[TrackSegment] = []
        untouched_segments: List[TrackSegment] = []
        changed = False
        for seg in self.segments:
            if seg.end <= start or seg.start >= end:
                untouched_segments.append(seg)
                continue
            overlap_start = max(seg.start, start)
            overlap_end = min(seg.end, end)
            selected_segments.append(TrackSegment(
                overlap_start + delta,
                overlap_end + delta,
                seg.source_start + (overlap_start - seg.start),
                source_audio=seg.source_audio,
                source_mipmaps=seg.source_mipmaps,
                source_sample_rate=seg.source_sample_rate,
            ))
            changed = True
            if seg.start < overlap_start:
                untouched_segments.append(TrackSegment(
                    seg.start, overlap_start, seg.source_start,
                    source_audio=seg.source_audio,
                    source_mipmaps=seg.source_mipmaps,
                    source_sample_rate=seg.source_sample_rate,
                ))
            if seg.end > overlap_end:
                untouched_segments.append(TrackSegment(
                    overlap_end, seg.end,
                    seg.source_start + (overlap_end - seg.start),
                    source_audio=seg.source_audio,
                    source_mipmaps=seg.source_mipmaps,
                    source_sample_rate=seg.source_sample_rate,
                ))
        if not changed or any(seg.start < 0.0 for seg in selected_segments):
            return False
        for moved in selected_segments:
            for seg in untouched_segments:
                if moved.start < seg.end - 0.001 and moved.end > seg.start + 0.001:
                    return False
        self.segments = sorted(untouched_segments + selected_segments, key=lambda s: s.start)
        self.ensure_full_segment()
        new_points: List[AutomationPoint] = []
        for point in self.automation_points:
            if start <= point.time <= end:
                new_points.append(AutomationPoint(point.time + delta, point.value))
            else:
                new_points.append(point)
        self.automation_points = new_points
        self._ensure_automation_bounds()
        return True

    def interpolate_gain(self, start_time: float, frames: int, sample_rate: int) -> np.ndarray:
        if frames <= 0:
            return np.zeros(0, dtype=np.float32)
        if not self.automation_points:
            return np.full(frames, 0.5, dtype=np.float32)
        points = sorted(self.automation_points, key=lambda p: p.time)
        times = np.array([p.time for p in points], dtype=np.float32)
        values = np.array([p.value for p in points], dtype=np.float32)
        sample_times = start_time + np.arange(frames, dtype=np.float32) / float(sample_rate)
        gains = np.interp(sample_times, times, values).astype(np.float32)
        return gains

    def merge_segments(self) -> bool:
        """Авто-слияние смежных по таймлайну и источнику сегментов."""
        if len(self.segments) < 2:
            return False
        self.segments.sort(key=lambda s: s.start)
        merged: List[TrackSegment] = []
        curr = self.segments[0]
        for i in range(1, len(self.segments)):
            nxt = self.segments[i]
            if math.isclose(curr.end, nxt.start, abs_tol=1e-4) and \
               math.isclose(curr.source_start + curr.duration, nxt.source_start, abs_tol=1e-4):
                curr = TrackSegment(
                    curr.start, nxt.end, curr.source_start,
                    source_audio=curr.source_audio,
                    source_mipmaps=curr.source_mipmaps,
                    source_sample_rate=curr.source_sample_rate,
                )
            else:
                merged.append(curr)
                curr = nxt
        merged.append(curr)
        if len(merged) < len(self.segments):
            self.segments = merged
            return True
        return False

    def _ensure_automation_bounds(self) -> None:
        self.automation_points.sort(key=lambda p: p.time)
        if not self.automation_points:
            self.clear_automation()
            return
        if self.automation_points[0].time > 0.0:
            self.automation_points.insert(0, AutomationPoint(0.0, self.automation_points[0].value))
        else:
            self.automation_points[0].time = 0.0
        if self.automation_points[-1].time < self.duration:
            self.automation_points.append(AutomationPoint(self.duration, self.automation_points[-1].value))
        else:
            self.automation_points[-1].time = self.duration


@dataclass
class ProjectModel:
    tracks: List[TrackModel] = field(default_factory=list)
    selected_track: Optional[int] = None
    selected_point: Optional[Tuple[int, int]] = None
    selected_segment: Optional[Tuple[int, int]] = None
    selected_segments: List[Tuple[int, int]] = field(default_factory=list)
    selected_gap: Optional[Tuple[int, int, int]] = None  # (track_index, left_seg_idx, right_seg_idx)
    playhead_time: float = 0.0
    playing: bool = False
    loop_range: Optional[Tuple[float, float]] = None
    play_range_start: float = 0.0
    play_range_end: Optional[float] = None
    project_path: Optional[str] = None
    master_l: float = -60.0
    master_r: float = -60.0
    master_peak_l: float = -60.0
    master_peak_r: float = -60.0

    def add_track(self, track: TrackModel) -> None:
        self.tracks.append(track)
        if self.selected_track is None:
            self.selected_track = 0

    def duration(self) -> float:
        if not self.tracks:
            return 0.0
        return max(track.duration for track in self.tracks)

    def active_track_indexes(self) -> List[int]:
        soloed = [i for i, t in enumerate(self.tracks) if t.solo]
        if soloed:
            return [i for i in soloed if not self.tracks[i].mute]
        return [i for i, t in enumerate(self.tracks) if not t.mute]


class HistoryStack:
    def __init__(self) -> None:
        self.states: List[dict] = []
        self.index = -1

    def clear(self) -> None:
        self.states.clear()
        self.index = -1

    def record(self, state: dict) -> None:
        snapshot = copy.deepcopy(state)
        if self.index >= 0 and self.states[self.index] == snapshot:
            return
        if self.index < len(self.states) - 1:
            self.states = self.states[: self.index + 1]
        self.states.append(snapshot)
        if len(self.states) > 200:
            overflow = len(self.states) - 200
            self.states = self.states[overflow:]
            self.index = max(-1, self.index - overflow)
        self.index = len(self.states) - 1

    def can_undo(self) -> bool:
        return self.index > 0

    def can_redo(self) -> bool:
        return 0 <= self.index < len(self.states) - 1

    def undo(self) -> Optional[dict]:
        if not self.can_undo():
            return None
        self.index -= 1
        return copy.deepcopy(self.states[self.index])

    def redo(self) -> Optional[dict]:
        if not self.can_redo():
            return None
        self.index += 1
        return copy.deepcopy(self.states[self.index])
