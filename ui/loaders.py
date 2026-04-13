from __future__ import annotations

import os
from typing import List

import numpy as np
from PySide6.QtCore import QObject, Signal
from pydub import AudioSegment

from core.models import (
    TARGET_CHANNELS,
    TARGET_SAMPLE_RATE,
    TARGET_SAMPLE_WIDTH,
    TrackModel,
    TrackSegment,
)


class AudioFileLoader(QObject):
    progress = Signal(int, int, str)
    finished = Signal(list)
    failed = Signal(str)

    def __init__(self, paths: List[str]):
        super().__init__()
        self.paths = paths
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        loaded_tracks: List[TrackModel] = []
        try:
            total = len(self.paths)
            for index, path in enumerate(self.paths, start=1):
                if self._cancel:
                    break
                self.progress.emit(index - 1, total, os.path.basename(path))
                segment = AudioSegment.from_file(path)
                segment = (
                    segment.set_frame_rate(TARGET_SAMPLE_RATE)
                    .set_channels(TARGET_CHANNELS)
                    .set_sample_width(TARGET_SAMPLE_WIDTH)
                )
                raw = np.array(segment.get_array_of_samples(), dtype=np.int16)
                audio = raw.reshape((-1, TARGET_CHANNELS)).astype(np.float32) / 32768.0
                name = os.path.splitext(os.path.basename(path))[0]
                track = TrackModel(track_id=index, name=name, file_path=path, duration=0.0)
                track.set_audio_data(audio, TARGET_SAMPLE_RATE)
                track.segments = [TrackSegment(0.0, track.duration, 0.0)]
                loaded_tracks.append(track)
                self.progress.emit(index, total, os.path.basename(path))
            self.finished.emit(loaded_tracks)
        except Exception as exc:
            self.failed.emit(str(exc))
