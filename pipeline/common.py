"""Shared helpers: paths, config, FFmpeg runner, probing."""
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "workspace"
WORK.mkdir(exist_ok=True)
CONFIG_FILE = ROOT / "config.json"

DEFAULTS = {
    "gemini_key": "",
    "gemini_model": "auto",
    "font_latin": "Arial Black",
    "font_devanagari": "Nirmala UI",
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def tool(name):
    ext = ".exe" if os.name == "nt" else ""
    local = ROOT / "bin" / (name + ext)
    if local.exists():
        return str(local)
    found = shutil.which(name)
    if found:
        return found
    raise RuntimeError(f"{name} was not found. Close this and run start.bat again so it can download it.")


def probe(path):
    r = subprocess.run(
        [tool("ffprobe"), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if r.returncode != 0:
        raise RuntimeError("Could not read this video file: " + r.stderr[-300:])
    data = json.loads(r.stdout)
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not v:
        raise RuntimeError("No video stream found in this file.")
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    w, h = int(v["width"]), int(v["height"])
    rot = 0
    try:
        rot = int(float(v.get("tags", {}).get("rotate", 0)))
    except Exception:
        pass
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(float(sd["rotation"]))
    if abs(rot) % 180 == 90:
        w, h = h, w
    dur = float(data.get("format", {}).get("duration") or v.get("duration") or 0)
    return {"duration": dur, "width": w, "height": h, "has_audio": has_audio}


def run_ffmpeg(args, duration=None, on_progress=None, cwd=None):
    """Run ffmpeg; raise RuntimeError with the error tail on failure."""
    cmd = [tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin",
           "-progress", "pipe:1", "-nostats", "-y"] + [str(a) for a in args]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         encoding="utf-8", errors="replace", cwd=cwd)
    err = []
    t = threading.Thread(target=lambda: err.append(p.stderr.read()), daemon=True)
    t.start()
    for line in p.stdout:
        if on_progress and duration and line.startswith("out_time_us="):
            val = line.split("=", 1)[1].strip()
            if val.isdigit():
                on_progress(min(1.0, int(val) / 1e6 / duration))
    p.wait()
    t.join(timeout=5)
    if p.returncode != 0:
        raise RuntimeError("FFmpeg failed: " + (err[0][-800:] if err else "unknown error"))
