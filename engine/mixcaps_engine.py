"""MixCaps engine: audio -> word-timed captions for English, Telugish and Hinglish.

Usage:
  python mixcaps_engine.py transcribe --audio a.wav --lang telugish --out job_dir [options]
  python mixcaps_engine.py rechunk --job job_dir --words-per-caption 2

Writes into job_dir:
  progress.txt     "percent|stage|message" (rewritten while running; "100|done|..." or "-1|error|...")
  words.json       every recognised word with times
  captions.lua     `return { {s=..., e=..., t="..."}, ... }` for the Resolve script (no JSON parser needed there)
  captions.srt     same captions as SRT
  log.txt          details
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
sys.path.insert(0, HERE)
APPDIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "MixCaps")


def _cuda_dll_dirs():
    roots = {os.path.join(os.path.dirname(os.path.dirname(sys.executable)), "Lib", "site-packages"),
             os.path.join(os.path.dirname(sys.executable), "Lib", "site-packages")}
    try:
        import site
        roots.update(site.getsitepackages())
    except Exception:
        pass
    for r in roots:
        for d in glob.glob(os.path.join(r, "nvidia", "*", "bin")):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            try:
                os.add_dll_directory(d)
            except Exception:
                pass


_cuda_dll_dirs()

import numpy as np  # noqa: E402

from romanize import romanize, has_indic  # noqa: E402
from engfix import Lexicon, fuse, load_list  # noqa: E402


class Job:
    def __init__(self, out):
        self.out = out
        os.makedirs(out, exist_ok=True)
        self.logf = open(os.path.join(out, "log.txt"), "a", encoding="utf-8")

    def log(self, *a):
        msg = " ".join(str(x) for x in a)
        self.logf.write(time.strftime("%H:%M:%S ") + msg + "\n")
        self.logf.flush()
        try:
            print(msg, flush=True)
        except Exception:
            pass

    _last = None

    def progress(self, pct, stage, msg=""):
        """Never raises: the app may be reading the file at the same moment (Windows locks it)."""
        key = (int(pct), stage)
        final = pct < 0 or pct >= 100
        if key == self._last and not final:
            return
        self._last = key
        line = f"{int(pct)}|{stage}|{msg}"
        path = os.path.join(self.out, "progress.txt")
        for _ in range(40 if final else 3):
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(line)
                break
            except OSError:
                time.sleep(0.05)
        self.log(f"[{int(pct)}%] {stage} {msg}")


def load_config():
    cfg = {}
    for p in (os.path.join(APPDIR, "config.json"), os.path.join(HERE, "config.json")):
        if os.path.exists(p):
            try:
                cfg.update(json.load(open(p, encoding="utf-8-sig")))
            except Exception:
                pass
    cfg.setdefault("models_dir", os.path.join(APPDIR, "models"))
    return cfg


def pick_device(want):
    import ctranslate2
    if want in ("cuda", "auto"):
        try:
            if ctranslate2.get_cuda_device_count() > 0:
                return "cuda", "float16"
        except Exception:
            pass
        if want == "cuda":
            raise RuntimeError("No NVIDIA GPU / CUDA found. Use device 'cpu'.")
    return "cpu", "int8"


# Speed tiers. "main" transcribes; "hint" is the English pass used to spell English words in Telugish/Hinglish.
TIERS = {
    "light":    {"english": "small",          "hinglish": "small",          "telugish": "telugu-small",  "hint": "small",
                 "label": "Light", "about": "Fastest and easy on the PC. Fine without a GPU. Less accurate."},
    "balanced": {"english": "large-v3-turbo", "hinglish": "large-v3-turbo", "telugish": "telugu-medium", "hint": "large-v3-turbo",
                 "label": "Balanced", "about": "Good accuracy at good speed. About 3 GB of GPU memory, or a fast CPU."},
    "best":     {"english": "large-v3",       "hinglish": "large-v3-turbo", "telugish": "telugu-large",  "hint": "large-v3-turbo",
                 "label": "Best", "about": "Most accurate. About 5 GB of GPU memory; slow without a GPU."},
}
# Rough sizes: (GB on disk, GB of GPU memory in float16)
MODEL_INFO = {
    "small": (0.5, 1.0), "large-v3-turbo": (1.6, 2.5), "large-v3": (3.1, 4.5),
    "telugu-small": (0.5, 1.0), "telugu-medium": (1.5, 2.5), "telugu-large": (3.1, 4.5),
}
TELUGU_KEYS = {"telugu-small": ("telugu_small_path", "whisper-telugu-small-ct2"),
               "telugu-medium": ("telugu_medium_path", "whisper-telugu-medium-ct2"),
               "telugu-large": ("telugu_path", "whisper-telugu-large-v2-ct2")}


def model_ref(cfg, kind):
    """Local path or downloadable name. Telugu models are local-only (None if not installed)."""
    md = cfg["models_dir"]
    kind = {"turbo": "large-v3-turbo", "best": "large-v3", "telugu": "telugu-large"}.get(kind, kind)
    if kind in TELUGU_KEYS:
        key, folder = TELUGU_KEYS[kind]
        for p in (cfg.get(key), os.path.join(md, folder)):
            if p and os.path.exists(os.path.join(p, "model.bin")):
                return p
        return None
    if kind == "hindi":
        p = cfg.get("hindi_path") or os.path.join(md, "whisper-hindi-large-v2-ct2")
        return p if os.path.exists(os.path.join(p, "model.bin")) else None
    key = {"large-v3-turbo": "turbo_path", "large-v3": "best_path", "small": "small_path"}.get(kind)
    if key and cfg.get(key) and os.path.isdir(cfg[key]):
        return cfg[key]
    return kind  # faster-whisper downloads it on first use


def is_installed(cfg, kind):
    ref = model_ref(cfg, kind)
    if ref is None:
        return False
    if os.path.isdir(ref):
        return True
    repo = {"small": "Systran--faster-whisper-small", "large-v3": "Systran--faster-whisper-large-v3",
            "large-v3-turbo": "mobiuslabsgmbh--faster-whisper-large-v3-turbo"}.get(kind, "")
    return bool(glob.glob(os.path.join(cfg["models_dir"], f"models--{repo}", "snapshots", "*", "model.bin")))


def resolve_main(cfg, tier, lang, job=None):
    """Pick the model for this tier, falling back to whichever Telugu model is installed."""
    want = TIERS[tier][lang]
    if lang == "telugish":
        for k in [want] + [k for k in ("telugu-large", "telugu-medium", "telugu-small") if k != want]:
            ref = model_ref(cfg, k)
            if ref:
                if k != want and job:
                    job.log(f"{want} not installed; using {k}")
                return k, ref
        if job:
            job.log("no Telugu model installed; using the general model (much less accurate for Telugu)")
        return "large-v3-turbo", model_ref(cfg, "large-v3-turbo")
    return want, model_ref(cfg, want)


LOW_USAGE = {"on": False}


def load_model(ref, device, ctype, cfg, job):
    from faster_whisper import WhisperModel
    kw = {}
    if LOW_USAGE["on"]:
        kw["cpu_threads"] = max(2, (os.cpu_count() or 4) // 2)
        if device == "cuda":
            ctype = "int8_float16"  # about half the GPU memory
    job.log("loading model", ref, device, ctype, kw)
    try:
        return WhisperModel(ref, device=device, compute_type=ctype, download_root=cfg["models_dir"], **kw)
    except Exception as e:
        if device == "cuda":
            job.log("GPU load failed, falling back to CPU:", e)
            return WhisperModel(ref, device="cpu", compute_type="int8", download_root=cfg["models_dir"], **kw)
        raise


def run_pass(model, audio, language, job, p0, p1, label, prompt=None, segs_out=None):
    """Transcribe, returning words [{'s','e','w','p'}] with segment-quality filtering.
    segs_out (list) receives (start, end, avg_logprob, first_word_index, end_word_index) per kept segment."""
    dur = len(audio) / 16000.0
    segs, info = model.transcribe(
        audio, language=language, word_timestamps=True, beam_size=3 if LOW_USAGE["on"] else 5,
        vad_filter=True, initial_prompt=prompt,
        log_prob_threshold=-1.0, no_speech_threshold=0.6, compression_ratio_threshold=2.4)
    words = []
    for s in segs:
        job.progress(p0 + (p1 - p0) * min(1.0, s.end / max(dur, 0.1)), label, f"{s.end:5.0f}s / {dur:.0f}s")
        if s.compression_ratio > 2.8:
            job.log("drop (repetition loop):", s.text)
            continue
        first = len(words)
        if segs_out is not None:
            segs_out.append([s.start, s.end, s.avg_logprob, first, None])
        ws = [w for w in (s.words or []) if w.word.strip()]
        texts = [w.word.strip() for w in ws]
        if any("�" in t for t in texts):
            # a multi-byte character was split between two word tokens: take spellings from the segment text
            seg_tokens = s.text.split()
            if len(seg_tokens) == len(ws):
                texts = seg_tokens
            else:
                merged, mws = [], []
                for w, t in zip(ws, texts):
                    if merged and "�" in merged[-1]:
                        merged[-1] = merged[-1] + t
                        mws[-1] = (mws[-1][0], w)
                    else:
                        merged.append(t)
                        mws.append((w, w))
                texts = [t.replace("�", "") for t in merged]
                ws = None
                for (a, b), t in zip(mws, texts):
                    if t:
                        words.append(dict(s=round(a.start, 3), e=round(b.end, 3), w=t, p=round(min(a.probability, b.probability), 3)))
                if segs_out is not None:
                    segs_out[-1][4] = len(words)
                continue
        for w, t in zip(ws, texts):
            t = t.replace("�", "")
            if t:
                words.append(dict(s=round(w.start, 3), e=round(w.end, 3), w=t, p=round(w.probability, 3)))
        if segs_out is not None:
            segs_out[-1][4] = len(words)
    return words


def detect_language(model, audio, job=None, n=4, length=20):
    """Telugish / Hinglish / English from a few speech samples.
    Whisper's own detector calls code-mixed Telugu 'English', so instead force the general model to write
    Telugu and look at the script it uses: Telugu speech -> Telugu script, Hindi speech -> Devanagari
    (it refuses to write Telugu), English speech -> Latin letters."""
    from faster_whisper.vad import get_speech_timestamps, VadOptions
    ts = get_speech_timestamps(audio, VadOptions(min_silence_duration_ms=500))
    if not ts:
        return "english", 0.0, {}
    total = len(audio)
    starts = []
    for k in range(n):
        target = int(total * (k + 0.5) / n)
        st = min(ts, key=lambda t: abs(t["start"] - target))["start"]
        if st not in starts:
            starts.append(st)
    text = ""
    for st in starts:
        segs, _ = model.transcribe(audio[st: st + length * 16000], language="te", beam_size=1,
                                   condition_on_previous_text=False, without_timestamps=True, vad_filter=True)
        text += " " + " ".join(x.text for x in segs)
    te = len(re.findall("[\u0C00-\u0C7F]", text))
    hi = len(re.findall("[\u0900-\u097F]", text))
    la = len(re.findall("[A-Za-z]", text))
    tot = max(te + hi + la, 1)
    scores = {"telugish": te / tot, "hinglish": hi / tot, "english": la / tot}
    if scores["telugish"] >= 0.35:
        lang = "telugish"
    elif scores["hinglish"] >= 0.35:
        lang = "hinglish"
    else:
        lang = "english"
    if job:
        job.log("language detect:", {k: round(v, 2) for k, v in scores.items()}, "->", lang)
    return lang, scores[lang], scores


def english_spans(model, audio, segs, native, job):
    """Which English-pass sentences are really English speech (not translated Telugu/Hindi)?
    Uses Whisper's language detector on each sentence's audio."""
    spans = []
    for s, e, lp, a, b in segs:
        if e - s < 0.8 or b is None or b - a < 2 or lp < -0.7:
            continue
        clip = audio[int(s * 16000): int(e * 16000)]
        try:
            _, _, probs = model.detect_language(clip)
        except Exception as ex:
            job.log("language detection failed:", ex)
            return []
        probs = dict(probs)
        pe, pn = probs.get("en", 0.0), probs.get(native, 0.0)
        if pe / max(pe + pn, 1e-6) >= 0.8:
            spans.append((s, e, a, b))
    return spans


def splice_english(words, hints, spans):
    """Replace native-model words inside English sentences with the English pass's words."""
    if not spans:
        return words

    def inside(w):
        m = (w["s"] + w["e"]) / 2
        return any(s - 0.05 <= m <= e + 0.05 for s, e, _, _ in spans)

    # Second check: in real English speech the native model mostly heard English words (recovered by fuse()).
    # In noisy Telugu/Hindi speech the English pass "translates" instead, and those words stay native.
    good = []
    for sp in spans:
        s, e = sp[0], sp[1]
        native = [w for w in words if s - 0.05 <= (w["s"] + w["e"]) / 2 <= e + 0.05]
        # no native words under it = the native model heard no speech there (game noise): don't trust the English pass
        if len(native) >= 2 and sum(1 for w in native if w.get("en")) / len(native) >= 0.6:
            good.append(sp)
    spans = good

    def inside(w):
        m = (w["s"] + w["e"]) / 2
        return any(s - 0.05 <= m <= e + 0.05 for s, e, _, _ in spans)

    kept = [w for w in words if not inside(w)]
    for s, e, a, b in spans:
        for w in split_tokens(hints[a:b], "english"):
            w["en"] = True
            kept.append(w)
    kept.sort(key=lambda w: w["s"])
    return kept


