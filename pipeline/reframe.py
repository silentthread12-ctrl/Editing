"""Turn 16:9 into 9:16: follow the speaker's face, or fall back to a blurred-background fit."""
import shutil
import tempfile
from pathlib import Path

from .common import run_ffmpeg

SAMPLE_FPS = 2


def detect_faces(video, start, dur, log):
    """Return list of face centre x (fraction 0..1 of frame width) or None, sampled at SAMPLE_FPS."""
    import cv2
    tmp = Path(tempfile.mkdtemp(prefix="faces_"))
    try:
        run_ffmpeg(["-ss", f"{start:.2f}", "-t", f"{dur:.2f}", "-i", video,
                    "-vf", f"fps={SAMPLE_FPS},scale=480:-2", "-q:v", "4", str(tmp / "f_%05d.jpg")])
        cascades = [cv2.CascadeClassifier(cv2.data.haarcascades + n) for n in
                    ("haarcascade_frontalface_default.xml", "haarcascade_frontalface_alt2.xml")]
        centres = []
        for f in sorted(tmp.glob("f_*.jpg")):
            img = cv2.imread(str(f), cv2.IMREAD_GRAYSCALE)
            if img is None:
                centres.append(None)
                continue
            h, w = img.shape
            best = None
            for c in cascades:
                faces = c.detectMultiScale(img, 1.12, 5, minSize=(max(24, w // 20), max(24, w // 20)))
                for (x, y, fw, fh) in faces:
                    if best is None or fw * fh > best[2] * best[3]:
                        best = (x, y, fw, fh)
                if best is not None:
                    break
            centres.append((best[0] + best[2] / 2) / w if best else None)
        return centres
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _median(vals):
    s = sorted(vals)
    return s[len(s) // 2]


def build_path(centres, src_w, crop_w, dur):
    """Smooth camera path: stays still, moves only when the speaker really moves. Returns [(t, x_px)]."""
    valid = [c for c in centres if c is not None]
    if not valid:
        return None
    # fill gaps with nearest known value
    filled, last = [], None
    for c in centres:
        if c is not None:
            last = c
        filled.append(last)
    nxt = None
    for i in range(len(filled) - 1, -1, -1):
        if centres[i] is not None:
            nxt = centres[i]
        if filled[i] is None:
            filled[i] = nxt
    # median smoothing (window 5 samples = 2.5 s)
    sm = [_median(filled[max(0, i - 2): i + 3]) for i in range(len(filled))]

    def px(c):
        return max(0.0, min(src_w - crop_w, c * src_w - crop_w / 2))

    cur = px(sm[0])
    path = [(0.0, cur)]
    thr = 0.30 * crop_w
    last_move = -9.0
    for i in range(1, len(sm)):
        t = i / SAMPLE_FPS
        tgt = px(sm[i])
        if abs(tgt - cur) > thr and t - last_move >= 1.5:
            path.append((t, cur))
            path.append((t + 0.5, tgt))
            cur, last_move = tgt, t
    path.append((dur + 1.0, cur))
    return path


def x_expression(path):
    """Build an ffmpeg expression in t for a piecewise-linear path."""
    pts = []
    for t, x in path:
        if pts and t <= pts[-1][0] + 0.01:
            continue
        pts.append((round(t, 2), round(x, 1)))
    if len(pts) == 1:
        return f"{pts[0][1]}"
    expr = f"{pts[-1][1]}"
    for i in range(len(pts) - 2, -1, -1):
        (t0, x0), (t1, x1) = pts[i], pts[i + 1]
        expr = f"if(lt(t,{t1}),{x0}+({x1}-{x0})*(t-{t0})/({t1}-{t0}),{expr})"
    return expr


def blur_fit_graph():
    return ("[0:v]split=2[a][b];"
            "[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=10:2,"
            "scale=1080:1920,eq=brightness=-0.08[bg];"
            "[b]scale=1080:1920:force_original_aspect_ratio=decrease:flags=lanczos[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[v0]")


def crop_graph(src_w, src_h, x_expr):
    cw = int(round(src_h * 9 / 16 / 2)) * 2
    cw = min(cw, src_w - src_w % 2)
    return f"[0:v]crop={cw}:{src_h - src_h % 2}:x='{x_expr}':y=0,scale=1080:1920:flags=lanczos,setsar=1[v0]", cw


def plan(video, start, dur, info, mode, log):
    """Return (filter_graph_ending_in_[v0])."""
    w, h = info["width"], info["height"]
    if w / h <= 0.60:           # already vertical
        return blur_fit_graph()
    if mode == "blur":
        return blur_fit_graph()
    cw = min(int(round(h * 9 / 16 / 2)) * 2, w)
    if mode == "center":
        g, _ = crop_graph(w, h, f"{(w - cw) / 2:.1f}")
        return g
    # auto: follow the face
    try:
        centres = detect_faces(video, start, dur, log)
    except Exception as e:
        log(f"  face detection unavailable ({str(e)[:80]}), using blurred-background layout")
        return blur_fit_graph()
    rate = sum(c is not None for c in centres) / max(1, len(centres))
    if rate < 0.35:
        log(f"  no clear speaker face found ({int(rate * 100)}% of frames), using blurred-background layout")
        return blur_fit_graph()
    path = build_path(centres, w, cw, dur)
    log(f"  following the speaker ({int(rate * 100)}% of frames had a face)")
    g, _ = crop_graph(w, h, x_expression(path))
    return g
