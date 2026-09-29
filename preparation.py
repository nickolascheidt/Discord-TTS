#!/usr/bin/env python3
"""
preparation.py - prepares reference audio for voice cloning (TTS).

Usage:
    python preparation.py path/to/audio.opus
    python preparation.py path/to/folder          # processes every audio file
    python preparation.py audio.ogg --enhance     # also writes the resemble-enhance variant
    python preparation.py audio.ogg --sr 22050    # output sample rate (default 24000)
    python preparation.py audio.ogg --trim        # cuts long silences
    python preparation.py audio.ogg --check       # speaker similarity (needs resemblyzer)

Output: written to THE SAME FOLDER as the input file.
    name_ref.wav       -> DeepFilterNet + loudnorm      (variant A)
    name_ref_enh.wav   -> resemble-enhance + loudnorm   (variant B, only with --enhance)

Dependencies:
    ffmpeg on PATH
    pip install -r requirements-prep.txt
    pip install resemble-enhance        (optional, for --enhance)
"""

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

AUDIO_EXTS = {".opus", ".ogg", ".oga", ".m4a", ".mp3", ".wav", ".flac", ".aac", ".webm", ".mp4"}
WORK_SR = 48000  # DeepFilterNet works at 48k


# ----------------------------------------------------------------------------
# utilities
# ----------------------------------------------------------------------------

def run(cmd):
    """Runs a command and raises with a readable stderr if it fails."""
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(map(str, cmd))}\n{p.stderr[-2000:]}")
    return p


def check_ffmpeg():
    if shutil.which("ffmpeg") is None:
        sys.exit("ERROR: ffmpeg not found on PATH. Install it before running.")


def to_wav(src: Path, dst: Path, trim: bool):
    """Decodes to mono 48k PCM 16 wav, with no dynamics processing at all."""
    af = []
    if trim:
        # cuts silence at the start and stretches > ~0.5s in the middle/end
        af.append(
            "silenceremove=start_periods=1:start_silence=0.1:start_threshold=-45dB"
            ":stop_periods=-1:stop_silence=0.5:stop_threshold=-45dB:detection=rms"
        )
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src)]
    if af:
        cmd += ["-af", ",".join(af)]
    cmd += ["-ac", "1", "-ar", str(WORK_SR), "-c:a", "pcm_s16le", str(dst)]
    run(cmd)


def loudnorm(src: Path, dst: Path, sr: int, lufs: float):
    """Normalizes loudness and does the final resample. No compressor, no gate, no EQ."""
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
        "-af", f"loudnorm=I={lufs}:TP=-2.0:LRA=7,aresample=resampler=soxr",
        "-ac", "1", "-ar", str(sr), "-c:a", "pcm_s16le", str(dst),
    ])


def diagnose(path: Path, label: str):
    """Audio diagnosis: this is what decides whether the clip is any good, not the filter."""
    x, sr = sf.read(str(path), dtype="float32", always_2d=True)
    x = x.mean(axis=1)
    dur = len(x) / sr
    peak = float(np.max(np.abs(x))) if len(x) else 0.0
    peak_db = 20 * np.log10(peak) if peak > 0 else -np.inf
    clipped = float(np.mean(np.abs(x) >= 0.999)) * 100

    print(f"    [{label}] {dur:.1f}s | peak {peak_db:+.1f} dBFS | clip {clipped:.2f}%")

    warnings = []
    if dur < 12:
        warnings.append("too short (<12s) - the embedding will be unstable")
    elif dur > 45:
        warnings.append("too long (>45s) - cut the best 15-30s stretch")
    if clipped > 0.05:
        warnings.append("CLIPPED - the distortion can't be undone, find another clip")
    if peak_db < -30:
        warnings.append("very quiet - probably bad capture or the person far from the mic")
    for w in warnings:
        print(f"      ! {w}")
    return dur


# ----------------------------------------------------------------------------
# denoisers (lazy import: loading torch takes a while)
# ----------------------------------------------------------------------------

class DeepFilter:
    def __init__(self):
        from df.enhance import init_df
        print("  loading DeepFilterNet...")
        self.model, self.state, _ = init_df()

    def __call__(self, src: Path, dst: Path):
        from df.enhance import enhance, load_audio, save_audio
        audio, _ = load_audio(str(src), sr=self.state.sr())
        out = enhance(self.model, self.state, audio)
        save_audio(str(dst), out, self.state.sr())