def split_tokens(words, lang):
    """Romanize Indic tokens, split off punctuation; keep a 'punct' flag for chunk breaks."""
    out = []
    for w in words:
        t = w["w"]
        indic = has_indic(t)
        if indic:
            t = romanize(t, "te" if lang == "telugish" else "hi")
        brk = bool(re.search(r"[.?!,;:]$", t))
        t = t.strip(" \"“”")
        t = re.sub(r"[.,;:]+$", "", t) if lang == "english" else re.sub(r"[.,;:!?]+$", "", t)
        if not t:
            continue
        out.append(dict(s=w["s"], e=w["e"], w=t, p=w["p"], indic=indic, brk=brk))
    return out


def collapse_loops(words, max_rep=4):
    out = []
    for w in words:
        if len(out) >= max_rep and all(x["w"].lower() == w["w"].lower() for x in out[-max_rep:]):
            continue
        out.append(w)
    return out


def apply_replacements(words, rules):
    """rules: list of (pattern_words(lowercase list), replacement string). Matches across word boundaries."""
    if not rules:
        return words
    i, out = 0, []
    while i < len(words):
        hit = None
        for pat, rep in rules:
            n = len(pat)
            if [re.sub(r"[^\w']", "", x["w"].lower()) for x in words[i:i + n]] == pat:
                hit = (n, rep)
                break
        if hit:
            n, rep = hit
            grp = words[i:i + n]
            out.append(dict(grp[0], w=rep, e=grp[-1]["e"], fixed=True))
            i += n
        else:
            out.append(words[i])
            i += 1
    return out


