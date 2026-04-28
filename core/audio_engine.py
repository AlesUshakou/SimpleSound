from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

TARGET_SAMPLE_RATE = 48000
TARGET_CHANNELS = 2
METER_FLOOR_DB = -60.0


class AudioBackendUnavailableError(RuntimeError):
    pass


try:
    import sounddevice as sd  # type: ignore
except Exception:  # pragma: no cover
    sd = None


@dataclass
class SnapshotTrack:
    index: int
    audio_data: np.ndarray
    duration: float
    segments: List[Tuple[float, float, float, np.ndarray]]
    automation_times: np.ndarray
    automation_values: np.ndarray

    def gains_for_block(self, start_time: float, frames: int, sample_rate: int) -> np.ndarray:
        if frames <= 0:
            return np.zeros(0, dtype=np.float32)
        if self.automation_times.size == 0:
            return np.full(frames, 0.5, dtype=np.float32)
        sample_times = start_time + np.arange(frames, dtype=np.float32) / float(sample_rate)
        return np.interp(sample_times, self.automation_times, self.automation_values).astype(np.float32)


@dataclass
class PlaybackSnapshot:
    playing: bool
    playhead_time: float
    track_levels: Dict[int, dict] = field(default_factory=dict)
    master: dict = field(default_factory=lambda: {
        'level': (METER_FLOOR_DB, METER_FLOOR_DB),
        'peak': (METER_FLOOR_DB, METER_FLOOR_DB),
    })
    finished: bool = False


