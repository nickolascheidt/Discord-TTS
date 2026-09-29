"""WhatsApp chat export -> ranked sample candidates.

    python tools/whatsapp_samples.py exports --me "Your Name"

Takes a .zip, or a folder full of .zip files (processes all of them).
Extracts, splits by sender, converts to WAV, ranks by quality, and leaves the
best ones in work/NAME/candidates/.

Then you listen to the candidates and clone the best one with voice.py at the
project root.

The other person's name is detected by itself: in a 1:1 chat it's the sender
who isn't you. Only use voice messages from people who agreed to be cloned.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import unicodedata
import zipfile
from pathlib import Path

# The script lives in tools/, but writes to the project root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

WORK = ROOT / "work"
SAMPLE_RATE = 24_000
TOP_CANDIDATES = 8

PATTERN = re.compile(r"(?:\[.*?\]\s*|.*? - )([^:]+?): .*?([\w\-]+\.(?:opus|m4a|mp3|aac))")


def slug(name: str) -> str:
    """'João Silva' -> 'joao'. Folder and voice names have to be predictable."""
    base = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    base = re.sub(r"[^a-zA-Z0-9]+", "_", base).strip("_").lower()
    return base.split("_")[0] or "unknown"


# -------------------------------------------------------------------- prepare


def extract_zip(path: Path, my_name: str, dest_root: Path) -> Path | None:
    """Extracts the other side's voice messages. Returns the folder it created."""
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        txts = [n for n in names if n.lower().endswith(".txt")]
        if not txts:
            print(f"  {path.name}: no _chat.txt, skipping")
            return None

        text = z.read(txts[0]).decode("utf-8", errors="replace")
        lines = []
        senders: dict[str, int] = {}

        for line in text.splitlines():
            m = PATTERN.search(line.replace("‎", "").replace("‏", ""))
            if not m:
                continue
            who, file = m.group(1).strip(), m.group(2)
            senders[who] = senders.get(who, 0) + 1
            lines.append((who, file))

        others = [s for s in senders if s != my_name]
        if not others:
            print(f"  {path.name}: only found {list(senders)} — check --me")
            return None
        if len(others) > 1:
            print(f"  {path.name}: {len(others)} senders ({others}); using the most active")

        person = max(others, key=lambda s: senders[s])
        folder = dest_root / slug(person) / "raw"
        folder.mkdir(parents=True, exist_ok=True)

        copied = 0
        for who, file in lines:
            if who != person:
                continue
            source = next((n for n in names if n.endswith(file)), None)
            if source:
                (folder / file).write_bytes(z.read(source))
                copied += 1

        print(f"  {path.name}: {person} -> {copied} voice messages")
        return folder.parent if copied else None


def convert(person_folder: Path) -> int:
    """opus/m4a -> mono 24 kHz WAV with leveled loudness."""
    source, dest = person_folder / "raw", person_folder / "wav"
    dest.mkdir(exist_ok=True)

    files = [p for p in sorted(source.iterdir()) if p.suffix.lower() in {".opus", ".m4a", ".mp3", ".aac"}]
    ok = 0
    for f in files:
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(f), "-ac", "1", "-ar", str(SAMPLE_RATE),
             "-af", "loudnorm=I=-23:TP=-2:LRA=7", str(dest / f"{f.stem}.wav")],
            capture_output=True, text=True,
        )
        ok += r.returncode == 0
    return ok


def rank(person_folder: Path) -> None:
    """Reuses rank_samples.py instead of duplicating the scoring logic."""
    from rank_samples import analyze, load, score

    wavs = sorted((person_folder / "wav").glob("*.wav"))
    results = []
    for f in wavs:
        try:
            x, sr = load(f)
            if len(x) < sr * 0.5:
                continue
            m = analyze(x, sr)
            results.append((score(m), f, m))
        except Exception:
            continue

    results.sort(key=lambda r: -r[0])
    candidates = person_folder / "candidates"
    if candidates.exists():
        shutil.rmtree(candidates)
    candidates.mkdir()

    print(f"\n  best of {person_folder.name}:")
    for i, (points, f, m) in enumerate(results[:TOP_CANDIDATES], 1):
        shutil.copy2(f, candidates / f"{i:02d}_score{points:.0f}_{f.name}")
        print(f"    {i}. score {points:5.1f}  {m['dur']:5.1f}s  SNR {m['snr']:4.1f}dB  {f.name}")


def cmd_prepare(target: str, my_name: str) -> int:
    if shutil.which("ffmpeg") is None:
        print("ffmpeg not found on PATH.")
        return 1

    path = Path(target)
    zips = [path] if path.suffix.lower() == ".zip" else sorted(path.glob("*.zip"))
    if not zips:
        print(f"no .zip in {path}")
        return 1

    WORK.mkdir(exist_ok=True)
    people = []
    for z in zips:
        folder = extract_zip(z, my_name, WORK)
        if folder:
            people.append(folder)

    for folder in people:
        n = convert(folder)
        print(f"\n  {folder.name}: {n} WAVs converted")
        rank(folder)

    if people:
        print("\nListen to the candidates and then clone the best one with voice.py:")
        print(f"  python voice.py clone {people[0] / 'candidates' / '01_....wav'} {people[0].name}")
    return 0


# ----------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", help="a WhatsApp .zip export or a folder with several")
    ap.add_argument("--me", required=True, help="your name as it appears in _chat.txt")

    args = ap.parse_args()
    return cmd_prepare(args.target, args.me)


if __name__ == "__main__":
    sys.exit(main())
