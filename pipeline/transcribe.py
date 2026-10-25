"""Speech to text with faster-whisper (offline, free). Handles English, Hindi, Nepali, Hinglish."""
import os
import site
from pathlib import Path

import numpy as np

MODEL_BY_QUALITY = {"fast": "small", "balanced": "large-v3-turbo", "best": "large-v3"}
HINGLISH_PROMPT = ("Aaj hum is video mein baat karenge. Yeh bahut important hai, toh dhyan se suno. "
                   "Okay, let's start. Mujhe lagta hai ki ye sabse best tareeka hai.")

_model = None
_model_key = None
_device_used = "cpu"


def _add_cuda_paths():
    """Make pip-installed NVIDIA libraries (cuBLAS / cuDNN) visible on Windows."""
    if os.name != "nt":
        return
    bases = set(site.getsitepackages() + [site.getusersitepackages()])
    for base in bases:
        nv = Path(base) / "nvidia"
        if nv.exists():
            for b in nv.glob("*/bin"):
                try:
                    os.add_dll_directory(str(b))
                except Exception:
                    pass
                os.environ["PATH"] = str(b) + os.pathsep + os.environ.get("PATH", "")


def has_cuda():
    try:
        _add_cuda_paths()
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def device_label():
    return "NVIDIA GPU" if has_cuda() else "CPU"


def get_model(quality, log):
    global _model, _model_key, _device_used
    _add_cuda_paths()
    from faster_whisper import WhisperModel
    name = MODEL_BY_QUALITY.get(quality, MODEL_BY_QUALITY["balanced"])
    if _model is not None and _model_key == name:
        return _model
    log(f"Loading speech model '{name}' (the first time it downloads about 0.5-3 GB, please wait)...")
    model = None
    if has_cuda():
        try:
            model = WhisperModel(name, device="cuda", compute_type="float16")
            # Force the CUDA libraries to load now so a missing DLL fails here, not mid-video.
            list(model.transcribe(np.zeros(16000 * 2, dtype=np.float32))[0])
            _device_used = "cuda"
            log("Using your NVIDIA GPU.")
        except Exception as e:
            log(f"GPU not usable ({str(e)[:120]}). Using CPU instead.")
            model = None
    if model is None:
        model = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=os.cpu_count() or 4)
        _device_used = "cpu"
    _model, _model_key = model, name
    return model


def transcribe(audio_path, language, quality, duration, log, progress):
    model = get_model(quality, log)
    lang, prompt = None, None
    if language == "hinglish":
        lang, prompt = "hi", HINGLISH_PROMPT
    elif language in ("en", "hi", "ne"):
        lang = language
    log("Transcribing speech (this is the slowest step for long videos)...")
    seg_iter, info = model.transcribe(
        str(audio_path), language=lang, task="transcribe", beam_size=5,
        word_timestamps=True, vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 600},
        condition_on_previous_text=False, initial_prompt=prompt,
    )
    segments = []
    for s in seg_iter:
        if getattr(s, "no_speech_prob", 0) > 0.9 and getattr(s, "avg_logprob", 0) < -1.5:
            continue
        words = [{"start": round(w.start, 2), "end": round(w.end, 2), "word": w.word.strip()}
                 for w in (s.words or []) if w.word.strip()]
        if not words:
            continue
        segments.append({"start": round(s.start, 2), "end": round(s.end, 2),
                         "text": s.text.strip(), "words": words})
        if duration:
            progress(min(0.99, s.end / duration))
    detected = "hinglish" if language == "hinglish" else info.language
    return {"language": detected, "segments": segments}
