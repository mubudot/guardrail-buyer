#!/usr/bin/env python3
"""Cut dead air, lay the per-scene narration on the recording, encode MP4 (ffmpeg).

    python scripts/video/assemble.py --build build/video --out docs/demo_draft.mp4
"""
import argparse, json, subprocess
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", default="build/video")
    ap.add_argument("--out", default="docs/demo_draft.mp4")
    a = ap.parse_args()
    b = Path(a.build)
    tl = json.loads((b / "timeline.json").read_text())
    cuts = sorted(tl["cuts"])

    def mapped(t):
        return t - sum(min(t, e) - s for s, e in cuts if t > s)

    length = mapped(tl["total"])
    vsel = "+".join(f"between(t,{s:.2f},{e:.2f})" for s, e in cuts)
    pre, cur = "", "vb0"
    for i, (s0, e0, x, y, w, h) in enumerate(tl.get("blurs", [])):  # hide the Sandbox buyer's email (raw time)
        pre += (f"[{cur}]split[m{i}][c{i}];[c{i}]crop={w}:{h}:{x}:{y},boxblur=18:4[bx{i}];"
                f"[m{i}][bx{i}]overlay={x}:{y}:enable='between(t,{s0:.2f},{e0:.2f})'[vb{i + 1}];")
        cur = f"vb{i + 1}"
    vf = (f"[0:v]fps=25[vb0];{pre}[{cur}]null" + (f",select='not({vsel})',setpts=N/25/TB" if cuts else "")
          + ",format=yuv420p[v]")
    inputs, af, labels = ["-i", tl["video"]], [], []
    for i, sc in enumerate(tl["scenes"], start=1):
        wav = b / f"{sc['id']}.wav"
        inputs += ["-i", str(wav)]
        ms = int(max(0.0, mapped(sc["start"]) + 0.25) * 1000)
        af.append(f"[{i}:a]aresample=44100,adelay={ms}|{ms}[a{i}]")
        labels.append(f"[a{i}]")
    af.append("".join(labels) + f"amix=inputs={len(labels)}:normalize=0,volume=0.85,apad,atrim=0:{length:.2f}[a]")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", ";".join([vf, *af]),
           "-map", "[v]", "-map", "[a]", "-t", f"{length:.2f}", "-c:v", "libx264", "-preset", "medium",
           "-crf", "23", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", a.out]
    subprocess.run(cmd, check=True)
    print(f"wrote {a.out}  length {length:.1f}s  (raw {tl['total']:.1f}s, cut {tl['total'] - length:.1f}s)")


if __name__ == "__main__":
    main()
