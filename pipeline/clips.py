"""Choose the best moments: Gemini (free tier, smart) with an offline heuristic fallback."""
import json
import re
import time
import wave

import numpy as np
import requests

KW_EN = ("secret mistake mistakes never always truth nobody why how biggest worst best important money free stop "
         "warning shocking crazy actually lesson learn story problem solution million billion rich fail failed "
         "success scary dangerous easy simple hack tip tips reason").split()
KW_HI = ("kabhi sach raaz raz galti sabse bahut zaroori zaruri paisa paise dhyan dekho suno kyun kyu kaise matlab "
         "asli bada khatarnak pagal sahi galat dhoka mushkil aasan tareeka seekho "
         "कभी सच राज़ राज गलती सबसे बहुत ज़रूरी जरूरी पैसा पैसे ध्यान देखो सुनो क्यों कैसे मतलब असली खतरनाक "
         "पागल सही गलत धोखा मुश्किल आसान तरीका सीखो").split()
KW_NE = ("kahilyai sacho galti sabaibhanda dherai jaruri paisa hera sunnus kina kasari samasya ramro naramro dukha "
         "कहिल्यै साँचो गल्ती सबैभन्दा धेरै जरुरी पैसा हेर सुन्नुस् किन कसरी समस्या राम्रो नराम्रो दुःख").split()
KW = set(w.lower() for w in KW_EN + KW_HI + KW_NE)
TOKEN = re.compile(r"[\wऀ-ॿ]+")
END_PUNCT = (".", "?", "!", "।", "|", "…")


# ---------------------------------------------------------------- audio energy
def load_energy(wav_path):
    """Loudness in dB for every second of the audio."""
    out = []
    with wave.open(str(wav_path)) as w:
        sr = w.getframerate()
        while True:
            raw = w.readframes(sr * 60)
            if not raw:
                break
            a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            n = len(a) // sr
            if n:
                sec = a[: n * sr].reshape(n, sr)
                out.extend(20 * np.log10(np.sqrt((sec ** 2).mean(axis=1)) + 1e-6))
            rest = a[n * sr:]
            if len(rest) > sr // 4:
                out.append(20 * np.log10(np.sqrt((rest ** 2).mean()) + 1e-6))
    return np.array(out, dtype=np.float32)


# ---------------------------------------------------------------- heuristic picker
def _seg_feats(seg):
    text = seg["text"]
    toks = TOKEN.findall(text.lower())
    return (len(seg["words"]) or len(toks),
            sum(t in KW for t in toks),
            text.count("?") + text.count("？"),
            text.count("!"),
            len(re.findall(r"\d+", text)))


