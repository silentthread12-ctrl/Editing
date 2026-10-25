"""Word-by-word highlighted captions (ASS format), with Devanagari <-> Roman (Hinglish) support."""
import re

DEVA = re.compile(r"[ऀ-ॿ]")
COLORS = {"yellow": "FFE600", "green": "3DFF7A", "cyan": "27E1FF", "pink": "FF4FA3", "orange": "FF9A1F", "white": "FFFFFF"}
SIZES = {"small": 64, "medium": 78, "large": 92}
MARGIN = {"low": 340, "middle": 820, "high": 1280}


def to_roman(word):
    """Devanagari -> readable Hinglish/Nepali in English letters (offline, approximate)."""
    if not DEVA.search(word):
        return word
    try:
        from indic_transliteration import sanscript
        out = sanscript.transliterate(word, sanscript.DEVANAGARI, sanscript.ITRANS)
    except Exception:
        return word
    out = out.replace(".n", "n").replace(".N", "n").replace("M", "n").replace("~n", "n").replace(".m", "n")
    out = out.replace("aa", "a").replace("ii", "i").replace("uu", "u").replace("A", "a").replace("I", "i").replace("U", "u")
    out = out.replace("RRi", "ri").replace("sh", "sh").replace("Sh", "sh").replace("S", "sh").replace("Ch", "chh")
    out = out.replace("T", "t").replace("D", "d").replace("N", "n").replace("H", "h").replace("L", "l")
    out = re.sub(r"[.^~'`]", "", out)
    # schwa deletion: word ending in a bare consonant is pronounced without the final 'a'
    last = [c for c in word if DEVA.match(c)]
    if last and "क" <= last[-1] <= "ह" and out.endswith("a") and len(out) > 2:
        out = out[:-1]
    return out.lower()


def _ts(t):
    t = max(0.0, t)
    cs = int(round(t * 100))
    return f"{cs // 360000}:{(cs // 6000) % 60:02d}:{(cs // 100) % 60:02d}.{cs % 100:02d}"


def _esc(s):
    return s.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def _bgr(rgb):
    return f"&H00{rgb[4:6]}{rgb[2:4]}{rgb[0:2]}&"


def build_ass(words, clip_start, opts, cfg, out_path):
    """words: [{start,end,word}] in source time. Returns True if captions were written."""
    script = opts.get("script", "same")
    ws = []
    for w in words:
        txt = w["word"].strip()
        if not txt:
            continue
        if script == "roman":
            txt = w.get("roman") or to_roman(txt)
        if opts.get("caps") and not DEVA.search(txt):
            txt = txt.upper()
        ws.append({"s": w["start"] - clip_start, "e": w["end"] - clip_start, "t": _esc(txt)})
    if not ws:
        return False

    # group into short lines
    lines, cur = [], []
    for w in ws:
        gap = (w["s"] - cur[-1]["e"]) if cur else 0
        chars = sum(len(x["t"]) for x in cur) + len(w["t"])
        if cur and (len(cur) >= 3 or chars > 20 or gap > 0.7 or cur[-1]["t"].endswith((".", "?", "!", "।"))):
            lines.append(cur)
            cur = []
        cur.append(w)
    if cur:
        lines.append(cur)

    size = SIZES.get(opts.get("size", "medium"), 78)
    color = _bgr(COLORS.get(opts.get("color", "yellow"), COLORS["yellow"]))
    margin = MARGIN.get(opts.get("position", "low"), 340)
    font_l, font_d = cfg["font_latin"], cfg["font_devanagari"]

    head = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Cap,{font_l},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,2,2,60,60,{margin},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    ev = []
    for li, line in enumerate(lines):
        has_deva = any(DEVA.search(w["t"]) for w in line)
        fn = f"{{\\fn{font_d}}}" if has_deva else ""
        for k, w in enumerate(line):
            st = w["s"]
            en = line[k + 1]["s"] if k + 1 < len(line) else max(w["e"], st + 0.25)
            if en - st < 0.05:
                en = st + 0.05
            parts = []
            for j, x in enumerate(line):
                if j == k:
                    parts.append(f"{{\\1c{color}\\fscx108\\fscy108}}{x['t']}{{\\1c&H00FFFFFF&\\fscx100\\fscy100}}")
                else:
                    parts.append(x["t"])
            ev.append(f"Dialogue: 0,{_ts(st)},{_ts(en)},Cap,,0,0,0,,{fn}{' '.join(parts)}")
    with open(out_path, "w", encoding="utf-8-sig") as f:
        f.write(head + "\n".join(ev) + "\n")
    return True
