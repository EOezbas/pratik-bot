"""Detects an audible metronome in a practice recording.

A digital metronome produces short broadband clicks at a strictly constant interval.
Human playing drifts by tens of milliseconds, so a long run of onsets that fit
a fixed grid with very low timing error is taken as a metronome.

Mechanical metronomes and clicks masked by playing do not fit a fixed grid, so a
second, looser check follows bright onsets beat by beat and accepts a long chain
whose beat-to-beat timing is steadier than typical human playing.
"""
import subprocess
import tempfile

import numpy as np
from scipy.signal import butter, find_peaks, sosfiltfilt, stft

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
STEADY_TOL = 0.04  # how far a beat may land from the predicted time, seconds
STEADY_MIN_BEATS = 20
STEADY_MIN_COVER = 0.9  # share of beats in the chain that have a click
STEADY_MAX_JITTER_MS = 10.0
STEADY_MAX_ANCHORS = 60
STEADY_MIN_SPAN = 0.6  # a metronome runs through most of the take, not just a passage
CLEAN_RUN = 8  # consecutive clicks heard clearly, e.g. where the playing pauses
CLEAN_MAX_RESID_MS = 1.5  # far tighter than any human can hold over that many beats
MIN_FLUX = 0.4  # log-energy jump per 2 ms frame


def decode(data: bytes) -> np.ndarray:
    # A temp file instead of a pipe: phone videos often keep their index at the end of the file
    with tempfile.NamedTemporaryFile() as f:
        f.write(data)
        f.flush()
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", f.name, "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "pipe:1"],
            capture_output=True, timeout=120, check=True)
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
    # Same test at an offset no musical subdivision lands on: dense or random onsets
    # hit it as often as the grid, a metronome (even one clicking subdivisions) does not
    _, off_dist = match(onsets, expected + period * 0.37)
    off_rate = float(np.mean(off_dist <= FINAL_TOL))
    on_rate = float(np.mean(hit))
    resid_ms = float(np.std(resid) * 1000) if hit.any() else 99.0
    return period, longest_run(hit), int(hit.sum()), resid_ms, on_rate, off_rate, ks, hit


def detect(data: bytes):
    """Returns (has_metronome, bpm or None, duration in seconds)."""
    x = decode(data)
    duration = int(round(len(x) / SR))
    if len(x) < SR * 5:
        return False, None, duration
    env = onset_envelope(x)
    onsets = pick_onsets(env)
    if len(onsets) < MIN_RUN:
        return False, None, duration

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
        bpm = detect_clean_run(onsets, env) or detect_steady(x, env)
        return (True, bpm, duration) if bpm else (False, None, duration)
    return True, int(round(60 / true_period(*best[1:]))), duration


def detect_clean_run(onsets: np.ndarray, env: np.ndarray):
    """Beat tempo when a stretch of clicks is machine-exact, though playing masks the rest."""
    anchors = onsets if len(onsets) <= 400 else onsets[np.linspace(0, len(onsets) - 1, 400).astype(int)]
    best = None
    for period in candidate_periods(env):
        for anchor in anchors:
            # Walk forward from the anchor while every beat has an onset close to the grid
            ts = [anchor]
            while True:
                pred = ts[-1] + period
                near, dist = match(onsets, np.array([pred]))
                if dist[0] > FINAL_TOL:
                    break
                ts.append(float(near[0]))
                if len(ts) >= 2 * CLEAN_RUN:
                    break
            if len(ts) < CLEAN_RUN:
                continue
            ts = np.array(ts)
            k = np.arange(len(ts))
            beat, start = np.polyfit(k, ts, 1)
            resid_ms = float(np.std(ts - (start + k * beat)) * 1000)
            if resid_ms <= CLEAN_MAX_RESID_MS and (best is None or (len(ts), -resid_ms) > best[0]):
                best = ((len(ts), -resid_ms), beat)
    if best is None:
        return None
    beat = best[1]
    while beat < 60 / MAX_BPM * 1.5 and beat * 2 <= 60 / MIN_BPM:
        beat *= 2
    return int(round(60 / beat))


