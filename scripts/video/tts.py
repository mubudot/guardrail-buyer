#!/usr/bin/env python3
"""Synthesize one WAV per scene with Piper (offline TTS). Falls back to espeak-ng.

    pip install piper-tts
    # voice (CC BY 4.0 dataset): https://huggingface.co/rhasspy/piper-voices/tree/main/en/en_US/libritts_r/medium
    python scripts/video/tts.py --voice /path/en_US-libritts_r-medium.onnx --out build/video
Writes <out>/<scene>.wav and <out>/durations.json.
"""
import argparse, json, shutil, subprocess, sys, wave
from pathlib import Path

HERE = Path(__file__).resolve().parent


def duration(p: Path) -> float:
    with wave.open(str(p)) as w:
        return w.getnframes() / w.getframerate()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--voice", default="")
    ap.add_argument("--speaker", default="0")
    ap.add_argument("--length-scale", default="1.08")
    ap.add_argument("--out", default="build/video")
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    scenes = json.loads((HERE / "scenes.json").read_text())["scenes"]
    durs, engine = {}, None
    for s in scenes:
        wav = out / f"{s['id']}.wav"
        if a.voice and Path(a.voice).exists():
            engine = "piper"
            subprocess.run([sys.executable, "-m", "piper", "-m", a.voice, "-f", str(wav), "-s", a.speaker,
                            "--length-scale", a.length_scale, "--sentence-silence", "0.25"],
                           input=s["narration"].encode(), check=True, capture_output=True)
        elif shutil.which("espeak-ng"):
            engine = "espeak-ng"
            subprocess.run(["espeak-ng", "-v", "en-us", "-s", "155", "-w", str(wav), s["narration"]], check=True)
        else:
            sys.exit("no TTS engine: install piper-tts (+ a voice) or espeak-ng")
        durs[s["id"]] = round(duration(wav), 2)
        print(f"{s['id']}: {durs[s['id']]}s")
    (out / "durations.json").write_text(json.dumps({"engine": engine, "durations": durs}, indent=1))
    print("total narration", round(sum(durs.values()), 1), "s via", engine)


if __name__ == "__main__":
    main()