class ResembleEnhance:
    def __init__(self):
        import torch
        self.torch = torch
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        if self.device == "cpu":
            print("  warning: resemble-enhance without a GPU will be SLOW (minutes per clip)")

    def __call__(self, src: Path, dst: Path):
        import torchaudio
        from resemble_enhance.enhancer.inference import enhance as re_enhance
        dwav, sr = torchaudio.load(str(src))
        dwav = dwav.mean(dim=0)
        wav, new_sr = re_enhance(
            dwav, sr, self.device, nfe=64, solver="midpoint", lambd=0.9, tau=0.5
        )
        sf.write(str(dst), wav.cpu().numpy(), new_sr)


# ----------------------------------------------------------------------------
# objective identity check
# ----------------------------------------------------------------------------

def speaker_similarity(a: Path, b: Path):
    """Cosine between speaker embeddings. If it drops a lot, the processing is
    erasing identity - even if it sounds better to your ear."""
    from resemblyzer import VoiceEncoder, preprocess_wav
    enc = VoiceEncoder(verbose=False)
    ea = enc.embed_utterance(preprocess_wav(str(a)))
    eb = enc.embed_utterance(preprocess_wav(str(b)))
    return float(np.dot(ea, eb) / (np.linalg.norm(ea) * np.linalg.norm(eb)))


# ----------------------------------------------------------------------------

def process(src: Path, args, df, re_):
    print(f"\n>> {src.name}")
    tmpdir = Path(tempfile.mkdtemp(prefix="prepref_"))
    try:
        raw = tmpdir / "raw.wav"
        to_wav(src, raw, args.trim)
        diagnose(raw, "original")

        outputs = []

        # variant A: DeepFilterNet
        dfw = tmpdir / "df.wav"
        df(raw, dfw)
        out_a = src.with_name(f"{src.stem}_ref.wav")
        loudnorm(dfw, out_a, args.sr, args.lufs)
        diagnose(out_a, "deepfilternet")
        outputs.append(out_a)

        # variant B: resemble-enhance (runs from raw, it has its own denoiser)
        if re_ is not None:
            rew = tmpdir / "re.wav"
            re_(raw, rew)
            out_b = src.with_name(f"{src.stem}_ref_enh.wav")
            loudnorm(rew, out_b, args.sr, args.lufs)
            diagnose(out_b, "resemble-enhance")
            outputs.append(out_b)

        if args.check:
            for o in outputs:
                sim = speaker_similarity(raw, o)
                flag = "  <- suspicious, the identity drifted" if sim < 0.80 else ""
                print(f"    similarity vs original: {sim:.3f} ({o.name}){flag}")

        for o in outputs:
            print(f"    written: {o}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="Prepares reference audio for voice cloning.")
    ap.add_argument("path", type=Path, help="audio file or folder")
    ap.add_argument("--sr", type=int, default=24000,
                    help="output sample rate; use what your model expects (Pocket TTS: 24000)")
    ap.add_argument("--lufs", type=float, default=-23.0, help="target loudness (default -23)")
    ap.add_argument("--enhance", action="store_true", help="also writes the resemble-enhance variant")
    ap.add_argument("--trim", action="store_true", help="cuts long silences")
    ap.add_argument("--check", action="store_true", help="measures speaker similarity (resemblyzer)")
    args = ap.parse_args()

    check_ffmpeg()

    if not args.path.exists():
        sys.exit(f"ERROR: path doesn't exist: {args.path}")

    if args.path.is_dir():
        targets = sorted(p for p in args.path.iterdir()
                         if p.suffix.lower() in AUDIO_EXTS and "_ref" not in p.stem)
    else:
        targets = [args.path]

    if not targets:
        sys.exit("ERROR: no audio found.")

    df = DeepFilter()
    re_ = ResembleEnhance() if args.enhance else None

    failures = 0
    for p in targets:
        try:
            process(p, args, df, re_)
        except Exception as e:
            failures += 1
            print(f"    ERROR in {p.name}: {e}")

    print(f"\ndone: {len(targets) - failures}/{len(targets)} files")


if __name__ == "__main__":
    main()