class PortAudioAudioEngine:
    """Чистый аудио-бэкенд без зависимостей от Qt.

    UI должен опрашивать `snapshot()` и рисовать то, что отдаст этот бэкенд.
    """

    def __init__(self, project: Any, blocksize: int = 128) -> None:
        if sd is None:
            raise AudioBackendUnavailableError(
                'sounddevice is not installed. Install it with: pip install sounddevice'
            )
        self.project = project
        self.blocksize = max(64, int(blocksize))
        self.sample_rate = TARGET_SAMPLE_RATE
        self.channels = TARGET_CHANNELS
        self._stream: Optional[Any] = None
        self._lock = threading.RLock()
        self._render_tracks: List[SnapshotTrack] = []
        self._playing = False
        self._finished_flag = False
        self._loop_enabled = False
        self._start_anchor = 0.0
        self._range_start = 0.0
        self._range_end: Optional[float] = None
        self._range_start_frame = 0
        self._range_end_frame: Optional[int] = None
        self._current_frame = 0
        self._play_start_dac_time: Optional[float] = None
        self._track_levels: Dict[int, dict] = {}
        self._master_levels: dict = {
            'level': (METER_FLOOR_DB, METER_FLOOR_DB),
            'peak': (METER_FLOOR_DB, METER_FLOOR_DB),
        }
        self._last_snapshot_time = 0.0
        self._callback_started = False
        # Pre-open stream for instant playback start
        try:
            self._ensure_stream()
        except Exception:
            pass

    # --------- Public API ---------

    def set_project(self, project: Any) -> None:
        with self._lock:
            self.project = project
            if self._playing:
                self._render_tracks = self._build_render_tracks()

    def refresh_mix(self) -> None:
        with self._lock:
            self._render_tracks = self._build_render_tracks()

    def refresh_render_tracks(self) -> None:
        self.refresh_mix()

    def play(self, start: float, end: Optional[float], loop_enabled: bool) -> None:
        self._ensure_stream()
        with self._lock:
            self._render_tracks = self._build_render_tracks()
            self._start_anchor = max(0.0, float(start))
            self._range_start = self._start_anchor
            self._range_end = None if end is None else max(self._range_start, float(end))
            self._range_start_frame = int(round(self._range_start * self.sample_rate))
            self._range_end_frame = None if self._range_end is None else int(round(self._range_end * self.sample_rate))
            self._current_frame = self._range_start_frame
            self._loop_enabled = bool(loop_enabled and self._range_end is not None and self._range_end > self._range_start)
            self._play_start_dac_time = None
            self._finished_flag = False
            self._callback_started = False
            self._reset_meters_locked()
            self._playing = True
            self._last_snapshot_time = time.perf_counter()

    def stop(self) -> None:
        with self._lock:
            self._playing = False
            self._callback_started = False
            self._play_start_dac_time = None
            self._reset_meters_locked()
        # Stream stays open — callback will output silence when _playing is False

    def close(self) -> None:
        stream = None
        with self._lock:
            stream = self._stream
            self._stream = None
            self._playing = False
            self._callback_started = False
            self._play_start_dac_time = None
            self._reset_meters_locked()
        if stream is not None:
            try:
                stream.abort(ignore_errors=True)
            except Exception:
                try:
                    stream.stop(ignore_errors=True)
                except Exception:
                    pass
            try:
                stream.close(ignore_errors=True)
            except Exception:
                pass

    def snapshot(self) -> PlaybackSnapshot:
        with self._lock:
            playhead = self._current_playhead_locked()
            finished = self._finished_flag
            if finished:
                self._finished_flag = False
            return PlaybackSnapshot(
                playing=self._playing,
                playhead_time=playhead,
                track_levels={k: dict(v) for k, v in self._track_levels.items()},
                master=dict(self._master_levels),
                finished=finished,
            )

    # --------- Internals ---------

    def _ensure_stream(self) -> None:
        existing = None
        with self._lock:
            existing = self._stream
        if existing is not None:
            try:
                if not bool(existing.active):
                    existing.start()
                return
            except Exception:
                with self._lock:
                    if self._stream is existing:
                        self._stream = None
                try:
                    existing.close(ignore_errors=True)
                except Exception:
                    pass
        stream = sd.OutputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype='float32',
            blocksize=self.blocksize,
            latency='low',
            callback=self._callback,
            prime_output_buffers_using_stream_callback=True,
        )
        stream.start()
        with self._lock:
            if self._stream is None:
                self._stream = stream
                return
        # На случай гонки — закрываем лишний поток
        try:
            stream.abort(ignore_errors=True)
        except Exception:
            try:
                stream.stop(ignore_errors=True)
            except Exception:
                pass
        try:
            stream.close(ignore_errors=True)
        except Exception:
            pass

    def _current_playhead_locked(self) -> float:
        if self._play_start_dac_time is not None and self._stream is not None:
            try:
                stream_time = float(self._stream.time)
            except Exception:
                stream_time = 0.0
            elapsed = max(0.0, stream_time - self._play_start_dac_time)
            if self._loop_enabled and self._range_end is not None:
                span = max(0.0, self._range_end - self._range_start)
                if span > 0.0:
                    return self._range_start + (elapsed % span)
            if self._range_end is not None:
                return min(self._range_start + elapsed, self._range_end)
            return self._range_start + elapsed
        fallback = self._current_frame / float(self.sample_rate)
        if self._loop_enabled and self._range_end is not None:
            span = max(0.0, self._range_end - self._range_start)
            if span > 0.0:
                return self._range_start + ((fallback - self._range_start) % span)
        if self._range_end is not None:
            return min(fallback, self._range_end)
        return fallback

    def _build_render_tracks(self) -> List[SnapshotTrack]:
        tracks: List[SnapshotTrack] = []
        active_indexes = (
            set(self.project.active_track_indexes())
            if hasattr(self.project, 'active_track_indexes')
            else set(range(len(self.project.tracks)))
        )
        for index, track in enumerate(getattr(self.project, 'tracks', [])):
            if index not in active_indexes:
                continue
            audio = getattr(track, 'audio_data', None)
            duration = float(getattr(track, 'duration', 0.0))
            raw_segments = getattr(track, 'segments', None) or []

            # Check if any segment has its own cross-track audio
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
                    start = max(0.0, float(getattr(seg, 'start', 0.0)))
                    end = min(duration + 120.0, float(getattr(seg, 'end', duration)))
                    source_start = max(0.0, float(getattr(seg, 'source_start', start)))
                    # Use segment's own audio source if cross-track, otherwise track's
                    seg_audio = getattr(seg, 'source_audio', None)
                    if seg_audio is None:
                        seg_audio = audio
                    if seg_audio is None:
                        continue  # No audio source at all for this segment
                    if not (isinstance(seg_audio, np.ndarray) and seg_audio.dtype == np.float32 and seg_audio.flags.c_contiguous):
                        seg_audio = np.ascontiguousarray(np.asarray(seg_audio, dtype=np.float32))
                    if end > start:
                        segments.append((start, end, source_start, seg_audio))
            points = sorted(getattr(track, 'automation_points', []), key=lambda p: getattr(p, 'time', 0.0))
            if not points:
                automation_times = np.array([0.0, duration], dtype=np.float32)
                automation_values = np.array([0.5, 0.5], dtype=np.float32)
            else:
                automation_times = np.array([float(getattr(p, 'time', 0.0)) for p in points], dtype=np.float32)
                automation_values = np.array([float(getattr(p, 'value', 0.5)) for p in points], dtype=np.float32)
                if automation_times[0] > 0.0:
                    automation_times = np.insert(automation_times, 0, 0.0)
                    automation_values = np.insert(automation_values, 0, automation_values[0])
                if duration > 0.0 and automation_times[-1] < duration:
                    automation_times = np.append(automation_times, duration)
                    automation_values = np.append(automation_values, automation_values[-1])
            if not segments:
                continue  # Skip tracks with no playable segments
            # audio_data for the track (may be None for empty tracks with cross-track segments)
            track_audio = (
                audio if isinstance(audio, np.ndarray) and audio.dtype == np.float32 and audio.flags.c_contiguous
                else (np.ascontiguousarray(np.asarray(audio, dtype=np.float32)) if audio is not None
                      else np.zeros((0, self.channels), dtype=np.float32))
            )
            tracks.append(SnapshotTrack(
                index=index,
                audio_data=track_audio,
                duration=duration,
                segments=segments,
                automation_times=automation_times,
                automation_values=automation_values,
            ))
        return tracks

    def _reset_meters_locked(self) -> None:
        self._track_levels = {}
        self._master_levels = {
            'level': (METER_FLOOR_DB, METER_FLOOR_DB),
            'peak': (METER_FLOOR_DB, METER_FLOOR_DB),
        }

    def _callback(self, outdata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
        del status
        with self._lock:
            out = np.zeros((frames, self.channels), dtype=np.float32)
            if not self._playing:
                outdata[:] = out
                return
            if self._play_start_dac_time is None:
                try:
                    self._play_start_dac_time = float(time_info.outputBufferDacTime)
                except Exception:
                    self._play_start_dac_time = 0.0
            written = 0
            track_levels: Dict[int, dict] = {}
            master_peak = np.zeros(2, dtype=np.float32)
            master_rms = np.zeros(2, dtype=np.float32)
            finished_now = False
            while written < frames:
                if self._range_end_frame is not None and self._current_frame >= self._range_end_frame:
                    if self._loop_enabled:
                        self._current_frame = self._range_start_frame
                    else:
                        self._playing = False
                        self._play_start_dac_time = None
                        self._finished_flag = True
                        finished_now = True
                        break
                take = frames - written
                if self._range_end_frame is not None:
                    take = min(take, max(0, self._range_end_frame - self._current_frame))
                if take <= 0:
                    break
                block_start_sec = self._current_frame / float(self.sample_rate)
                chunk = np.zeros((take, self.channels), dtype=np.float32)
                block_track_levels: Dict[int, dict] = {}
                for track in self._render_tracks:
                    track_chunk = self._mix_track(track, block_start_sec, take)
                    if track_chunk.size:
                        chunk += track_chunk
                        peak = np.max(np.abs(track_chunk), axis=0)
                        rms = np.sqrt(np.mean(np.square(track_chunk), axis=0))
                        block_track_levels[track.index] = {
                            'peak': (float(peak[0]), float(peak[1])),
                            'rms': (float(rms[0]), float(rms[1])),
                        }
                chunk = np.clip(chunk, -1.0, 1.0)
                out[written:written + take] = chunk
                written += take
                self._current_frame += take
                for idx, level in block_track_levels.items():
                    track_levels[idx] = level
            if written < frames:
                out[written:] = 0.0
            if finished_now:
                self._reset_meters_locked()
                outdata[:] = out
                return
            if out.size:
                master_peak = np.max(np.abs(out), axis=0)
                master_rms = np.sqrt(np.mean(np.square(out), axis=0))
            self._track_levels = self._compose_display_levels(track_levels)
            self._master_levels = self._compose_master_levels(master_peak, master_rms)
            outdata[:] = out

    def _mix_track(self, track: SnapshotTrack, start_time: float, frames: int) -> np.ndarray:
        out = np.zeros((frames, self.channels), dtype=np.float32)
        if not track.segments:
            return out

        gains = track.gains_for_block(start_time, frames, self.sample_rate).reshape(-1, 1)
        block_end_time = start_time + frames / float(self.sample_rate)

        for seg in track.segments:
            seg_start, seg_end, source_start, seg_audio = seg

            if seg_audio is None or seg_audio.size == 0:
                continue

            overlap_start = max(start_time, seg_start)
            overlap_end = min(block_end_time, seg_end)
            if overlap_end <= overlap_start:
                continue

            dst_start = int(round((overlap_start - start_time) * self.sample_rate))
            take_frames = int(round((overlap_end - overlap_start) * self.sample_rate))
            take_frames = min(take_frames, frames - dst_start)

            src_anchor = int(round(source_start * self.sample_rate))
            src_start = src_anchor + int(round((overlap_start - seg_start) * self.sample_rate))

            if src_start < 0 or src_start >= seg_audio.shape[0]:
                continue
            take_frames = min(take_frames, seg_audio.shape[0] - src_start)
            if take_frames <= 0:
                continue

            src_slice = seg_audio[src_start: src_start + take_frames]
            gain_slice = gains[dst_start: dst_start + take_frames]
            out[dst_start: dst_start + take_frames] += src_slice * gain_slice

        return out

    def _compose_display_levels(self, raw_levels: Dict[int, dict]) -> Dict[int, dict]:
        display: Dict[int, dict] = {}
        for idx, level in raw_levels.items():
            peak_l, peak_r = level.get('peak', (0.0, 0.0))
            rms_l, rms_r = level.get('rms', (0.0, 0.0))
            display[idx] = {
                'level': (self._compose_meter_db(peak_l, rms_l), self._compose_meter_db(peak_r, rms_r)),
                'peak': (self._linear_to_db(peak_l), self._linear_to_db(peak_r)),
            }
        return display

    def _compose_master_levels(self, peak: np.ndarray, rms: np.ndarray) -> dict:
        peak_l = float(peak[0]) if len(peak) > 0 else 0.0
        peak_r = float(peak[1]) if len(peak) > 1 else peak_l
        rms_l = float(rms[0]) if len(rms) > 0 else 0.0
        rms_r = float(rms[1]) if len(rms) > 1 else rms_l
        return {
            'level': (self._compose_meter_db(peak_l, rms_l), self._compose_meter_db(peak_r, rms_r)),
            'peak': (self._linear_to_db(peak_l), self._linear_to_db(peak_r)),
        }

    @staticmethod
    def _linear_to_db(value: float) -> float:
        value = float(value)
        if value <= 1e-6:
            return METER_FLOOR_DB
        db = 20.0 * np.log10(value)
        return float(max(METER_FLOOR_DB, min(6.0, db)))

    def _compose_meter_db(self, peak_linear: float, rms_linear: float) -> float:
        peak_db = self._linear_to_db(peak_linear)
        rms_db = self._linear_to_db(rms_linear)
        return max(peak_db - 1.0, rms_db + 3.0)
