"""MixCaps app: local web UI (127.0.0.1 only) + talks to the Resolve bridge through %LOCALAPPDATA%\\MixCaps\\ipc."""
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "MixCaps")
IPC = os.path.join(APP, "ipc")
JOBS = os.path.join(APP, "jobs")
PORT_FILE = os.path.join(APP, "app_port.txt")
os.makedirs(IPC, exist_ok=True)
os.makedirs(JOBS, exist_ok=True)

LOG = open(os.path.join(APP, "app_log.txt"), "a", encoding="utf-8")


def log(*a):
    LOG.write(time.strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(x) for x in a) + "\n")
    LOG.flush()


# ------------------------------------------------------------------ bridge
def to_lua(v):
    if v is None:
        return "nil"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(float(v)) if isinstance(v, float) else str(v)
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "") + '"'
    if isinstance(v, (list, tuple)):
        return "{" + ",".join(to_lua(x) for x in v) + "}"
    if isinstance(v, dict):
        return "{" + ",".join(f"[{to_lua(str(k))}]={to_lua(x)}" for k, x in v.items()) + "}"
    raise TypeError(type(v))


_cmd_lock = threading.Lock()
_cmd_id = [int(time.time()) % 100000 * 100]


_last_beat = [0.0]


def bridge_alive():
    try:
        s = open(os.path.join(IPC, "bridge.txt"), encoding="utf-8").read()
        _last_beat[0] = max(_last_beat[0], float(s.split()[0]))
    except Exception:
        pass  # mid-write or missing: fall back to the last good beat
    return time.time() - _last_beat[0] <= 15  # Resolve can pause scripts for a few seconds while it works


def bridge(op, timeout=15, on_status=None, **kw):
    """Send one command to the Resolve bridge and wait for its result."""
    if not bridge_alive():
        return {"ok": False, "error": "Resolve isn't connected. In Resolve: Workspace > Scripts > MixCaps."}
    with _cmd_lock:
        _cmd_id[0] += 1
        cid = _cmd_id[0]
        cmd_path = os.path.join(IPC, "cmd.lua")
        t0 = time.time()
        while os.path.exists(cmd_path) and time.time() - t0 < 5:
            time.sleep(0.05)
        res_path = os.path.join(IPC, f"result_{cid}.json")
        tmp = cmd_path + ".w"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("return " + to_lua(dict(kw, op=op, id=cid)) + "\n")
        os.replace(tmp, cmd_path)
    t0 = time.time()
    last_status = None
    while time.time() - t0 < timeout:
        if os.path.exists(res_path):
            time.sleep(0.02)
            try:
                res = json.load(open(res_path, encoding="utf-8"))
            except Exception:
                time.sleep(0.1)
                res = json.load(open(res_path, encoding="utf-8"))
            os.remove(res_path)
            return res
        if on_status:
            try:
                st = json.load(open(os.path.join(IPC, "status.json"), encoding="utf-8"))
                if st.get("id") == cid and st != last_status:
                    last_status = st
                    on_status(st.get("pct", 0), st.get("msg", ""))
            except Exception:
                pass
        if not bridge_alive():
            return {"ok": False, "error": "Lost connection to Resolve."}
        time.sleep(0.1)
    return {"ok": False, "error": f"Resolve didn't answer ({op})."}


# ------------------------------------------------------------------ state
STATE = {
    "phase": "idle", "pct": 0, "msg": "", "error": None,
    "captions": [], "offset": 0.0, "job": None, "info": None, "result": None,
    "last_poll": time.time(),
}
_state_lock = threading.Lock()


def set_state(**kw):
    with _state_lock:
        STATE.update(kw)


def busy():
    return STATE["phase"] in ("render", "transcribe", "insert")


def engine_cmd(args):
    py = sys.executable
    if py.lower().endswith("pythonw.exe"):
        py = py[:-len("pythonw.exe")] + "python.exe"
    return [py, os.path.join(HERE, "mixcaps_engine.py")] + args


