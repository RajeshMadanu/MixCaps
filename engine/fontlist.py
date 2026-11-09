"""List installed fonts as {family: [styles]} the way Fusion's Text+ names them.

No dependencies: reads the 'name' table of each .ttf/.otf/.ttc directly. Typographic names
(IDs 16/17) win over the legacy ones (1/2), which is what Text+ shows in its Font/Style menus.
"""
import os
import struct

FONT_DIRS = [
    os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts"),
]


def _names(data, off):
    num = struct.unpack_from(">H", data, off + 4)[0]
    table = None
    for i in range(num):
        tag, _, toff, _ = struct.unpack_from(">4sIII", data, off + 12 + 16 * i)
        if tag == b"name":
            table = toff
            break
    if table is None:
        return None
    _, count, strs = struct.unpack_from(">HHH", data, table)
    best = {}
    for i in range(count):
        pid, eid, lang, nid, ln, so = struct.unpack_from(">HHHHHH", data, table + 6 + 12 * i)
        if nid not in (1, 2, 16, 17):
            continue
        raw = data[table + strs + so: table + strs + so + ln]
        if pid == 3 and eid in (0, 1, 10):
            score = 3 if lang == 0x409 else 2
            try:
                txt = raw.decode("utf-16-be")
            except UnicodeDecodeError:
                continue
        elif pid == 1 and eid == 0:
            score = 1
            txt = raw.decode("mac_roman", "replace")
        else:
            continue
        if score > best.get(nid, (0, ""))[0]:
            best[nid] = (score, txt.strip())
    fam = (best.get(16) or best.get(1) or (0, ""))[1]
    sty = (best.get(17) or best.get(2) or (0, "Regular"))[1]
    return (fam, sty) if fam else None


def read_font(path):
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return []
    if data[:4] == b"ttcf":
        n = struct.unpack_from(">I", data, 8)[0]
        offs = struct.unpack_from(">" + "I" * n, data, 12)
    else:
        offs = (0,)
    out = []
    for o in offs:
        try:
            r = _names(data, o)
        except struct.error:
            r = None
        if r:
            out.append(r)
    return out


STYLE_ORDER = ["thin", "extralight", "light", "regular", "medium", "semibold", "bold", "extrabold", "black"]


def _style_key(s):
    k = s.lower().replace(" ", "").replace("-", "")
    base = k.replace("italic", "").replace("oblique", "") or "regular"
    rank = STYLE_ORDER.index(base) if base in STYLE_ORDER else 4
    return (rank, "italic" in k or "oblique" in k, s)


def list_fonts():
    fams = {}
    for d in FONT_DIRS:
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if name.lower().endswith((".ttf", ".otf", ".ttc")):
                for fam, sty in read_font(os.path.join(d, name)):
                    if fam.startswith(("@", ".")):
                        continue
                    fams.setdefault(fam, set()).add(sty)
    return {f: sorted(s, key=_style_key) for f, s in sorted(fams.items(), key=lambda x: x[0].lower())}


if __name__ == "__main__":
    fonts = list_fonts()
    print(len(fonts), "families")
    for f in ("Poppins", "Segoe UI", "Impact", "Consolas", "Arial"):
        print(f, fonts.get(f))