def heuristic_pick(segments, energy, n, min_len, max_len):
    if not segments:
        return []
    S = len(segments)
    feats = np.array([_seg_feats(s) for s in segments], dtype=np.float32)
    pre = np.vstack([np.zeros(5), np.cumsum(feats, axis=0)])
    if len(energy) > 5:
        p20, p90 = np.percentile(energy, [20, 90])
        if p90 - p20 < 1:
            p90 = p20 + 1
        e_pre = np.concatenate([[0], np.cumsum(energy)])
        sil_pre = np.concatenate([[0], np.cumsum(energy < p20)])
    else:
        p20 = p90 = 0
        e_pre = sil_pre = None
    pref = min_len + (max_len - min_len) * 0.7
    cands = []
    for i in range(S):
        st = segments[i]["start"]
        hook_kw = _seg_feats(segments[i])
        hook = 1.0 if (hook_kw[2] or hook_kw[1] or hook_kw[4]) else 0.35
        for j in range(i, S):
            en = segments[j]["end"]
            dur = en - st
            if dur > max_len:
                break
            if dur < min_len:
                continue
            words, kw, q, ex, num = pre[j + 1] - pre[i]
            s_kw = min(1.0, (kw / dur) * 30 / 3)
            s_q = min(1.0, (q + ex) / 4)
            wps = words / dur
            s_den = 1 - min(1.0, abs(wps - 2.6) / 2.0)
            if e_pre is not None:
                a = min(int(st), len(energy) - 1)
                b = max(a + 1, min(len(energy), int(en)))
                mean_db = (e_pre[b] - e_pre[a]) / (b - a)
                s_en = float(np.clip((mean_db - p20) / (p90 - p20), 0, 1))
                s_sil = float(np.clip(1 - 2 * (sil_pre[b] - sil_pre[a]) / (b - a), 0, 1))
            else:
                s_en = s_sil = 0.5
            s_end = 1.0 if segments[j]["text"].strip().endswith(END_PUNCT) else 0.4
            s_len = float(np.clip(1 - abs(dur - pref) / (max_len - min_len + 1), 0, 1))
            parts = {"a strong opening": hook * 0.15, "key words": s_kw * 0.20, "questions and emphasis": s_q * 0.10,
                     "good speaking pace": s_den * 0.15, "energetic delivery": s_en * 0.15,
                     "few pauses": s_sil * 0.10, "a clean ending": s_end * 0.08, "good length": s_len * 0.07}
            score = sum(parts.values()) * 100
            top = sorted(parts, key=parts.get, reverse=True)[:2]
            cands.append((score, st, en, i, top))
    cands.sort(reverse=True)
    picked = []
    for score, st, en, i, top in cands:
        if all(en <= p["start"] or st >= p["end"] for p in picked):
            hook = segments[i]["text"].strip()
            title = " ".join(hook.split()[:9])
            picked.append({"start": round(st, 2), "end": round(en, 2), "title": title, "hook": hook,
                           "score": int(round(score)), "reason": "Offline pick: " + " and ".join(top) + "."})
        if len(picked) >= n:
            break
    return picked


# ---------------------------------------------------------------- Gemini picker
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"


def pick_gemini_model(key, wanted="auto"):
    if wanted and wanted != "auto":
        return wanted
    try:
        r = requests.get(f"{GEMINI_BASE}/models", params={"pageSize": 200}, headers={"x-goog-api-key": key}, timeout=30)
        r.raise_for_status()
        best, best_v = None, -1.0
        for m in r.json().get("models", []):
            name = m.get("name", "")
            mt = re.fullmatch(r"models/gemini-(\d+(?:\.\d+)?)-flash", name)
            if mt and "generateContent" in m.get("supportedGenerationMethods", []):
                v = float(mt.group(1))
                if v > best_v:
                    best, best_v = name.split("/", 1)[1], v
        if best:
            return best
    except Exception:
        pass
    return "gemini-2.5-flash"


def _gem_call(key, model, prompt, schema, log, tries=4):
    url = f"{GEMINI_BASE}/models/{model}:generateContent"
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.4, "responseMimeType": "application/json", "responseSchema": schema}}
    last = ""
    for k in range(tries):
        r = requests.post(url, json=body, headers={"x-goog-api-key": key}, timeout=300)
        if r.status_code in (429, 500, 503):
            last = f"{r.status_code}"
            log(f"Gemini is busy or rate limited ({last}), waiting and retrying...")
            time.sleep(8 * (k + 1))
            continue
        if r.status_code != 200:
            raise RuntimeError(f"Gemini error {r.status_code}: {r.text[:300]}")
        data = r.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text)
    raise RuntimeError("Gemini kept refusing (rate limit). Try again in a minute.")


SCHEMA = {"type": "OBJECT", "properties": {"clips": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
    "start": {"type": "NUMBER"}, "end": {"type": "NUMBER"}, "title": {"type": "STRING"},
    "hook": {"type": "STRING"}, "reason": {"type": "STRING"}, "score": {"type": "INTEGER"}},
    "required": ["start", "end", "title", "score"]}}}, "required": ["clips"]}


def _fmt(segs):
    return "\n".join(f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in segs)