def run_engine(audio, opts, job):
    tier = opts.get("tier") or ("best" if opts.get("best") else "balanced")
    if tier not in ("light", "balanced", "best"):
        tier = "balanced"
    lang = opts.get("lang") or "auto"
    if lang not in ("auto", "english", "telugish", "hinglish"):
        lang = "auto"
    low = bool(opts.get("low"))
    args = ["transcribe", "--audio", audio, "--lang", lang, "--out", job,
            "--words-per-caption", str(int(opts.get("wpc", 3))), "--snap", "1" if opts.get("snap", True) else "0",
            "--quality", tier, "--low-usage", "1" if low else "0"]
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    if low and os.name == "nt":
        flags |= 0x00004000  # BELOW_NORMAL_PRIORITY_CLASS: Resolve and games stay smooth
    with open(os.path.join(job, "engine_out.txt"), "w", encoding="utf-8") as out:
        p = subprocess.Popen(engine_cmd(args), stdout=out, stderr=subprocess.STDOUT, creationflags=flags)
        prog = os.path.join(job, "progress.txt")
        while p.poll() is None:
            try:
                with open(prog, encoding="utf-8") as f:
                    pct, stage, msg = f.read().split("|", 2)
                set_state(pct=int(pct), msg=f"{stage} {msg}".strip())
            except Exception:
                pass
            time.sleep(0.3)
    if p.returncode != 0:
        try:
            pct, stage, msg = open(os.path.join(job, "progress.txt"), encoding="utf-8").read().split("|", 2)
        except Exception:
            msg = "see " + os.path.join(job, "log.txt")
        raise RuntimeError("Speech engine failed: " + msg)
    return json.load(open(os.path.join(job, "captions.json"), encoding="utf-8"))