def bright_onsets(x: np.ndarray) -> np.ndarray:
    sos = butter(4, 4000, btype="highpass", fs=SR, output="sos")
    y = sosfiltfilt(sos, x)
    n = 1 + max(0, len(y) - WIN) // HOP
    idx = np.arange(WIN)[None, :] + HOP * np.arange(n)[:, None]
    energy = np.sqrt(np.mean(y[idx] ** 2, axis=1))
    # A floor tied to the loudest clicks keeps faint high-band wiggles from counting
    energy += 0.02 * np.percentile(energy, 99) + 1e-6
    flux = np.maximum(np.diff(np.log(energy), prepend=np.log(energy[0])), 0)
    med = np.median(flux)
    mad = np.median(np.abs(flux - med)) + 1e-9
    peaks, _ = find_peaks(flux, height=max(med + 6 * mad, 1.5 * MIN_FLUX), distance=int(0.1 * SR / HOP))
    return peaks * HOP / SR


def track_beats(onsets: np.ndarray, start: float, period: float):
    """Follows beats from `start`, letting the period drift slowly; stops after two misses."""
    ks, ts = [0], [start]
    k = misses = 0
    while True:
        k += 1
        pred = ts[-1] + period * (k - ks[-1])
        if pred > onsets[-1] + STEADY_TOL:
            break
        j = np.searchsorted(onsets, pred)
        near = min((onsets[i] for i in (j - 1, j) if 0 <= i < len(onsets)), key=lambda o: abs(o - pred))
        if abs(near - pred) <= STEADY_TOL:
            period = 0.8 * period + 0.2 * (near - ts[-1]) / (k - ks[-1])
            ks.append(k)
            ts.append(near)
            misses = 0
        else:
            misses += 1
            if misses > 1:
                break
    return np.array(ks), np.array(ts)


def detect_steady(x: np.ndarray, env: np.ndarray):
    """Beat tempo when bright clicks keep a steadier pulse than human playing, else None."""
    onsets = bright_onsets(x)
    if len(onsets) < STEADY_MIN_BEATS:
        return None
    anchors = onsets[np.linspace(0, len(onsets) - 1, min(len(onsets), STEADY_MAX_ANCHORS)).astype(int)]
    # Playing often pulses faster than the click, so also try multiples of each period
    periods = sorted({round(p * f, 4) for p in candidate_periods(env)[:8] for f in (1, 2, 3, 4)
                      if 60 / MAX_BPM <= p * f <= 60 / MIN_BPM})
    best = None
    for period in periods:
        for anchor in anchors:
            ks, ts = track_beats(onsets, anchor, period)
            if len(ks) < STEADY_MIN_BEATS or len(ks) / (ks[-1] + 1) < STEADY_MIN_COVER:
                continue
            if ts[-1] - ts[0] < STEADY_MIN_SPAN * len(x) / SR:
                continue
            # Deviation of each beat from the midpoint of its two neighbours
            inner = np.where((np.diff(ks)[:-1] == 1) & (np.diff(ks)[1:] == 1))[0] + 1
            if len(inner) < 10:
                continue
            jitter_ms = float(np.std(ts[inner] - (ts[inner - 1] + ts[inner + 1]) / 2) * 1000)
            if jitter_ms > STEADY_MAX_JITTER_MS:
                continue
            beat = float(np.median(np.diff(ts) / np.diff(ks)))
            if best is None or (len(ks), -jitter_ms) > best[0]:
                best = ((len(ks), -jitter_ms), beat)
    return int(round(60 / best[1])) if best else None


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


# ---------- tempo of the playing itself, for takes without a metronome ----------

N_FFT, T_HOP = 1024, 160  # 10 ms frames
FPS = SR / T_HOP
T_MIN_BPM, T_MAX_BPM = 40, 220
PRIOR_BPM, PRIOR_OCT = 100.0, 1.0
# The pulse found is often the note rate, twice the beat; these decide when to halve it
HALF_MIN_BPM = 55  # never halve below this
HALF_ACCENT_RATIO = 1.4  # every other pulse this much stronger means it is the beat
HALF_FAST_BPM = 125  # at or above this, a nearly as strong half tempo wins
HALF_SCORE_RATIO = 0.7


def _onset_strength(x):
    _, _, z = stft(x, fs=SR, nperseg=N_FFT, noverlap=N_FFT - T_HOP, boundary=None, padded=False)
    mag = np.log1p(1000 * np.abs(z))
    flux = np.maximum(np.diff(mag, axis=1), 0).sum(axis=0)
    flux -= np.convolve(flux, np.ones(31) / 31, mode="same")  # remove slow loudness changes
    return np.maximum(flux, 0)


def _autocorr(env):
    e = env - env.mean()
    n = len(e)
    f = np.fft.rfft(e, 2 * n)
    ac = np.fft.irfft(f * np.conj(f))[:n]
    return ac / (ac[0] + 1e-12)


