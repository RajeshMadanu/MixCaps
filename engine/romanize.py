"""Casual romanization of Telugu and Devanagari, the way people type Telugish / Hinglish.

Telugu:  ప్రతి గ్రూప్ లో -> prathi grup lo      నా దగ్గర -> naa daggara
Hindi:   भाई ये क्या हो गया -> bhai ye kya ho gaya   करना -> karna

Not ISO transliteration on purpose: long vowels collapse (chala, not chaalaa), త is "th",
Hindi drops the inherent schwa where speakers drop it.
"""
import re
import unicodedata

# ---------------------------------------------------------------- Telugu
TE_CONS = {
    "క": "k", "ఖ": "kh", "గ": "g", "ఘ": "gh", "ఙ": "n",
    "చ": "ch", "ఛ": "chh", "జ": "j", "ఝ": "jh", "ఞ": "n",
    "ట": "t", "ఠ": "th", "డ": "d", "ఢ": "dh", "ణ": "n",
    "త": "th", "థ": "th", "ద": "d", "ధ": "dh", "న": "n",
    "ప": "p", "ఫ": "ph", "బ": "b", "భ": "bh", "మ": "m",
    "య": "y", "ర": "r", "ఱ": "r", "ల": "l", "ళ": "l", "ఴ": "l", "వ": "v",
    "శ": "sh", "ష": "sh", "స": "s", "హ": "h", "ౘ": "ts", "ౙ": "dz",
}
# value: (short spelling, spelling in one-syllable words / word-initial independent vowel)
TE_VOWEL = {
    "అ": ("a", "a"), "ఆ": ("a", "aa"), "ఇ": ("i", "i"), "ఈ": ("i", "ee"),
    "ఉ": ("u", "u"), "ఊ": ("u", "oo"), "ఋ": ("ru", "ru"), "ౠ": ("ru", "ru"),
    "ఎ": ("e", "e"), "ఏ": ("e", "e"), "ఐ": ("ai", "ai"), "ఒ": ("o", "o"),
    "ఓ": ("o", "o"), "ఔ": ("au", "au"),
}
TE_SIGN = {
    "ా": ("a", "aa"), "ి": ("i", "i"), "ీ": ("i", "ee"), "ు": ("u", "u"),
    "ూ": ("u", "oo"), "ృ": ("ru", "ru"), "ౄ": ("ru", "ru"), "ె": ("e", "e"),
    "ే": ("e", "e"), "ై": ("ai", "ai"), "ొ": ("o", "o"), "ో": ("o", "o"),
    "ౌ": ("au", "au"), "ౕ": ("", ""), "ౖ": ("", ""),
}
TE_VIRAMA, TE_ANUSVARA, TE_VISARGA, TE_CANDRA = "్", "ం", "ః", "ఁ"
TE_ARDHA = "ఀ"
# geminates written casually: త్త -> tt (kotta), చ్చ -> ch (vachindi)
GEMINATE = {"th": "tt", "ch": "ch", "chh": "chh", "sh": "sh", "kh": "kkh", "gh": "ggh",
            "dh": "ddh", "bh": "bbh", "ph": "pph", "jh": "jjh", "ts": "ts", "dz": "dz"}
LABIAL = ("p", "b", "m", "ph", "bh")

# ---------------------------------------------------------------- Devanagari
HI_CONS = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n",
    "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "n",
    "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n",
    "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n",
    "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m",
    "य": "y", "र": "r", "ल": "l", "ळ": "l", "व": "v",
    "श": "sh", "ष": "sh", "स": "s", "ह": "h",
    "क़": "q", "ख़": "kh", "ग़": "gh", "ज़": "z", "ड़": "d", "ढ़": "rh", "फ़": "f", "य़": "y", "ऱ": "r", "ऩ": "n",
}
HI_NUKTA_MAP = {"क": "q", "ख": "kh", "ग": "gh", "ज": "z", "ड": "d", "ढ": "rh", "फ": "f"}
HI_VOWEL = {
    "अ": ("a", "a"), "आ": ("a", "aa"), "इ": ("i", "i"), "ई": ("i", "ee"),
    "उ": ("u", "u"), "ऊ": ("u", "oo"), "ऋ": ("ri", "ri"), "ए": ("e", "e"),
    "ऐ": ("ai", "ai"), "ओ": ("o", "o"), "औ": ("au", "au"), "ऑ": ("o", "o"),
    "ऍ": ("e", "e"), "ऎ": ("e", "e"), "ऒ": ("o", "o"),
}
HI_SIGN = {
    "ा": ("a", "a"), "ि": ("i", "i"), "ी": ("i", "i"), "ु": ("u", "u"),
    "ू": ("u", "u"), "ृ": ("ri", "ri"), "े": ("e", "e"), "ै": ("ai", "ai"),
    "ो": ("o", "o"), "ौ": ("au", "au"), "ॉ": ("o", "o"), "ॅ": ("e", "e"),
    "ॆ": ("e", "e"), "ॊ": ("o", "o"),
}
HI_VIRAMA, HI_ANUSVARA, HI_VISARGA, HI_CANDRA, HI_NUKTA = "्", "ं", "ः", "ँ", "़"

DIGITS = {chr(0x0C66 + i): str(i) for i in range(10)}
DIGITS.update({chr(0x0966 + i): str(i) for i in range(10)})