def parse_rules(lines):
    rules = []
    for line in lines:
        if "=>" in line:
            a, b = line.split("=>", 1)
        elif "->" in line:
            a, b = line.split("->", 1)
        else:
            continue
        pat = [re.sub(r"[^\w']", "", x.lower()) for x in a.split()]
        pat = [x for x in pat if x]
        if pat and b.strip():
            rules.append((pat, b.strip()))
    rules.sort(key=lambda r: -len(r[0]))
    return rules


# ------------------------------------------------------------ voice timing
def voice_activity(audio, hop=0.01):
    """Frame-level speech-band energy (dB), 10 ms hops."""
    sr = 16000
    n = int(sr * hop)
    x = audio[: len(audio) - len(audio) % n]
    if len(x) == 0:
        return np.zeros(0)
    # crude speech band: first difference removes rumble, moving-average smooths hiss
    d = np.diff(x, prepend=x[:1])
    frames = d.reshape(-1, n)
    db = 20 * np.log10(np.sqrt(np.mean(frames ** 2, axis=1)) + 1e-9)
    return db


def snap_captions(caps, audio, job):
    db = voice_activity(audio)
    if len(db) == 0:
        return caps
    thr = max(np.percentile(db, 30) + 8, np.max(db) - 45)
    on = db > thr
    # onsets: off -> on (held for 3 frames), offsets: on -> off (held for 8 frames)
    onsets, offsets = [], []
    for i in range(1, len(on) - 8):
        if on[i] and not on[i - 1] and on[i:i + 3].all():
            onsets.append(i * 0.01)
        if on[i - 1] and not on[i] and not on[i:i + 8].any():
            offsets.append(i * 0.01)
    onsets, offsets = np.array(onsets), np.array(offsets)
    moved = 0
    for c in caps:
        if len(onsets):
            k = np.argmin(np.abs(onsets - c["s"]))
            # never pull a caption ahead of the word; only nudge it onto the voice start just after
            if -0.03 <= onsets[k] - c["s"] <= 0.15:
                c["s"] = float(onsets[k]); moved += 1
        if len(offsets):
            k = np.argmin(np.abs(offsets - c["e"]))
            if -0.12 <= offsets[k] - c["e"] <= 0.3:
                c["e"] = float(offsets[k])
    job.log(f"voice snap: moved {moved}/{len(caps)} caption starts (threshold {thr:.1f} dB)")
    return caps