def _snap(c, segments, min_len, max_len):
    starts = np.array([s["start"] for s in segments])
    ends = np.array([s["end"] for s in segments])
    si = int(np.abs(starts - c["start"]).argmin())
    ei = int(np.abs(ends - c["end"]).argmin())
    if abs(starts[si] - c["start"]) > 4:
        st = c["start"]
    else:
        st = float(starts[si])
    en = float(ends[ei]) if abs(ends[ei] - c["end"]) <= 4 else c["end"]
    dur = en - st
    if dur < min_len * 0.6 or dur > max_len * 1.3:
        return None
    return {"start": round(st, 2), "end": round(en, 2), "title": c.get("title", "").strip() or "Short",
            "hook": c.get("hook", ""), "score": int(c.get("score", 70)),
            "reason": c.get("reason", "") or "Picked by Gemini."}


def gemini_romanize(words, key, model_name, log):
    """Natural Hinglish spelling for Devanagari words. Returns a list the same length, or None."""
    model = pick_gemini_model(key, model_name)
    schema = {"type": "OBJECT", "properties": {"words": {"type": "ARRAY", "items": {"type": "STRING"}}},
              "required": ["words"]}
    prompt = ("Rewrite each word below in natural Hinglish / Roman Nepali as people type it in chat "
              "(e.g. 'ज़रूरी' -> 'zaroori', 'क्या' -> 'kya'). Words already in English letters stay unchanged. "
              f"Return exactly {len(words)} items in the same order, one per input word.\n"
              + json.dumps(words, ensure_ascii=False))
    out = _gem_call(key, model, prompt, schema, log).get("words")
    if isinstance(out, list) and len(out) == len(words):
        return [str(x) for x in out]
    log("  Gemini spelling had the wrong length, using the offline spelling instead")
    return None


def gemini_pick(segments, key, model_name, n, min_len, max_len, language, log):
    model = pick_gemini_model(key, model_name)
    log(f"Asking Gemini ({model}) to choose the best moments...")
    total = segments[-1]["end"] - segments[0]["start"]
    chunks, cur, cur_start = [], [], segments[0]["start"]
    for s in segments:
        if cur and s["end"] - cur_start > 2400:
            chunks.append(cur)
            cur, cur_start = [], s["start"]
        cur.append(s)
    if cur:
        chunks.append(cur)
    langname = {"en": "English", "hi": "Hindi", "ne": "Nepali", "hinglish": "Hinglish (Hindi written in English letters)"}.get(
        language, "unknown (could be English, Hindi, Nepali or Hinglish)")
    results = []
    for ch in chunks:
        share = (ch[-1]["end"] - ch[0]["start"]) / max(total, 1)
        want = max(1, int(np.ceil(n * share)) + (1 if len(chunks) > 1 else 0))
        prompt = (
            "You are an expert short-form video editor (TikTok, Instagram Reels, YouTube Shorts).\n"
            "Below is a timestamped transcript of a long video. Spoken language: " + langname + ". "
            "It may be English, Hindi, Nepali or Hinglish, often with English words mixed into Hindi or Nepali.\n"
            f"Choose the {want} best moments to cut into standalone vertical shorts.\n"
            "Rules:\n"
            f"- each clip must be between {min_len} and {max_len} seconds\n"
            "- start at the beginning of a sentence and end after a complete thought\n"
            "- the first 3 seconds must hook the viewer; the clip must make sense without the rest of the video\n"
            "- clips must not overlap\n"
            "- prefer surprising claims, stories with a payoff, strong opinions, useful tips, emotional or funny moments\n"
            "- avoid intros, outros, sponsor reads, filler and small talk\n"
            "- start and end are seconds taken from the transcript line boundaries\n"
            "- title: catchy, max 8 words, same language and writing style as the speech "
            "(use English letters if the speaker uses Hinglish)\n"
            "- hook: the opening line of the clip; reason: one short English sentence on why it works; score 1-100\n\n"
            "TRANSCRIPT:\n" + _fmt(ch))
        data = _gem_call(key, model, prompt, SCHEMA, log)
        results.extend(data.get("clips", []))
    out = []
    for c in sorted(results, key=lambda x: -x.get("score", 0)):
        sn = _snap(c, segments, min_len, max_len)
        if sn and all(sn["end"] <= p["start"] or sn["start"] >= p["end"] for p in out):
            out.append(sn)
        if len(out) >= n:
            break
    return out
