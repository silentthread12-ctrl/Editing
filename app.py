"""Shorts Maker - private local website. Run with start.bat (Windows)."""
import hmac
import io
import json
import os
import re
import shutil
import threading
import time
import traceback
import uuid
import webbrowser
import zipfile
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_file

from pipeline import clips as clipmod
from pipeline import common, transcribe
from pipeline import render as rendermod

HOSTED = bool(os.environ.get("HOSTED"))          # online (Hugging Face) mode
PORT = int(os.environ.get("PORT", 5055))
PASSWORD = os.environ.get("APP_PASSWORD", "")
ROOT = common.ROOT
app = Flask(__name__, static_folder=None)
LOCK = threading.Lock()          # one heavy job at a time
SAVE_LOCK = threading.Lock()
JOBS = {}


ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}


@app.before_request
def guard():
    """Optional password online; locally only this PC may talk to the server."""
    if PASSWORD:
        auth = request.authorization
        if not auth or not hmac.compare_digest((auth.password or "").encode(), PASSWORD.encode()):
            return Response("Password needed", 401, {"WWW-Authenticate": 'Basic realm="Shorts Maker"'})
    if not HOSTED:
        if request.host not in ALLOWED_HOSTS:
            abort(403)
        origin = request.headers.get("Origin")
        if origin and origin.replace("http://", "") not in ALLOWED_HOSTS:
            abort(403)


def jdir(jid):
    return common.WORK / jid


def save(job):
    with SAVE_LOCK:
        (jdir(job["id"]) / "state.json").write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")


def log(job, msg):
    print(f"[{job['id'][:6]}] {msg}", flush=True)
    job["log"].append(time.strftime("%H:%M:%S ") + msg)
    del job["log"][:-300]


def stage(job, status, message, progress=0.0):
    job.update(status=status, message=message, progress=progress)
    save(job)


def slug(s):
    s = re.sub(r"[^A-Za-z0-9]+", "-", s or "").strip("-")[:40]
    return s or "short"


def load_jobs():
    for p in common.WORK.glob("*/state.json"):
        try:
            j = json.loads(p.read_text(encoding="utf-8"))
            if j.get("status") in ("queued", "probing", "extracting", "transcribing", "selecting", "rendering"):
                j["status"] = "ready" if j.get("clips") else "error"
                j["message"] = "Interrupted when the app closed. " + ("You can render again." if j.get("clips") else "Please start again.")
            JOBS[j["id"]] = j
        except Exception:
            pass