# ------------------------------------------------------------ captions
def chunk(words, max_words=3, max_chars=20, gap_break=0.45, hold=0.6, min_dur=0.3):
    caps, cur = [], []

    def flush():
        if cur:
            caps.append(dict(s=cur[0]["s"], e=cur[-1]["e"], t=" ".join(x["w"] for x in cur)))
            cur.clear()

    for i, w in enumerate(words):
        if cur:
            gap = w["s"] - cur[-1]["e"]
            text_len = len(" ".join(x["w"] for x in cur)) + 1 + len(w["w"])
            if len(cur) >= max_words or text_len > max_chars or gap > gap_break or cur[-1].get("brk"):
                flush()
        cur.append(w)
    flush()
    # a caption can't last longer than its words take to say (Whisper sometimes stretches into silence)
    for c in caps:
        max_dur = min(3.0, 0.45 + 0.085 * len(c["t"]))
        if c["e"] - c["s"] > max_dur:
            c["s"] = c["e"] - max_dur
    # timing: keep each caption up until the next one if close, never overlap
    for i, c in enumerate(caps):
        nxt = caps[i + 1]["s"] if i + 1 < len(caps) else None
        end = c["e"] + hold * 0.5
        if nxt is not None:
            if nxt - c["e"] <= hold:
                end = nxt
            end = min(end, nxt)
        c["e"] = max(end, c["s"] + min_dur)
        if nxt is not None and c["e"] > nxt:
            c["e"] = nxt
    return [c for c in caps if c["e"] > c["s"]]


