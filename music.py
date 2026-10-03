"""Tells whether a recording contains music (playing or singing) or only talk.

Uses YAMNet, a small audio event classifier trained on AudioSet, which scores
each ~1 s frame for 521 sound classes. A take counts as music as soon as a few
frames score high on any music, instrument or singing class.
"""
import logging
import os
import threading

import numpy as np

import metronome

log = logging.getLogger("pratik")

MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "yamnet.tflite")
FRAME = 15600  # samples at 16 kHz, the model's fixed input
HOP = 7800
# AudioSet indices: singing, choir, chant, rapping, humming (24-32) and all music classes (132-276)
MUSIC_CLASSES = np.r_[24:33, 132:277]
MUSIC_SCORE = 0.3
MIN_MUSIC_FRAMES = 3  # about 2 seconds
MIN_SECONDS = 2.0  # shorter takes are not judged

_lock = threading.Lock()
_interp = None


def _model():
    global _interp
    if _interp is None:
        from ai_edge_litert.interpreter import Interpreter
        it = Interpreter(model_path=MODEL_PATH, num_threads=2)
        it.allocate_tensors()
        _interp = it
    return _interp


def music_frames(x: np.ndarray, stop_at=None) -> int:
    with _lock:
        it = _model()
        inp = it.get_input_details()[0]["index"]
        out = it.get_output_details()[0]["index"]
        hits = 0
        frame = np.zeros(FRAME, np.float32)
        for start in range(0, max(1, len(x) - FRAME // 2), HOP):
            seg = x[start:start + FRAME]
            frame[:] = 0
            frame[:len(seg)] = seg
            it.set_tensor(inp, frame)
            it.invoke()
            if it.get_tensor(out)[0][MUSIC_CLASSES].max() >= MUSIC_SCORE:
                hits += 1
                if stop_at and hits >= stop_at:
                    break
        return hits


def has_music(data: bytes) -> bool:
    """False only when the take is clearly talk or noise; any doubt counts as music."""
    try:
        x = metronome.decode(data)
        if len(x) < metronome.SR * MIN_SECONDS:
            return True
        return music_frames(x, stop_at=MIN_MUSIC_FRAMES) >= MIN_MUSIC_FRAMES
    except Exception:
        log.exception("music detection failed")
        return True
