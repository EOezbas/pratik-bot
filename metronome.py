"""Detects an audible metronome in a practice recording.

A metronome produces short broadband clicks at a strictly constant interval.
Human playing drifts by tens of milliseconds, so a long run of onsets that fit
a fixed grid with very low timing error is taken as a metronome.
"""
import subprocess

import numpy as np
from scipy.signal import butter, find_peaks, sosfiltfilt

SR = 16000
HOP = 32  # 2 ms
WIN = 128
MIN_BPM, MAX_BPM = 40, 240
TOL = 0.012  # max distance of an onset from the grid, seconds
MAX_RESID_MS = 2.8
MIN_RUN = 12  # consecutive matched beats
MIN_HITS = 12
MIN_HIT_RATE = 0.45  # share of grid beats with a click, when not consecutive
FINAL_TOL = 0.005  # tolerance on the refined grid
MIN_FLUX = 0.4  # log-energy jump per 2 ms frame


def decode(data: bytes) -> np.ndarray:
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", "pipe:0", "-ac", "1", "-ar", str(SR), "-f", "f32le", "pipe:1"],
        input=data, capture_output=True, timeout=60, check=True)
    return np.frombuffer(proc.stdout, dtype=np.float32)


def onset_envelope(x: np.ndarray) -> np.ndarray:
    sos = butter(4, 2000, btype="highpass", fs=SR, output="sos")
    y = sosfiltfilt(sos, x)
    n = 1 + max(0, len(y) - WIN) // HOP
    idx = np.arange(WIN)[None, :] + HOP * np.arange(n)[:, None]
    energy = np.sqrt(np.mean(y[idx] ** 2, axis=1)) + 1e-6
    flux = np.diff(np.log(energy), prepend=np.log(energy[0]))
    return np.maximum(flux, 0)


def pick_onsets(env: np.ndarray) -> np.ndarray:
    med = np.median(env)
    mad = np.median(np.abs(env - med)) + 1e-9
    peaks, _ = find_peaks(env, height=max(med + 4 * mad, MIN_FLUX), distance=int(0.01 * SR / HOP))
    return peaks * HOP / SR


def candidate_periods(env: np.ndarray) -> list:
    e = env - env.mean()
    ac = np.correlate(e, e, mode="full")[len(e) - 1:]
    lo = int(60 / MAX_BPM * SR / HOP)
    hi = min(int(60 / MIN_BPM * SR / HOP), len(ac) - 1)
    if hi <= lo:
        return []
    seg = ac[lo:hi]
    peaks, _ = find_peaks(seg)
    best = peaks[np.argsort(seg[peaks])[::-1][:4]] if len(peaks) else []
    out = []
    for p in best:
        base = (p + lo) * HOP / SR
        for div in (1, 2, 3, 4):
            cand = base / div
            if 60 / MAX_BPM <= cand <= 60 / MIN_BPM:
                out.append(cand)
    return out


def match(onsets: np.ndarray, expected: np.ndarray):
    pos = np.clip(np.searchsorted(onsets, expected), 1, len(onsets) - 1)
    left, right = onsets[pos - 1], onsets[pos]
    nearest = np.where(np.abs(left - expected) < np.abs(right - expected), left, right)
    return nearest, np.abs(nearest - expected)


def longest_run(hit: np.ndarray) -> int:
    run = best = 0
    for h in hit:
        run = run + 1 if h else 0
        best = max(best, run)
    return best


def fit_grid(onsets: np.ndarray, period: float, anchor: float):
    span_lo, span_hi = onsets[0], onsets[-1]
    for _ in range(3):
        ks = np.arange(int(np.floor((span_lo - anchor) / period)) - 1,
                       int(np.ceil((span_hi - anchor) / period)) + 2)
        nearest, dist = match(onsets, anchor + ks * period)
        hit = dist <= TOL
        if hit.sum() < 4:
            return None
        period, anchor = np.polyfit(ks[hit], nearest[hit], 1)

    expected = anchor + ks * period
    nearest, dist = match(onsets, expected)
    hit = dist <= FINAL_TOL
    resid = nearest[hit] - expected[hit]
    # Same test half a beat off the grid: random or dense onsets hit both equally
    _, off_dist = match(onsets, expected + period / 2)
    off_rate = float(np.mean(off_dist <= FINAL_TOL))
    on_rate = float(np.mean(hit))
    resid_ms = float(np.std(resid) * 1000) if hit.any() else 99.0
    return period, longest_run(hit), int(hit.sum()), resid_ms, on_rate, off_rate, ks, hit


def detect(data: bytes):
    """Returns (has_metronome, bpm or None)."""
    x = decode(data)
    if len(x) < SR * 5:
        return False, None
    env = onset_envelope(x)
    onsets = pick_onsets(env)
    if len(onsets) < MIN_RUN:
        return False, None

    # Clicks are often weaker than notes, so every onset can anchor the grid
    anchors = onsets if len(onsets) <= 400 else onsets[np.linspace(0, len(onsets) - 1, 400).astype(int)]
    best = None
    for period in candidate_periods(env):
        for anchor in anchors:
            res = fit_grid(onsets, period, anchor)
            if not res:
                continue
            p, run, hits, resid_ms, on_rate, off_rate, ks, hit = res
            if resid_ms > MAX_RESID_MS or off_rate >= 0.5 * on_rate or hits < MIN_HITS:
                continue
            if run >= MIN_RUN or on_rate >= MIN_HIT_RATE:
                score = (hits, on_rate, -resid_ms)
                if best is None or score > best[0]:
                    best = (score, p, ks, hit)
    if best is None:
        return False, None
    return True, int(round(60 / true_period(*best[1:])))


def true_period(period: float, ks: np.ndarray, hit: np.ndarray) -> float:
    # A grid at a subdivision of the click rate hits mostly on one parity
    for _ in range(2):
        even = hit[ks % 2 == 0].mean() if (ks % 2 == 0).any() else 0
        odd = hit[ks % 2 == 1].mean() if (ks % 2 == 1).any() else 0
        lo_, hi_ = sorted((even, odd))
        if hi_ == 0 or lo_ >= 0.5 * hi_ or period * 2 > 60 / MIN_BPM:
            break
        keep = (ks % 2 == 0) if even >= odd else (ks % 2 == 1)
        ks, hit, period = ks[keep] // 2, hit[keep], period * 2
    return period