def build_audio(segments, out_path, marks=None):
    """Mix the timeline's audio clips into one 16 kHz mono WAV (what Resolve would render, minus effects)."""
    import wave
    import av
    import numpy as np
    sr = 16000
    total = max((sg["rec"] + sg["dur"] for sg in segments), default=0)
    buf = np.zeros(int(total * sr) + sr, dtype=np.float32)
    used = 0
    for sg in segments:
        try:
            with av.open(sg["path"]) as c:
                if not c.streams.audio:
                    continue
                st = c.streams.audio[0]
                # Resolve reports the clip's source start in TIMECODE terms when the file carries a start
                # timecode (e.g. 01:00:00:00 -> 3600 s). Convert to seconds from the start of the file.
                file_dur = (c.duration / 1e6) if c.duration else None
                tc = c.metadata.get("timecode") or next((x.metadata.get("timecode") for x in c.streams if x.metadata.get("timecode")), None)
                if tc and file_dur and sg["src"] >= file_dur - 0.05:
                    vfps = float(c.streams.video[0].average_rate) if c.streams.video else 30.0
                    hh, mm, ss, ff = [int(x) for x in re.split(r"[:;.]", tc)[:4]]
                    tc_sec = hh * 3600 + mm * 60 + ss + ff / max(vfps, 1)
                    log("clip has start timecode", tc, "-> shifting source by", tc_sec, "s")
                    sg = dict(sg, src=max(0.0, sg["src"] - tc_sec))
                rs = av.AudioResampler(format="flt", layout="mono", rate=sr)
                c.seek(int(max(sg["src"] - 1.0, 0) / st.time_base), stream=st)
                need = int(sg["dur"] * sr)
                got = []
                n = 0
                skip_to = sg["src"]
                for fr in c.decode(st):
                    t0 = float(fr.pts * st.time_base) if fr.pts is not None else None
                    for o in rs.resample(fr):
                        a = o.to_ndarray().reshape(-1)
                        if t0 is not None and t0 < skip_to:
                            cut = int((skip_to - t0) * sr)
                            a = a[cut:] if cut < len(a) else a[:0]
                        got.append(a)
                        n += len(a)
                    if n >= need:
                        break
                if got:
                    a = np.concatenate(got)[:need]
                    i = int(sg["rec"] * sr)
                    buf[i:i + len(a)] += a
                    used += 1
        except Exception as e:
            log("audio clip failed:", sg.get("path"), e)
    if used == 0 or float(np.abs(buf).max()) < 1e-4:
        raise RuntimeError("The timeline audio came out silent - check the clips have audio and aren't muted.")
    pcm = (np.clip(buf, -1, 1) * 32767).astype("<i2")
    with wave.open(out_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return used


def generate_worker(opts):
    job = os.path.join(JOBS, time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(job, exist_ok=True)
    set_state(phase="render", pct=0, msg="Asking Resolve for the timeline audio", error=None, job=job, captions=[])
    try:
        if opts.get("audio"):
            audio, offset = opts["audio"], 0.0
        else:
            audio, offset = None, 0.0
            amap = bridge("audiomap", timeout=30)
            if amap.get("ok") and amap.get("segments"):
                segs = amap["segments"]
                info = amap.get("info") or {}
                set_state(info=info, msg=f"Reading {len(segs)} audio clips from the timeline")
                if opts.get("marks") and info.get("markIn") is not None and info.get("markOut") is not None:
                    a, b = info["markIn"], info["markOut"]
                    if a >= info.get("start", 0):
                        a, b = a - info["start"], b - info["start"]
                    lo, hi = a / info["fps"], b / info["fps"]
                    clipped = []
                    for sg in segs:
                        s0, s1 = max(sg["rec"], lo), min(sg["rec"] + sg["dur"], hi)
                        if s1 > s0:
                            clipped.append(dict(sg, rec=s0 - lo, src=sg["src"] + (s0 - sg["rec"]), dur=s1 - s0))
                    segs, offset = clipped, lo
                try:
                    path = os.path.join(job, "mixcaps_audio.wav")
                    n = build_audio(segs, path)
                    log("built audio from", n, "clips")
                    audio = path
                except Exception as e:
                    log("building audio failed, falling back to a Resolve render:", e)
            elif amap.get("error") and "timeline" in amap.get("error", ""):
                raise RuntimeError(amap["error"])
            if audio is None:
                res = bridge("render", timeout=3600, dir=job, marks=bool(opts.get("marks")),
                             on_status=lambda p, m: set_state(pct=int(p or 0), msg=m))
                if not res.get("ok"):
                    raise RuntimeError(res.get("error", "render failed"))
                audio, offset = res["audio"], float(res.get("offset", 0))
                set_state(info=res.get("info"))
        set_state(phase="transcribe", pct=0, msg="Starting speech engine", offset=offset)
        caps = run_engine(audio, opts, job)
        result = {}
        try:
            result = json.load(open(os.path.join(job, "result.json"), encoding="utf-8"))
        except Exception:
            pass
        names = {"telugish": "Telugish", "hinglish": "Hinglish", "english": "English"}
        msg = f"{len(caps)} captions ready"
        if result.get("detected"):
            msg += f" · detected {names.get(result['detected'][0], result['detected'][0])}"
        if result.get("seconds"):
            msg += f" · {result['seconds']:.0f}s"
        if result.get("note"):
            msg += " · " + result["note"]
        set_state(phase="review", pct=100, msg=msg, captions=caps, result=result)
        try:
            json.dump({"offset": offset, "msg": msg}, open(os.path.join(job, "app_state.json"), "w", encoding="utf-8"))
        except Exception:
            pass
        log("generated", len(caps), "captions in", job)
    except Exception as e:
        log("generate failed:", e)
        set_state(phase="error", error=str(e), msg=str(e))
    finally:
        # the timeline audio Resolve rendered for us is only needed for transcription: delete it
        # (files the user picked with "From file..." live elsewhere and are never touched)
        for name in os.listdir(job):
            if name.startswith("mixcaps_audio."):
                try:
                    os.remove(os.path.join(job, name))
                except OSError as e:
                    log("could not delete", name, e)


def ensure_carrier(fps, width=1920, height=1080, seconds=20):
    """A transparent ProRes 4444 clip at the timeline's exact size and frame rate. Each caption's Text+ sits on one;
    it must match the timeline size or Resolve squeezes the Fusion picture into the clip's own (smaller) frame.
    ProRes is intra-only, so one encoded empty frame is reused for every frame: instant, whatever the size."""
    import fractions
    import av
    import numpy as np
    fps = float(fps or 30)
    width, height = int(width or 1920), int(height or 1080)
    rate = fractions.Fraction(fps).limit_denominator(1001)
    folder = os.path.join(APP, "carriers")
    os.makedirs(folder, exist_ok=True)
    name = f"mixcaps_carrier_{width}x{height}_{str(round(fps, 3)).replace('.', '_')}.mov"
    path = os.path.join(folder, name)
    if os.path.exists(path):
        return path
    tmp = path + ".part.mov"
    c = av.open(tmp, "w")
    st = c.add_stream("prores_ks", rate=rate)
    st.width, st.height = width, height
    st.pix_fmt = "yuva444p10le"
    st.options = {"profile": "4444", "qscale": "31"}
    frame = av.VideoFrame.from_ndarray(np.zeros((height, width, 4), np.uint8), format="rgba").reformat(format="yuva444p10le")
    st.codec_context.time_base = 1 / rate
    frame.pts = 0
    frame.time_base = 1 / rate
    first = (list(st.encode(frame)) + list(st.encode()))[0]
    data, tb = bytes(first), first.time_base or (1 / rate)
    step = first.duration or int(round((1 / rate) / tb))
    for i in range(int(round(fps * seconds))):
        pk = av.Packet(data)
        pk.stream = st
        pk.time_base = tb
        pk.pts = pk.dts = i * step
        pk.duration = step
        pk.is_keyframe = True
        c.mux(pk)
    c.close()
    os.replace(tmp, path)
    log("made carrier", path, os.path.getsize(path))
    return path


def insert_worker(caps, style, upper):
    set_state(phase="insert", pct=0, msg="Sending captions to Resolve", error=None)
    try:
        info = bridge("info", timeout=10)
        if not info.get("ok"):
            raise RuntimeError(info.get("error", "Resolve isn't ready"))


        out = []
        for c in caps:
            if not c["t"].strip():
                continue
            o = dict(s=c["s"], e=c["e"], t=c["t"])
            for k in ("li", "slot", "block"):  # stacked presets: line look, track slot, block id
                if isinstance(c.get(k), (int, float)) and not isinstance(c.get(k), bool):
                    o[k] = int(c[k])
            if isinstance(c.get("be"), (int, float)):
                o["be"] = float(c["be"])  # block end: the line stays until its block clears
            out.append(o)
        res = bridge("insert", timeout=3600, captions=out, offset=STATE["offset"], style=style, upper=upper,
                     on_status=lambda p, m: set_state(pct=int(p or 0), msg=m))
        if not res.get("ok"):
            raise RuntimeError(res.get("error", "insert failed"))
        msg = f"Added {res.get('placed')} captions on track V{res.get('track')}"
        if res.get("failed"):
            msg += f" ({res['failed']} failed - see resolve_log.txt)"
        set_state(phase="review", pct=100, msg=msg)
    except Exception as e:
        log("insert failed:", e)
        set_state(phase="review", error=str(e), msg=str(e))


SYSTEM = {"info": None}


def load_system():
    try:
        flags = 0x08000000 if os.name == "nt" else 0
        out = subprocess.run(engine_cmd(["check", "--json"]), capture_output=True, text=True, timeout=60, creationflags=flags).stdout
        SYSTEM["info"] = json.loads(out.strip().splitlines()[-1])
    except Exception as e:
        log("system check failed:", e)
        SYSTEM["info"] = {"error": str(e)}


def srt_text(caps, offset):
    def t(x):
        ms = int(round(x * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
    return "\n".join(f"{i}\n{t(c['s'] + offset)} --> {t(c['e'] + offset)}\n{c['t']}\n" for i, c in enumerate(caps, 1))


# ------------------------------------------------------------------ presets + fonts
BUILTIN_PRESETS = os.path.join(HERE, "presets_builtin.json")
USER_PRESETS = os.path.join(APP, "presets.json")
FONTS = {"list": None}


def load_fonts():
    try:
        import fontlist
        FONTS["list"] = fontlist.list_fonts()
    except Exception as e:
        log("font scan failed:", e)
        FONTS["list"] = {}


def _num(v, lo, hi, default):
    try:
        return min(hi, max(lo, float(v)))
    except (TypeError, ValueError):
        return default


def _rgb(v, default):
    try:
        return [_num(v[i], 0, 1, default[i]) for i in range(3)]
    except (TypeError, IndexError, KeyError):
        return list(default)


def cap_in(c):
    """One caption from the page: time, text and the optional emphasis override (em: true / false)."""
    o = dict(s=float(c["s"]), e=float(c["e"]), t=str(c["t"]))
    if isinstance(c.get("em"), bool):
        o["em"] = c["em"]
    return o


ANIMS = ("pop", "fade", "slide", "blur", "type", "none")


def clean_preset(p):
    """Keep only known fields with sane values, so a hand-edited or shared file can't break Resolve."""
    out = {"name": str(p.get("name") or "Untitled").strip()[:40] or "Untitled",
           "upper": bool(p.get("upper", False)),
           "scale": _num(p.get("scale"), 0.3, 3, 1)}
    if p.get("type") == "stack":
        def sub(x, default_scale):
            lk = clean_look(x if isinstance(x, dict) else {})
            if not isinstance(x, dict) or "scale" not in x:
                lk["scale"] = default_scale
            return lk
        acc = [sub(x, 0.75) for x in (p.get("accents") or []) if isinstance(x, dict)][:6] or [sub({}, 0.75)]
        out.update({"type": "stack", "maxLines": int(_num(p.get("maxLines"), 1, 4, 4)),
                    "emph": sub(p.get("emph"), 1.5), "accents": acc, "filler": sub(p.get("filler"), 0.5)})
        return out
    out.update(clean_look(p))
    return out


def clean_look(p):
    out = {"scale": _num(p.get("scale"), 0.3, 3, 1),
           "font": str(p.get("font") or "Poppins")[:80],
           "fontStyle": str(p.get("fontStyle") or "Bold")[:40],
           "color": _rgb(p.get("color"), [1, 1, 1]),
           "tracking": _num(p.get("tracking"), 0.5, 2, 1),
           "anim": p.get("anim") if p.get("anim") in ANIMS else "pop"}
    fb = p.get("fallback")
    if isinstance(fb, dict) and fb.get("font"):
        out["fallback"] = {"font": str(fb["font"])[:80], "fontStyle": str(fb.get("fontStyle") or "Regular")[:40]}
    if p.get("anim") == "blur":
        out["blurIn"] = _num(p.get("blurIn"), 1, 15, 6)
    o = p.get("outline")
    if isinstance(o, dict) and _num(o.get("width"), 0, 1, 0) > 0:
        out["outline"] = {"color": _rgb(o.get("color"), [0, 0, 0]), "width": _num(o.get("width"), 0, 1, 0.1)}
    s = p.get("shadow")
    if isinstance(s, dict):
        out["shadow"] = {"color": _rgb(s.get("color"), [0, 0, 0]), "x": _num(s.get("x"), -0.5, 0.5, 0.04),
                         "y": _num(s.get("y"), -0.5, 0.5, -0.06), "blur": _num(s.get("blur"), 0, 3, 0),
                         "opacity": _num(s.get("opacity"), 0, 1, 1)}
    b = p.get("box")
    if isinstance(b, dict):
        out["box"] = {"color": _rgb(b.get("color"), [0, 0, 0]), "opacity": _num(b.get("opacity"), 0, 1, 0.75),
                      "padX": _num(b.get("padX"), 0, 2, 0.3), "padY": _num(b.get("padY"), 0, 2, 0.15),
                      "round": _num(b.get("round"), 0, 1, 0.3)}
    return out


def read_presets(path):
    try:
        data = json.load(open(path, encoding="utf-8-sig"))
    except FileNotFoundError:
        return []
    except Exception as e:
        log("bad presets file", path, e)
        return []
    if isinstance(data, dict):
        data = data.get("presets", [])
    return [clean_preset(p) for p in data if isinstance(p, dict)]


def write_user_presets(presets):
    tmp = USER_PRESETS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"mixcaps_presets": 1, "presets": presets}, f, indent=1, ensure_ascii=False)
    os.replace(tmp, USER_PRESETS)


def _tk_dialog(kind, **kw):
    try:
        import tkinter
        from tkinter import filedialog
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        fn = filedialog.asksaveasfilename if kind == "save" else filedialog.askopenfilename
        p = fn(**kw)
        root.destroy()
        return p or None
    except Exception as e:
        log("dialog failed", e)
        return None


PRESET_TYPES = [("MixCaps presets", "*.mixcaps.json *.json"), ("All", "*.*")]


def pick_file():
    try:
        import tkinter
        from tkinter import filedialog
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        p = filedialog.askopenfilename(title="Audio or video for captions",
                                       filetypes=[("Audio/video", "*.wav *.mp3 *.m4a *.aac *.flac *.mp4 *.mov *.mkv *.webm"), ("All", "*.*")])
        root.destroy()
        return p or None
    except Exception as e:
        log("file picker failed", e)
        return None


# ------------------------------------------------------------------ http
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json", extra=None):
        data = body if isinstance(body, bytes) else (json.dumps(body) if ctype == "application/json" else body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if "text" in ctype or "json" in ctype else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(200, open(os.path.join(HERE, "ui", "index.html"), "rb").read(), "text/html")
        if self.path in ("/logo.svg", "/favicon.ico"):
            return self._send(200, open(os.path.join(HERE, "ui", "logo.svg"), "rb").read(), "image/svg+xml")
        if self.path.startswith("/api/state"):
            STATE["last_poll"] = time.time()
            with _state_lock:
                s = {k: v for k, v in STATE.items() if k != "last_poll"}
            s["bridge"] = bridge_alive()
            return self._send(200, s)
        if self.path.startswith("/api/srt"):
            body = srt_text(STATE["captions"], STATE["offset"])
            return self._send(200, body, "application/x-subrip", {"Content-Disposition": 'attachment; filename="captions.srt"'})
        if self.path.startswith("/api/system"):
            if SYSTEM["info"] is None:
                return self._send(200, {"loading": True})
            return self._send(200, SYSTEM["info"])
        if self.path.startswith("/api/presets"):
            return self._send(200, {"builtin": read_presets(BUILTIN_PRESETS), "user": read_presets(USER_PRESETS)})
        if self.path.startswith("/api/fonts"):
            if FONTS["list"] is None:
                return self._send(200, {"loading": True})
            return self._send(200, {"fonts": FONTS["list"]})
        if self.path.startswith("/api/ping"):
            return self._send(200, {"ok": True, "app": "mixcaps"})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.headers.get("Origin") not in (None, f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            return self._send(403, {"error": "bad origin"})
        b = self._body()
        p = self.path
        if p == "/api/generate":
            if busy():
                return self._send(409, {"error": "busy"})
            if b.get("useFile"):
                f = pick_file()
                if not f:
                    return self._send(200, {"ok": False, "error": "No file chosen."})
                b["audio"] = f
            threading.Thread(target=generate_worker, args=(b,), daemon=True).start()
            return self._send(200, {"ok": True})
        if p == "/api/captions":
            caps = [cap_in(c) for c in b.get("captions", [])]
            set_state(captions=caps)
            if STATE["job"]:
                json.dump(caps, open(os.path.join(STATE["job"], "captions_edited.json"), "w", encoding="utf-8"), ensure_ascii=False)
            return self._send(200, {"ok": True})
        if p == "/api/jump":
            res = bridge("jump", timeout=5, seconds=float(b.get("s", 0)) + STATE["offset"])
            return self._send(200, res)
        if p == "/api/insert":
            if busy():
                return self._send(409, {"error": "busy"})
            caps = b.get("captions") or STATE["captions"]
            set_state(captions=[cap_in(c) for c in caps])
            # layout fields (li/slot/block/be) come with this request only; they're not stored
            layout = [dict(cap_in(c), **{k: c[k] for k in ("li", "slot", "block", "be") if k in c}) for c in caps]
            threading.Thread(target=insert_worker, args=(layout, b.get("style") or {}, bool(b.get("upper", True))), daemon=True).start()
            return self._send(200, {"ok": True})
        if p == "/api/refit":
            if busy():
                return self._send(409, {"error": "busy"})

            restyle = b.get("style")
            extra = {"style": restyle, "upper": bool(b.get("upper"))} if restyle else {}

            def refit_worker():
                set_state(phase="insert", pct=0, msg="Restyling captions" if restyle else "Fitting captions to this timeline", error=None)
                res = bridge("refit", timeout=1800, on_status=lambda pc, m: set_state(pct=int(pc or 0), msg=m), **extra)
                back = "review" if STATE["captions"] else "idle"
                if res.get("ok") and restyle:
                    set_state(phase=back, pct=100, msg=f"Applied the look to {res.get('refit')} captions")
                elif res.get("ok"):
                    set_state(phase=back, pct=100, msg=f"Fitted {res.get('refit')} captions to {res.get('width')}x{res.get('height')}")
                else:
                    set_state(phase=back, error=res.get("error"), msg=res.get("error"))
            threading.Thread(target=refit_worker, daemon=True).start()
            return self._send(200, {"ok": True})
        if p == "/api/remember":
            a, r = str(b.get("a", "")).strip(), str(b.get("b", "")).strip()
            if a and r:
                with open(os.path.join(APP, "replacements.txt"), "a", encoding="utf-8") as f:
                    f.write(f"{a} => {r}\n")
            return self._send(200, {"ok": True})
        if p == "/api/presets/save":
            pr = clean_preset(b.get("preset") or {})
            if pr["name"].lower() in (x["name"].lower() for x in read_presets(BUILTIN_PRESETS)):
                return self._send(200, {"ok": False, "error": f'"{pr["name"]}" is a built-in preset. Pick another name.'})
            user = [x for x in read_presets(USER_PRESETS) if x["name"].lower() != pr["name"].lower()]
            user.append(pr)
            write_user_presets(user)
            return self._send(200, {"ok": True, "preset": pr})
        if p == "/api/presets/delete":
            name = str(b.get("name", "")).lower()
            write_user_presets([x for x in read_presets(USER_PRESETS) if x["name"].lower() != name])
            return self._send(200, {"ok": True})
        if p == "/api/presets/export":
            user = read_presets(USER_PRESETS)
            if b.get("name"):
                user = [x for x in user if x["name"].lower() == str(b["name"]).lower()]
            if not user:
                return self._send(200, {"ok": False, "error": "You have no saved presets to export yet."})
            fn = (user[0]["name"] if len(user) == 1 else "my-presets").replace(" ", "-").lower()
            path = _tk_dialog("save", title="Export MixCaps presets", initialfile=fn + ".mixcaps.json",
                              defaultextension=".json", filetypes=PRESET_TYPES)
            if not path:
                return self._send(200, {"ok": False, "error": "Export cancelled."})
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"mixcaps_presets": 1, "presets": user}, f, indent=1, ensure_ascii=False)
            return self._send(200, {"ok": True, "count": len(user), "path": path})
        if p == "/api/presets/import":
            path = _tk_dialog("open", title="Import MixCaps presets", filetypes=PRESET_TYPES)
            if not path:
                return self._send(200, {"ok": False, "error": "Import cancelled."})
            new = read_presets(path)
            if not new:
                return self._send(200, {"ok": False, "error": "No presets found in that file."})
            builtin = {x["name"].lower() for x in read_presets(BUILTIN_PRESETS)}
            user = read_presets(USER_PRESETS)
            for pr in new:
                if pr["name"].lower() in builtin:
                    pr["name"] = (pr["name"] + " (imported)")[:40]
                user = [x for x in user if x["name"].lower() != pr["name"].lower()] + [pr]
            write_user_presets(user)
            return self._send(200, {"ok": True, "count": len(new)})
        if p == "/api/presets/folder":
            if not os.path.exists(USER_PRESETS):
                write_user_presets([])
            subprocess.Popen(["explorer", "/select,", USER_PRESETS])
            return self._send(200, {"ok": True})
        if p == "/api/info":
            return self._send(200, bridge("info", timeout=5))
        if p == "/api/quit":
            self._send(200, {"ok": True})
            threading.Thread(target=lambda: (time.sleep(0.3), os._exit(0)), daemon=True).start()
            return
        self._send(404, {"error": "not found"})


def other_instance():
    try:
        port = int(open(PORT_FILE).read().strip())
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1) as r:
            if json.load(r).get("app") == "mixcaps":
                return port
    except Exception:
        pass
    return None


def heartbeat():
    while True:
        try:
            with open(os.path.join(IPC, "app.txt"), "w") as f:
                f.write(str(int(time.time())))
        except OSError:
            pass
        # quit when the page has been closed for 3 minutes and nothing is running
        if time.time() - STATE["last_poll"] > 180 and not busy():
            log("page closed; exiting")
            os._exit(0)
        time.sleep(3)


PANEL_W = 520


def work_area():
    """Screen area without the taskbar: (left, top, right, bottom)."""
    try:
        import ctypes
        from ctypes import wintypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(0)  # report in the same units Chromium uses
        except Exception:
            pass
        r = wintypes.RECT()
        ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)
        return r.left, r.top, r.right, r.bottom
    except Exception:
        return 0, 0, 1920, 1040


