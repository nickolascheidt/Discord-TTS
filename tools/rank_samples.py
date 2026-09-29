"""Scores WAV files and ranks the best candidates for a voice sample.

    python tools/rank_samples.py FOLDER
    python tools/rank_samples.py FOLDER --copy    # copies the top 8 to FOLDER/candidates

It doesn't decide anything for you — it sorts the queue so you listen to the
best 8 instead of all 300.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import numpy as np
from scipy.io import wavfile

IDEAL_DURATION = (12.0, 25.0)
WINDOW = 0.025  # 25 ms


def load(path: Path) -> tuple[np.ndarray, int]:
    sr, data = wavfile.read(path)
    x = data.astype(np.float32)
    if x.ndim > 1:
        x = x.mean(axis=1)
    if np.issubdtype(data.dtype, np.integer):
        x /= np.iinfo(data.dtype).max
    return x, sr


def analyze(x: np.ndarray, sr: int) -> dict:
    dur = len(x) / sr
    n = max(1, int(WINDOW * sr))
    frames = x[: len(x) // n * n].reshape(-1, n)
    rms = np.sqrt((frames**2).mean(axis=1) + 1e-12)

    # Speech = windows above 20% of the peak; noise = the quietest 10%.
    floor = np.percentile(rms, 10)
    threshold = rms.max() * 0.2
    speech = rms > threshold
    snr = 20 * np.log10((rms[speech].mean() if speech.any() else 1e-6) / max(floor, 1e-6))

    # Brightness: how much of the energy is left above 3.5 kHz. Aggressive Opus flattens it.
    spectrum = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    freq = np.fft.rfftfreq(len(x), 1 / sr)
    total = (spectrum**2).sum() + 1e-12
    brightness = (spectrum[freq > 3500] ** 2).sum() / total

    return {
        "dur": dur,
        "silence": 1 - speech.mean(),
        "snr": snr,
        "brightness": brightness,
        "clipping": (np.abs(x) > 0.985).mean(),
        "instability": rms[speech].std() / (rms[speech].mean() + 1e-9) if speech.any() else 9.9,
    }


def score(m: dict) -> float:
    # Duration: 1.0 inside the ideal range, dropping outside it.
    lo, hi = IDEAL_DURATION
    if m["dur"] < lo:
        s_dur = max(0.0, m["dur"] / lo) ** 2
    elif m["dur"] > hi:
        s_dur = max(0.3, hi / m["dur"])
    else:
        s_dur = 1.0

    # Multiplicative on purpose: one serious defect (too much silence, too much
    # noise) spoils the whole sample. In a weighted sum, the file would make up
    # for it with the other scores and climb the ranking undeservedly.
    s_snr = np.clip((m["snr"] - 6) / 18, 0.05, 1)
    s_sil = np.clip(1 - m["silence"] * 1.8, 0.05, 1)
    s_bri = np.clip(m["brightness"] / 0.04, 0, 1)
    s_clip = 0.15 if m["clipping"] > 0.01 else 1.0
    s_stab = np.clip(1.7 - m["instability"], 0.3, 1)

    raw = s_dur * s_snr * s_sil * s_clip * (0.75 + 0.25 * s_bri) * (0.85 + 0.15 * s_stab)
    return float(100 * raw**0.6)  # compresses the scale, just for reading


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("folder")
    ap.add_argument("--copy", action="store_true", help="copies the top N to ./candidates")
    ap.add_argument("--top", type=int, default=8)
    args = ap.parse_args()

    folder = Path(args.folder)
    files = sorted(folder.glob("*.wav"))
    if not files:
        print(f"no .wav in {folder}")
        return 1

    rows = []
    for f in files:
        try:
            x, sr = load(f)
            if len(x) < sr * 0.5:
                continue
            m = analyze(x, sr)
            rows.append((score(m), f, m))
        except Exception as e:
            print(f"  skipped {f.name}: {e}")

    rows.sort(key=lambda r: -r[0])

    print(f"\n{len(rows)} files scored. Best {args.top}:\n")
    print(f"{'#':>2}  {'score':>5}  {'dur':>6}  {'SNR':>6}  {'silence':>7}  {'bright':>7}  file")
    print("-" * 78)
    for i, (points, f, m) in enumerate(rows[: args.top], 1):
        print(
            f"{i:>2}  {points:>5.1f}  {m['dur']:>5.1f}s  {m['snr']:>5.1f}dB  "
            f"{m['silence'] * 100:>6.0f}%  {m['brightness']:>7.3f}  {f.name}"
        )

    if args.copy:
        dest = folder / "candidates"
        dest.mkdir(exist_ok=True)
        for i, (points, f, _) in enumerate(rows[: args.top], 1):
            shutil.copy2(f, dest / f"{i:02d}_score{points:.0f}_{f.name}")
        print(f"\n-> {args.top} files copied to {dest}")

    print("\nNow listen to the top ones and pick the one that sounds most natural.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
