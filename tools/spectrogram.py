"""Side-by-side spectrogram of two audio files, to compare a sample with its output.

    python tools/spectrogram.py original.wav cloned.wav
    python tools/spectrogram.py a.wav b.wav -o comparison.png

Needs librosa and matplotlib — run it with the .venv's Python (requirements-prep.txt).
"""

from __future__ import annotations

import argparse

import librosa
import librosa.display
import matplotlib.pyplot as plt
import numpy as np


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("first")
    ap.add_argument("second")
    ap.add_argument("-o", "--output", default="spec.png", help="PNG to write (default spec.png)")
    args = ap.parse_args()

    fig, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True)
    for ax, path in zip(axes, (args.first, args.second)):
        y, sr = librosa.load(path, sr=None, mono=True)
        s = librosa.amplitude_to_db(np.abs(librosa.stft(y, n_fft=2048)), ref=np.max)
        librosa.display.specshow(s, sr=sr, x_axis="time", y_axis="hz", ax=ax, vmin=-80, vmax=0)
        ax.set_title(f"{path}  (sr={sr})")
    plt.tight_layout()
    plt.savefig(args.output, dpi=110)
    print(f"-> {args.output}")


if __name__ == "__main__":
    main()