def lua_str(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "") + '"'


def srt_time(t):
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def write_outputs(job, caps):
    with open(os.path.join(job.out, "captions.lua"), "w", encoding="utf-8") as f:
        f.write("return {\n")
        for c in caps:
            f.write(f"  {{s={c['s']:.3f}, e={c['e']:.3f}, t={lua_str(c['t'])}}},\n")
        f.write("}\n")
    json.dump([dict(s=round(c["s"], 3), e=round(c["e"], 3), t=c["t"]) for c in caps],
              open(os.path.join(job.out, "captions.json"), "w", encoding="utf-8"), ensure_ascii=False)
    with open(os.path.join(job.out, "captions.srt"), "w", encoding="utf-8") as f:
        for i, c in enumerate(caps, 1):
            f.write(f"{i}\n{srt_time(c['s'])} --> {srt_time(c['e'])}\n{c['t']}\n\n")


def user_lists(cfg):
    base = cfg.get("user_dir", APPDIR)
    words = load_list(os.path.join(base, "custom_words.txt"))
    rules = parse_rules(load_list(os.path.join(base, "replacements.txt")))
    return words, rules


def default_rules(lang):
    lines = load_list(os.path.join(HERE, f"replace_{lang}.txt"))
    return parse_rules(lines)


def transcribe(args):
    job = Job(args.out)
    t0 = time.time()
    try:
        cfg = load_config()
        job.progress(1, "loading", "reading audio")
        from faster_whisper import decode_audio
        audio = decode_audio(args.audio, sampling_rate=16000)
        dur = len(audio) / 16000.0
        job.log(f"audio {args.audio} {dur:.1f}s lang={args.lang} quality={args.quality}")
        LOW_USAGE["on"] = bool(args.low_usage)
        device, ctype = pick_device(args.device)
        tier = {"fast": "balanced"}.get(args.quality, args.quality)
        lang = args.lang
        hints, spans, note = [], [], ""
        general = None
        hint_ref = model_ref(cfg, TIERS[tier]["hint"])
        if lang == "auto":
            job.progress(2, "detecting", "detecting language")
            general = load_model(hint_ref, device, ctype, cfg, job)
            lang, conf, scores = detect_language(general, audio, job)
            job.detected = (lang, round(conf, 2))
            job.progress(4, "detecting", f"detected {lang} ({conf:.0%})")
        kind, ref = resolve_main(cfg, tier, lang, job)
        if lang == "hinglish" and cfg.get("hinglish_model") == "hindi" and model_ref(cfg, "hindi"):
            kind, ref = "hindi", model_ref(cfg, "hindi")
        job.log(f"tier={tier} lang={lang} main={kind} ({ref}) low_usage={LOW_USAGE['on']}")
        if general is not None and ref != hint_ref:
            del general
            general = None
        m = general if general is not None else load_model(ref, device, ctype, cfg, job)
        code = {"english": "en", "hinglish": "hi", "telugish": "te"}[lang]
        job.progress(5, "transcribing", lang.capitalize())
        raw = run_pass(m, audio, code, job, 5, 90 if lang == "english" else 60, "transcribing")
        general = m if ref == hint_ref else None
        del m
        if lang != "english":
            if general is None:
                general = load_model(hint_ref, device, ctype, cfg, job)
            job.progress(60, "english words", "listening for English words")
            hint_segs = []
            hints = run_pass(general, audio, "en", job, 60, 85, "english words", segs_out=hint_segs)
            job.progress(86, "english words", "finding English sentences")
            spans = english_spans(general, audio, hint_segs, code, job)
            eng_secs = sum(e - s for s, e, _, _ in spans)
            speech_secs = sum(e - s for s, e, _, _, _ in hint_segs) or 1
            job.log(f"english sentences: {len(spans)} of {len(hint_segs)} ({eng_secs:.0f}s of {speech_secs:.0f}s)")
            if eng_secs / speech_secs > 0.6:
                note = "This sounds mostly English - English mode will be more accurate."
        general = None
        job.progress(90, "fixing", "cleaning words")
        words = split_tokens(raw, lang)
        words = collapse_loops(words)
        extra_words, user_rules = user_lists(cfg)
        if lang != "english":
            stops = load_list(os.path.join(HERE, "stop_te.txt" if lang == "telugish" else "stop_hi.txt"))
            lex = Lexicon(extra_words, stops)
            words = fuse(words, hints, lex, "te" if lang == "telugish" else "hi")
            n_before = len(spans)
            words = splice_english(words, hints, spans)
            job.log(f"english sentences used: checked {n_before}")
            job.log(f"english words recovered: {sum(1 for w in words if w.get('en'))}/{len(words)}")
        words = apply_replacements(words, user_rules + default_rules(lang))
        json.dump(dict(lang=lang, duration=dur, words=words, hints=hints), open(os.path.join(job.out, "words.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=0)
        caps = chunk(words, args.words_per_caption, args.max_chars)
        if args.snap:
            caps = snap_captions(caps, audio, job)
            caps = chunk_fix_overlaps(caps)
        write_outputs(job, caps)
        info = dict(lang=lang, model=kind, tier=tier, detected=getattr(job, "detected", None), note=note,
                    seconds=round(time.time() - t0, 1), captions=len(caps))
        json.dump(info, open(os.path.join(job.out, "result.json"), "w", encoding="utf-8"))
        job.progress(100, "done", f"{len(caps)} captions in {time.time() - t0:.0f}s" + (f". {note}" if note else ""))
    except Exception as e:
        job.log(traceback.format_exc())
        job.progress(-1, "error", str(e).replace("|", "/")[:300])
        sys.exit(1)


def chunk_fix_overlaps(caps):
    for i in range(len(caps) - 1):
        if caps[i]["e"] > caps[i + 1]["s"]:
            caps[i]["e"] = caps[i + 1]["s"]
    return [c for c in caps if c["e"] - c["s"] > 0.05]


def rechunk(args):
    job = Job(args.job)
    d = json.load(open(os.path.join(args.job, "words.json"), encoding="utf-8"))
    caps = chunk(d["words"], args.words_per_caption, args.max_chars)
    write_outputs(job, caps)
    job.progress(100, "done", f"{len(caps)} captions")


def download(args):
    """Fetch the general model(s) so the first caption job doesn't stall."""
    from faster_whisper import WhisperModel
    cfg = load_config()
    for kind in args.models.split(","):
        ref = model_ref(cfg, kind)
        print("downloading", kind, ref, "->", cfg["models_dir"], flush=True)
        WhisperModel(ref, device="cpu", compute_type="int8", download_root=cfg["models_dir"])
        print("ok", kind, flush=True)


def system_info(cfg):
    import ctranslate2
    info = {"gpu": None, "vram_gb": 0, "ram_gb": 0, "cpus": os.cpu_count() or 0, "cuda": False}
    try:
        info["cuda"] = ctranslate2.get_cuda_device_count() > 0
    except Exception:
        pass
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5,
                             creationflags=0x08000000 if os.name == "nt" else 0).stdout
        name, mem = out.strip().splitlines()[0].rsplit(",", 1)
        info["gpu"], info["vram_gb"] = name.strip(), round(int(mem) / 1024, 1)
    except Exception:
        pass
    try:
        import ctypes

        class MS(ctypes.Structure):
            _fields_ = [("l", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                        ("avail", ctypes.c_ulonglong), ("tp", ctypes.c_ulonglong), ("ap", ctypes.c_ulonglong),
                        ("tv", ctypes.c_ulonglong), ("av", ctypes.c_ulonglong), ("ae", ctypes.c_ulonglong)]
        ms = MS()
        ms.l = ctypes.sizeof(MS)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
        info["ram_gb"] = round(ms.total / 2 ** 30, 1)
    except Exception:
        pass
    v = info["vram_gb"] if info["cuda"] else 0
    info["recommended"] = "best" if v >= 7 else ("balanced" if v >= 4 else "light")
    tiers = {}
    for t, d in TIERS.items():
        tiers[t] = dict(label=d["label"], about=d["about"], models={
            lg: dict(name=d[lg], installed=is_installed(cfg, d[lg]), disk_gb=MODEL_INFO[d[lg]][0], vram_gb=MODEL_INFO[d[lg]][1])
            for lg in ("english", "hinglish", "telugish")})
    info["tiers"] = tiers
    return info


def check(args):
    cfg = load_config()
    info = system_info(cfg)
    if getattr(args, "json", False):
        print(json.dumps(info))
        return
    print("device:", "cuda" if info["cuda"] else "cpu", "| gpu:", info["gpu"], info["vram_gb"], "GB | ram:", info["ram_gb"], "GB")
    print("recommended tier:", info["recommended"])
    for t, d in info["tiers"].items():
        print(f"  {t}:", ", ".join(f"{lg}={m['name']}{'' if m['installed'] else ' (not installed)'}" for lg, m in d["models"].items()))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("download")
    d.add_argument("--models", default="turbo")
    c = sub.add_parser("check")
    c.add_argument("--json", action="store_true")
    t = sub.add_parser("transcribe")
    t.add_argument("--audio", required=True)
    t.add_argument("--lang", choices=["auto", "english", "telugish", "hinglish"], default="auto")
    t.add_argument("--out", required=True)
    t.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])
    t.add_argument("--quality", default="balanced", choices=["light", "balanced", "best", "fast"])
    t.add_argument("--low-usage", type=int, default=0)
    t.add_argument("--words-per-caption", type=int, default=3)
    t.add_argument("--max-chars", type=int, default=20)
    t.add_argument("--snap", type=int, default=1)
    r = sub.add_parser("rechunk")
    r.add_argument("--job", required=True)
    r.add_argument("--words-per-caption", type=int, default=3)
    r.add_argument("--max-chars", type=int, default=20)
    args = ap.parse_args()
    if sys.stdout:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    {"transcribe": transcribe, "rechunk": rechunk, "download": download, "check": check}[args.cmd](args)


if __name__ == "__main__":
    main()