# ------------------------------------------------------------------ analysis
def analyze(job):
    with LOCK:
        try:
            o = job["options"]
            d = jdir(job["id"])
            src = Path(job["source"])
            stage(job, "probing", "Reading the video...", 0.02)
            info = common.probe(src)
            job["info"] = info
            if not info["has_audio"]:
                raise RuntimeError("This video has no audio, so there is nothing to transcribe.")
            log(job, f"Video: {info['width']}x{info['height']}, {info['duration'] / 60:.1f} minutes")

            audio = d / "audio.wav"
            if not audio.exists():
                stage(job, "extracting", "Extracting audio...", 0.05)
                common.run_ffmpeg(["-i", src, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", audio],
                                  duration=info["duration"],
                                  on_progress=lambda p: job.update(progress=0.05 + 0.05 * p))

            if info["duration"] <= 0:       # some formats (webm) do not report a length
                import wave
                with wave.open(str(audio)) as w:
                    info["duration"] = w.getnframes() / w.getframerate()
                log(job, f"Length measured from audio: {info['duration'] / 60:.1f} minutes")
            tj = d / "transcript.json"
            tr = None
            if tj.exists():
                t = json.loads(tj.read_text(encoding="utf-8"))
                if t.get("setting") == o["language"] and t.get("quality") == o["quality"]:
                    tr = t
                    log(job, "Re-using the saved transcript.")
            if tr is None:
                stage(job, "transcribing", "Listening to the video (speech to text)...", 0.1)
                tr = transcribe.transcribe(audio, o["language"], o["quality"], info["duration"],
                                           lambda m: log(job, m),
                                           lambda p: job.update(progress=0.1 + 0.7 * p))
                tr["setting"], tr["quality"] = o["language"], o["quality"]
                tj.write_text(json.dumps(tr, ensure_ascii=False), encoding="utf-8")
            if not tr["segments"]:
                raise RuntimeError("No speech was found in this video.")
            job["language"] = tr["language"]
            log(job, f"Language: {tr['language']}, {sum(len(s['words']) for s in tr['segments'])} words")

            stage(job, "selecting", "Choosing the best moments...", 0.85)
            segs = tr["segments"]
            n, mn, mx = int(o["n_clips"]), float(o["min_len"]), float(o["max_len"])
            picks, picker = None, "offline"
            cfg = common.load_config()
            if o.get("smart") and cfg.get("gemini_key"):
                try:
                    picks = clipmod.gemini_pick(segs, cfg["gemini_key"], cfg.get("gemini_model", "auto"),
                                                n, mn, mx, tr["language"], lambda m: log(job, m))
                    picker = "gemini"
                except Exception as e:
                    log(job, f"Gemini failed ({str(e)[:200]}). Using the offline picker instead.")
            elif o.get("smart"):
                log(job, "Smart AI is on but no Gemini key is saved (Settings). Using the offline picker.")
            if not picks:
                energy = clipmod.load_energy(audio)
                picks = clipmod.heuristic_pick(segs, energy, n, mn, mx)
                picker = "offline"
            if not picks:
                raise RuntimeError("Could not find a clip in the requested length range. Try a wider length range.")
            job["picker"] = picker
            job["clips"] = [dict(p, id=i, status="pending", file=None, ver=0) for i, p in enumerate(picks)]
            log(job, f"Found {len(picks)} clips using the {picker} picker.")
            stage(job, "ready", "Pick the clips you like, then press Make shorts.", 1.0)
        except Exception as e:
            log(job, "ERROR: " + str(e))
            print(traceback.format_exc(), flush=True)
            stage(job, "error", str(e), 0.0)


# ------------------------------------------------------------------ rendering
def render_job(job, selected, ropts):
    with LOCK:
        try:
            d = jdir(job["id"])
            out = d / "out"
            out.mkdir(exist_ok=True)
            src = Path(job["source"])
            tr = json.loads((d / "transcript.json").read_text(encoding="utf-8"))
            words = [w for s in tr["segments"] for w in s["words"]]
            if ropts.get("script") == "auto":
                ropts["script"] = "roman" if tr.get("setting") == "hinglish" else "same"
            total = len(selected)
            for k, cid in enumerate(selected):
                c = next(x for x in job["clips"] if x["id"] == cid)
                c["status"] = "rendering"
                stage(job, "rendering", f"Making short {k + 1} of {total}...", k / total)
                log(job, f"Rendering clip {cid + 1}: {c['start']:.1f}s to {c['end']:.1f}s")
                fname = f"short_{cid + 1:02d}.mp4"
                try:
                    rendermod.render_clip(
                        src, job["info"], c, out / fname, ropts, words, d, lambda m: log(job, m),
                        lambda p: job.update(progress=(k + p) / total))
                    c.update(status="done", file=fname, ver=int(time.time()))
                except Exception as e:
                    c["status"] = "error"
                    c["error"] = str(e)
                    log(job, f"Clip {cid + 1} failed: {e}")
            ok = sum(1 for x in job["clips"] if x["status"] == "done")
            stage(job, "ready", f"Done. {ok} short(s) ready to watch and download below.", 1.0)
        except Exception as e:
            log(job, "ERROR: " + str(e))
            print(traceback.format_exc(), flush=True)
            stage(job, "error", str(e), 0.0)


# ------------------------------------------------------------------ routes
@app.route("/")
def index():
    return send_file(ROOT / "static" / "index.html")


@app.route("/api/status")
def status():
    ff = True
    try:
        common.tool("ffmpeg")
    except Exception:
        ff = False
    cfg = common.load_config()
    return jsonify(ffmpeg=ff, device=transcribe.device_label(), gemini=bool(cfg.get("gemini_key")), hosted=HOSTED)


@app.route("/api/settings", methods=["GET", "POST"])
def settings():
    cfg = common.load_config()
    if request.method == "POST":
        data = request.get_json(force=True)
        for k in ("gemini_key", "gemini_model", "font_latin", "font_devanagari"):
            if k in data and isinstance(data[k], str):
                cfg[k] = data[k].strip()
        common.save_config(cfg)
    return jsonify(gemini_key_set=bool(cfg["gemini_key"]), gemini_model=cfg["gemini_model"],
                   font_latin=cfg["font_latin"], font_devanagari=cfg["font_devanagari"])


@app.route("/api/test-gemini", methods=["POST"])
def test_gemini():
    cfg = common.load_config()
    if not cfg.get("gemini_key"):
        return jsonify(ok=False, error="No key saved yet."), 400
    try:
        import requests
        r = requests.get(f"{clipmod.GEMINI_BASE}/models", params={"pageSize": 5},
                         headers={"x-goog-api-key": cfg["gemini_key"]}, timeout=30)
        if r.status_code != 200:
            return jsonify(ok=False, error=f"Google said {r.status_code}: {r.text[:200]}"), 400
        return jsonify(ok=True, model=clipmod.pick_gemini_model(cfg["gemini_key"], cfg.get("gemini_model", "auto")))
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 400


def public(job):
    for _ in range(5):          # the worker thread may be changing it while we copy
        try:
            return json.loads(json.dumps(job, ensure_ascii=False))
        except (RuntimeError, TypeError):
            time.sleep(0.02)
    return {k: v for k, v in job.items()}


UPLOADS = common.WORK / "_uploads"
UPLOAD_ID = re.compile(r"^[0-9a-f]{12}$")


@app.route("/api/upload/start", methods=["POST"])
def up_start():
    data = request.get_json(force=True)
    uid = uuid.uuid4().hex[:12]
    d = UPLOADS / uid
    d.mkdir(parents=True)
    name = Path(str(data.get("name") or "video.mp4")).name
    ext = Path(name).suffix.lower()[:8] or ".mp4"
    (d / "meta.json").write_text(json.dumps({"name": name, "ext": ext, "next": 0}), encoding="utf-8")
    return jsonify(id=uid)


@app.route("/api/upload/<uid>/chunk", methods=["POST"])
def up_chunk(uid):
    if not UPLOAD_ID.match(uid) or not (UPLOADS / uid).is_dir():
        abort(404)
    d = UPLOADS / uid
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    i = int(request.args.get("i", -1))
    if i != meta["next"]:
        return jsonify(error="out of order", next=meta["next"]), 409
    with open(d / "data", "ab") as f:
        f.write(request.get_data())
    meta["next"] = i + 1
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return jsonify(next=meta["next"])


def cleanup_old(days=3):
    """Online mode: free disk space by removing old projects."""
    cutoff = time.time() - days * 86400
    for jid, j in list(JOBS.items()):
        if j["created"] < cutoff and j["status"] in ("ready", "error"):
            JOBS.pop(jid, None)
            shutil.rmtree(jdir(jid), ignore_errors=True)
    if UPLOADS.exists():
        for p in UPLOADS.iterdir():
            if p.stat().st_mtime < cutoff:
                shutil.rmtree(p, ignore_errors=True)


@app.route("/api/jobs", methods=["GET", "POST"])
def jobs():
    if request.method == "GET":
        return jsonify(sorted((public(j) for j in list(JOBS.values())), key=lambda j: -j["created"]))
    form = request.form
    jid = uuid.uuid4().hex[:12]
    d = jdir(jid)
    d.mkdir(parents=True)
    local = (form.get("local_path") or "").strip().strip('"')
    f = request.files.get("file")
    upid = (form.get("upload_id") or "").strip()
    if HOSTED:
        local = ""                      # never read files from the server's own disk
        cleanup_old()
    if upid:
        ud = UPLOADS / upid
        if not UPLOAD_ID.match(upid) or not (ud / "data").is_file():
            shutil.rmtree(d, ignore_errors=True)
            return jsonify(error="The upload was not finished. Please try again."), 400
        meta = json.loads((ud / "meta.json").read_text(encoding="utf-8"))
        dest = d / ("source" + meta["ext"])
        shutil.move(str(ud / "data"), str(dest))
        shutil.rmtree(ud, ignore_errors=True)
        source, name = str(dest), meta["name"]
    elif local:
        p = Path(local)
        if not p.is_file():
            shutil.rmtree(d, ignore_errors=True)
            return jsonify(error="That file path does not exist on this PC."), 400
        source, name = str(p), p.name
    elif f and f.filename:
        name = Path(f.filename).name
        dest = d / ("source" + Path(name).suffix.lower())
        f.save(dest)
        source = str(dest)
    else:
        shutil.rmtree(d, ignore_errors=True)
        return jsonify(error="Choose a video file or paste its path."), 400
    opts = {
        "language": form.get("language", "auto"),
        "quality": form.get("quality", "balanced"),
        "n_clips": max(1, min(30, int(form.get("n_clips", 5)))),
        "min_len": float(form.get("min_len", 25)),
        "max_len": float(form.get("max_len", 60)),
        "smart": form.get("smart", "1") == "1",
    }
    job = {"id": jid, "name": name, "source": source, "created": time.time(), "status": "queued",
           "message": "Waiting for its turn...", "progress": 0.0, "options": opts, "log": [], "clips": [], "info": None}
    JOBS[jid] = job
    save(job)
    threading.Thread(target=analyze, args=(job,), daemon=True).start()
    return jsonify(public(job))


def get_job(jid):
    j = JOBS.get(jid)
    if not j:
        abort(404)
    return j


@app.route("/api/jobs/<jid>")
def job_get(jid):
    return jsonify(public(get_job(jid)))


@app.route("/api/jobs/<jid>", methods=["DELETE"])
def job_delete(jid):
    j = get_job(jid)
    if j["status"] in ("probing", "extracting", "transcribing", "selecting", "rendering", "queued"):
        return jsonify(error="Wait for it to finish first."), 400
    JOBS.pop(jid, None)
    shutil.rmtree(jdir(jid), ignore_errors=True)
    return jsonify(ok=True)


@app.route("/api/jobs/<jid>/repick", methods=["POST"])
def job_repick(jid):
    j = get_job(jid)
    if j["status"] not in ("ready", "error"):
        return jsonify(error="Busy."), 400
    data = request.get_json(force=True)
    o = j["options"]
    for k, cast in (("n_clips", int), ("min_len", float), ("max_len", float)):
        if k in data:
            o[k] = cast(data[k])
    if "smart" in data:
        o["smart"] = bool(data["smart"])
    for k in ("language", "quality"):
        if data.get(k):
            o[k] = data[k]
    j["status"] = "queued"
    save(j)
    threading.Thread(target=analyze, args=(j,), daemon=True).start()
    return jsonify(public(j))


@app.route("/api/jobs/<jid>/render", methods=["POST"])
def job_render(jid):
    j = get_job(jid)
    if j["status"] not in ("ready", "error") or not j.get("clips"):
        return jsonify(error="Not ready."), 400
    data = request.get_json(force=True)
    sel = []
    dur = j["info"]["duration"]
    for c in data.get("clips", []):
        mine = next((x for x in j["clips"] if x["id"] == c["id"]), None)
        if not mine:
            continue
        mine["start"] = max(0.0, float(c["start"]))
        mine["end"] = min(dur, float(c["end"]))
        mine["title"] = str(c.get("title", mine["title"]))[:120]
        if mine["end"] - mine["start"] < 3:
            return jsonify(error=f"Clip {mine['id'] + 1} is shorter than 3 seconds."), 400
        sel.append(mine["id"])
    if not sel:
        return jsonify(error="Select at least one clip."), 400
    ropts = data.get("options", {})
    for cid in sel:
        next(x for x in j["clips"] if x["id"] == cid)["status"] = "queued"
    j["status"] = "queued"
    save(j)
    threading.Thread(target=render_job, args=(j, sel, ropts), daemon=True).start()
    return jsonify(public(j))


@app.route("/api/jobs/<jid>/source")
def job_source(jid):
    j = get_job(jid)
    return send_file(j["source"], conditional=True)


@app.route("/api/jobs/<jid>/out/<fname>")
def job_out(jid, fname):
    j = get_job(jid)
    c = next((x for x in j["clips"] if x.get("file") == fname), None)
    if not c:
        abort(404)
    p = jdir(jid) / "out" / fname
    if request.args.get("dl"):
        return send_file(p, as_attachment=True, download_name=f"{slug(c['title'])}-{fname}", conditional=True)
    return send_file(p, conditional=True)


@app.route("/api/jobs/<jid>/zip")
def job_zip(jid):
    j = get_job(jid)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for c in j["clips"]:
            if c.get("file"):
                z.write(jdir(jid) / "out" / c["file"], f"{slug(c['title'])}-{c['file']}")
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name="shorts.zip", mimetype="application/zip")


if __name__ == "__main__":
    load_jobs()
    if HOSTED:
        print(f"\n  Shorts Maker (online mode) on port {PORT}\n", flush=True)
        app.run(host="0.0.0.0", port=PORT, threaded=True)
    else:
        print(f"\n  Shorts Maker is running at http://127.0.0.1:{PORT}  (only this PC can open it)\n", flush=True)
        threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
        app.run(host="127.0.0.1", port=PORT, threaded=True)