def _syllables(word, cons, vowels, signs, virama):
    """Split one word into a list of units: (consonant_roman or None, vowel_key, extra)
    vowel_key: 'inh' (inherent a), 'none' (virama / dead), or the sign / vowel char.
    extra: 'anusvara', 'visarga', 'candra' attached after the unit."""
    units = []
    i, n = 0, len(word)
    while i < n:
        ch = word[i]
        if ch in cons:
            c = cons[ch]
            j = i + 1
            if j < n and word[j] == HI_NUKTA:
                c = HI_NUKTA_MAP.get(ch, c)
                j += 1
            v = "inh"
            if j < n and word[j] in signs:
                v = word[j]; j += 1
            elif j < n and word[j] == virama:
                v = "none"; j += 1
            units.append([c, v, None])
            i = j
        elif ch in vowels:
            units.append([None, ch, None])
            i += 1
        elif ch in (TE_ANUSVARA, HI_ANUSVARA, TE_CANDRA, HI_CANDRA, TE_ARDHA, TE_VISARGA, HI_VISARGA):
            kind = "visarga" if ch in (TE_VISARGA, HI_VISARGA) else "anusvara"
            if units:
                units[-1][2] = kind
            i += 1
        else:
            units.append([ch, "raw", None])
            i += 1
    return units


def _anusvara(next_cons, at_end):
    if at_end or next_cons is None:
        return "m"
    return "m" if next_cons in LABIAL else "n"


def _render(units, vowels, signs, lang):
    nvow = sum(1 for u in units if u[1] not in ("none", "raw"))
    one_syl = nvow <= 1
    # Hindi schwa deletion (right to left): final inherent a dropped; medial V C [a] C V dropped
    if lang == "hi":
        alive = [u[1] not in ("none", "raw") for u in units]
        k = len(units) - 1
        if k >= 1 and units[k][1] == "inh" and units[k][2] is None and units[k - 1][1] != "none":
            units[k][1] = "none"; alive[k] = False
        for k in range(len(units) - 2, 0, -1):
            u = units[k]
            if u[1] != "inh" or u[2] is not None or u[0] is None:
                continue
            prev_v = alive[k - 1] and units[k - 1][1] != "none"
            nxt = units[k + 1]
            next_cv = nxt[0] is not None and nxt[1] not in ("none", "raw")
            closed_prev = units[k - 1][2] is not None or (k >= 2 and units[k - 2][1] == "none")
            if prev_v and next_cv and not closed_prev:
                u[1] = "none"; alive[k] = False
    out = []
    for idx, (c, v, extra) in enumerate(units):
        if v == "raw":
            out.append(DIGITS.get(c, c))
            continue
        prev = units[idx - 1] if idx else None
        cr = c or ""
        # geminate: previous unit is the same consonant with virama
        if c is not None and prev is not None and prev[1] == "none" and prev[0] == c and out:
            out.pop()
            cr = GEMINATE.get(c, c * 2)
        if c is None:
            long_ok = one_syl or (idx == 0 and v not in ("ఆ", "आ"))
            vr = vowels[v][1] if long_ok else vowels[v][0]
        elif v == "inh":
            vr = "a"
        elif v == "none":
            vr = ""
        else:
            vr = signs[v][1] if one_syl else signs[v][0]
        piece = cr + vr
        if extra == "anusvara":
            nxt = units[idx + 1][0] if idx + 1 < len(units) else None
            if lang == "hi" and idx + 1 >= len(units):
                piece += "n"
            else:
                piece += _anusvara(nxt, idx + 1 >= len(units))
        elif extra == "visarga":
            piece += "h"
        out.append(piece)
    s = "".join(out)
    if lang == "te":
        s = re.sub(r"a+yi$", "ai", s)
    else:
        if units and units[0][0] == "v" and units[0][1] in ("inh", "ा", "ो", "े", "ै"):
            s = "w" + s[1:]
        s = re.sub(r"ahin$", "ahi", s)
    return s


def romanize_word(word, lang):
    word = unicodedata.normalize("NFC", word).replace("‌", "").replace("‍", "")
    if lang == "te":
        units = _syllables(word, TE_CONS, TE_VOWEL, TE_SIGN, TE_VIRAMA)
        return _render(units, TE_VOWEL, TE_SIGN, "te")
    units = _syllables(word, HI_CONS, HI_VOWEL, HI_SIGN, HI_VIRAMA)
    return _render(units, HI_VOWEL, HI_SIGN, "hi")


_INDIC = re.compile(r"[ऀ-ॿఀ-౿‌‍]+")


def romanize(text, lang):
    """Romanize every Telugu/Devanagari run in text, leave Latin text alone."""
    return _INDIC.sub(lambda m: romanize_word(m.group(0), lang), text).replace("।", ".").replace("॥", ".")


def has_indic(text):
    return bool(_INDIC.search(text))


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    tests_te = "ఏం స్కోప్ రా అయ్యా ఇది ఎలా ఉంది నిజంగా బ్రో ప్రతి గ్రూప్ లో ఒక అబ్బాయి నా దగ్గర ఓపిక లేదు నేను చావకుండా ఆడాలి కరెంట్ పోయింది చాలా కొత్త వచ్చింది ఈ గేమ్ ఊరు అయిపోయిందా ఏమైంది వాట్ హాపెండ్ ఫిఫ్టీన్ కిల్స్ రియాక్షన్ టైమ్ మెసేజ్ నువ్వు ఎక్కడ పండబెట్టి కొడుతుండు అమ్మ"
    tests_hi = "भाई ये क्या हो गया करना समझना लड़की बहुत चलता हमने सबको नहीं मैं हूँ अब मेरा वाला ज़िंदगी फ़ोन गेम खेलना"
    print(romanize(tests_te, "te"))
    print(romanize(tests_hi, "hi"))