def _best_tempo(env):
    ac = _autocorr(env)
    lo, hi = int(FPS * 60 / T_MAX_BPM), min(int(FPS * 60 / T_MIN_BPM), len(ac) - 2)
    if hi <= lo:
        return None, 0.0
    lags = np.arange(lo, hi + 1)
    bpm = 60 * FPS / lags
    prior = np.exp(-0.5 * (np.log2(bpm / PRIOR_BPM) / PRIOR_OCT) ** 2)
    # Comb: a real beat period also lines up at its multiples
    seg = np.zeros(len(lags))
    for m, w in ((1, 1.0), (2, 0.5), (3, 0.33), (4, 0.25)):
        idx = lags * m
        ok = idx < len(ac)
        seg[ok] += w * ac[idx[ok]]
    seg /= 2.08
    peaks, _ = find_peaks(seg)
    if not len(peaks):
        return None, 0.0
    i = peaks[np.argmax(seg[peaks] * prior[peaks])]
    # A peak at 2/3 or 3/2 of this period almost as strong means the meter is ambiguous
    for ratio in (1.5, 2 / 3):
        j = int(round(lags[i] * ratio)) - lo
        if 0 <= j < len(seg) and seg[max(0, j - 1):j + 2].max() >= 0.97 * seg[i]:
            return None, 0.0
    # Parabolic interpolation for a sub-frame lag
    if 0 < i < len(seg) - 1:
        a, b, c = seg[i - 1], seg[i], seg[i + 1]
        off = 0.5 * (a - c) / (a - 2 * b + c) if (a - 2 * b + c) != 0 else 0
    else:
        off = 0
    lag = lags[i] + off
    return 60 * FPS / lag, float(seg[i])


def estimate_tempo(x):
    """Estimated beat tempo of the playing, or None when it is not steady enough to tell."""
    if len(x) < SR * 15:
        return None
    env = _onset_strength(x)
    bpm, strength = _best_tempo(env)
    if bpm is None:
        return None
    # The same tempo must hold in each part of the recording
    n = len(env)
    win = min(n, int(FPS * 16))
    parts = []
    starts = np.linspace(0, n - win, max(2, min(5, n // win + 1))).astype(int)
    for s in starts:
        parts.append(_best_tempo(env[s:s + win])[0])
    # Parts may lock onto half or double the tempo; that still supports it
    agree = [p for p in parts if p and abs(np.log2(p / bpm) - round(np.log2(p / bpm))) < 0.04]
    ok = strength >= 0.12 and len(agree) >= max(2, int(0.75 * len(parts)))
    if not ok:
        return None
    return int(round(beat_tempo(env, bpm)))


def _comb_score(env, bpm):
    ac = _autocorr(env)
    lag = 60 * FPS / bpm
    score = 0.0
    for m, w in ((1, 1.0), (2, 0.5), (3, 0.33), (4, 0.25)):
        i = int(round(lag * m))
        if i + 1 < len(ac):
            score += w * ac[max(0, i - 1):i + 2].max()
    return score / 2.08


def _accent_ratio(env, bpm):
    """How much stronger every other pulse is; well above 1 when the pulse is half a beat."""
    period = 60 * FPS / bpm
    best = None
    for phase in np.linspace(0, period, 24, endpoint=False):
        idx = np.round(np.arange(phase, len(env) - 2, period)).astype(int)
        val = np.array([env[max(0, i - 2):i + 3].max() for i in idx])
        if best is None or val.sum() > best.sum():
            best = val
    if best is None or len(best) < 8:
        return 1.0
    even, odd = best[0::2].mean(), best[1::2].mean()
    return max(even, odd) / (min(even, odd) + 1e-9)


def beat_tempo(env, bpm):
    """Halves a pulse tempo that is really the note rate: by accents first, then by preferring the slower beat."""
    half = bpm / 2
    if half < HALF_MIN_BPM:
        return bpm
    if _accent_ratio(env, bpm) >= HALF_ACCENT_RATIO:
        return half
    if bpm >= HALF_FAST_BPM and _comb_score(env, half) >= HALF_SCORE_RATIO * _comb_score(env, bpm):
        return half
    return bpm


def analyze(data: bytes):
    """Returns (has_metronome, metronome bpm, duration in seconds, estimated playing tempo)."""
    has, bpm, duration = detect(data)
    if has:
        return has, bpm, duration, None
    try:
        tempo = estimate_tempo(decode(data))
    except Exception:
        tempo = None
    return has, bpm, duration, tempo
