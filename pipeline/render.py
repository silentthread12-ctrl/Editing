"""Cut one clip, reframe to 9:16, burn captions, export an MP4."""
import os
from pathlib import Path

from . import captions, reframe
from .common import run_ffmpeg, load_config


def render_clip(src, info, clip, out_path, opts, all_words, workdir, log, progress):
    cfg = load_config()
    ss = max(0.0, clip["start"] - 0.15)
    en = min(info["duration"], clip["end"] + 0.35)
    dur = en - ss
    if dur < 1:
        raise RuntimeError("Clip is too short.")

    graph = reframe.plan(str(src), ss, dur, info, opts.get("reframe", "auto"), log)

    words = [w for w in all_words if w["end"] > ss and w["start"] < en]
    if (opts.get("captions", True) and opts.get("script") == "roman" and cfg.get("gemini_key")
            and any(captions.DEVA.search(w["word"]) for w in words)):
        try:
            from .clips import gemini_romanize
            rom = gemini_romanize([w["word"] for w in words], cfg["gemini_key"], cfg.get("gemini_model", "auto"), log)
            if rom:
                words = [dict(w, roman=r) for w, r in zip(words, rom)]
        except Exception as e:
            log(f"  Gemini spelling skipped ({str(e)[:80]})")
    ass_name = f"cap_{clip['id']}.ass"
    has_caps = False
    if opts.get("captions", True):
        has_caps = captions.build_ass(words, ss, opts, cfg, Path(workdir) / ass_name)
    if has_caps:
        fonts = ""
        if os.name == "nt":
            fonts = ":fontsdir='C\\:/Windows/Fonts'"
        graph += f";[v0]ass='{ass_name}'{fonts}[v]"
    else:
        graph += ";[v0]null[v]"

    args = ["-ss", f"{ss:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
            "-filter_complex", graph, "-map", "[v]"]
    if info.get("has_audio"):
        args += ["-map", "0:a:0"]
        if opts.get("loudnorm", True):
            args += ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"]
        args += ["-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
    args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", "30",
             "-movflags", "+faststart", str(out_path)]
    run_ffmpeg(args, duration=dur, on_progress=progress, cwd=str(workdir))