def find_app_browser():
    pf = [os.environ.get(k) for k in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
    names = [r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe",
             r"BraveSoftware\Brave-Browser\Application\brave.exe"]
    for n in names:
        for root in pf:
            if root and os.path.exists(os.path.join(root, n)):
                return os.path.join(root, n)
    return None


def open_window(url):
    """Open MixCaps as its own app window (no tabs / address bar), docked at the right of the screen."""
    exe = find_app_browser()
    if not exe:
        log("no Edge/Chrome found; opening default browser")
        webbrowser.open(url)
        return
    l, t, r, b = work_area()
    w, h = PANEL_W, b - t
    args = [exe, f"--app={url}", f"--window-size={w},{h}", f"--window-position={r - w},{t}",
            "--user-data-dir=" + os.path.join(APP, "window"), "--no-first-run", "--no-default-browser-check",
            "--disable-features=Translate,msEdgeSidebarV2,msUndersideButton", "--disable-extensions"]
    try:
        subprocess.Popen(args, creationflags=0x08000000 if os.name == "nt" else 0)
        log("opened app window with", exe)
    except Exception as e:
        log("app window failed:", e)
        webbrowser.open(url)


PREFERRED_PORT = 47821  # fixed so the window remembers its settings between runs


def restore_last_job(max_age_h=24):
    """Bring back the last caption list after the app restarts (edits included)."""
    try:
        jobs = sorted((d for d in os.listdir(JOBS) if os.path.isdir(os.path.join(JOBS, d))), reverse=True)
        for d in jobs:
            job = os.path.join(JOBS, d)
            if time.time() - os.path.getmtime(job) > max_age_h * 3600:
                return
            for name in ("captions_edited.json", "captions.json"):
                p = os.path.join(job, name)
                if os.path.exists(p):
                    caps = json.load(open(p, encoding="utf-8"))
                    meta = {}
                    if os.path.exists(os.path.join(job, "app_state.json")):
                        meta = json.load(open(os.path.join(job, "app_state.json"), encoding="utf-8"))
                    set_state(phase="review", pct=100, captions=caps, job=job, offset=float(meta.get("offset", 0)),
                              msg=f"{len(caps)} captions from your last run")
                    log("restored", len(caps), "captions from", job)
                    return
    except Exception as e:
        log("restore failed:", e)


def main():
    global PORT
    port = other_instance()
    if port:
        open_window(f"http://127.0.0.1:{port}/")
        return
    srv = None
    for p in (PREFERRED_PORT, 0):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), H)
            break
        except OSError:
            continue
    PORT = srv.server_address[1]
    open(PORT_FILE, "w").write(str(PORT))
    log("app started on port", PORT)
    threading.Thread(target=heartbeat, daemon=True).start()
    threading.Thread(target=load_system, daemon=True).start()
    threading.Thread(target=load_fonts, daemon=True).start()
    restore_last_job()
    open_window(f"http://127.0.0.1:{PORT}/")
    srv.serve_forever()


PORT = 0
if __name__ == "__main__":
    main()
