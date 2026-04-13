from __future__ import annotations

import math
from typing import Dict, Optional, Tuple, TYPE_CHECKING

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap

if TYPE_CHECKING:
    from .models import TrackModel


class WaveformRenderCache:
    """Кеширует pre-rendered тайлы waveform для конкретной комбинации
    (track_id, revision, zoom, height, tile_index, mode)."""

    TILE_WIDTH = 1024
    LEFT_PADDING = 14  # должно совпадать с TimelineCanvas.LEFT_PADDING

    def __init__(self) -> None:
        self._cache: Dict[Tuple[int, int, int, int, int, str], QPixmap] = {}

    def clear(self) -> None:
        self._cache.clear()

    def invalidate_track(self, track: "TrackModel") -> None:
        if track is None:
            return
        prefix = (track.track_id,)
        self._cache = {k: v for k, v in self._cache.items() if k[:1] != prefix}

    def render_tile(
        self,
        track: "TrackModel",
        px_per_second: float,
        lane_height: int,
        tile_index: int,
        color: QColor,
        mode: str = 'abs',
    ) -> Optional[QPixmap]:
        if track.audio_data is None or track.audio_data.size == 0 or track.duration <= 0.0:
            return None
        width = self.TILE_WIDTH
        height = max(1, int(lane_height))
        zoom_key = max(1, int(round(px_per_second * 100.0)))
        cache_key = (
            track.track_id,
            getattr(track, 'waveform_cache_revision', 0),
            zoom_key,
            height,
            tile_index,
            mode,
        )
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        samples = track.audio_data
        if samples.ndim == 2:
            mono = np.mean(samples, axis=1, dtype=np.float32)
        else:
            mono = samples.astype(np.float32, copy=False)
        total_samples = len(mono)
        if total_samples <= 0:
            return None

        start_x = tile_index * width
        end_x = start_x + width
        track_left_x = float(self.LEFT_PADDING)
        track_right_x = float(self.LEFT_PADDING) + track.duration * px_per_second
        visible_start_x = max(float(start_x), track_left_x)
        visible_end_x = min(float(end_x), track_right_x)
        visible_width = int(math.ceil(visible_end_x - visible_start_x))
        visible_offset = int(round(visible_start_x - float(start_x)))
        if visible_width <= 0:
            return None

        start_time = max(0.0, (visible_start_x - self.LEFT_PADDING) / px_per_second)
        end_time = min(track.duration, max(start_time, (visible_end_x - self.LEFT_PADDING) / px_per_second))
        if end_time <= start_time:
            return None

        start_sample = int(start_time * track.sample_rate)
        end_sample = min(total_samples, int(math.ceil(end_time * track.sample_rate)) + 1)
        if end_sample <= start_sample:
            return None

        tile = QPixmap(width, height)
        tile.fill(Qt.transparent)
        painter = QPainter(tile)
        try:
            painter.setRenderHint(QPainter.Antialiasing, False)
            painter.setPen(QPen(color, 1))
            center_y = height * 0.5
            amplitude = max(1.0, height * 0.42)
            bucket_count = max(1, visible_width)
            edges = np.linspace(start_sample, end_sample, bucket_count + 1, dtype=np.int64)
            if mode == 'signed':
                mins = np.zeros(bucket_count, dtype=np.float32)
                maxs = np.zeros(bucket_count, dtype=np.float32)
                for i in range(bucket_count):
                    a = int(edges[i])
                    b = int(edges[i + 1])
                    if b <= a:
                        continue
                    chunk = mono[a:b]
                    mins[i] = float(np.min(chunk))
                    maxs[i] = float(np.max(chunk))
                for x in range(bucket_count):
                    draw_x = visible_offset + x
                    y1 = center_y - maxs[x] * amplitude
                    y2 = center_y - mins[x] * amplitude
                    painter.drawLine(draw_x, int(round(y1)), draw_x, int(round(y2)))
            else:
                peaks = np.zeros(bucket_count, dtype=np.float32)
                abs_mono = np.abs(mono[start_sample:end_sample])
                local_edges = np.linspace(0, len(abs_mono), bucket_count + 1, dtype=np.int64)
                for i in range(bucket_count):
                    a = int(local_edges[i])
                    b = int(local_edges[i + 1])
                    if b <= a:
                        continue
                    peaks[i] = float(np.max(abs_mono[a:b]))
                for x in range(bucket_count):
                    draw_x = visible_offset + x
                    h = max(1.0, float(peaks[x]) * amplitude)
                    painter.drawLine(draw_x, int(round(center_y - h)), draw_x, int(round(center_y + h)))
        finally:
            painter.end()

        self._cache[cache_key] = tile
        return tile
