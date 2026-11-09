"""Recover English spellings of English words that an Indic-language model wrote in Telugu/Devanagari.

Two sources of evidence:
  1. an English-language Whisper pass over the same audio ("hint" words with times): if the hint
     heard an English word at the same moment and it sounds the same, use its spelling;
  2. a static lexicon (gaming / everyday English + the user's own word list), with stricter rules.
"""
import difflib
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))

NUM = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
       "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def num_words(n):
    if n < 20:
        return NUM[n]
    if n < 100:
        return TENS[n // 10] + ("" if n % 10 == 0 else NUM[n % 10])
    if n < 1000:
        return NUM[n // 100] + "hundred" + ("" if n % 100 == 0 else num_words(n % 100))
    return str(n)


def _skeleton(s):
    s = re.sub(r"[aeiouy]", "", s)
    return re.sub(r"(.)\1+", r"\1", s)


def indic_key(roman):
    s = re.sub(r"[^a-z]", "", roman.lower())
    for a, b in (("chh", "C"), ("ch", "C"), ("sh", "S"), ("th", "t"), ("dh", "d"), ("kh", "k"), ("gh", "g"),
                 ("bh", "b"), ("ph", "f"), ("jh", "j"), ("w", "v"), ("z", "j"), ("q", "k"), ("x", "ks")):
        s = s.replace(a, b)
    return _skeleton(s)


def eng_key(word):
    s = word.lower()
    if s.isdigit():
        s = num_words(int(s))
    s = re.sub(r"[^a-z]", "", s)
    for a, b in (("charact", "karakt"), ("chor", "kor"), ("chem", "kem"), ("tech", "tek"), ("sch", "sk"), ("mech", "mek"), ("chaos", "kaos")):
        s = s.replace(a, b)
    rules = [(r"[ts]ion", "Sn"), (r"tch", "C"), (r"ght", "t"), (r"^kn", "n"), (r"^wr", "r"), (r"^ps", "s"), (r"ph", "f"),
             (r"wh", "v"), (r"ck", "k"), (r"sh", "S"), (r"ch", "C"), (r"th", "t"), (r"dge", "j"), (r"ge$", "j"),
             (r"ges$", "js"), (r"gh", ""), (r"c(?=[eiy])", "s"), (r"c", "k"), (r"q", "k"), (r"x", "ks"),
             (r"w", "v"), (r"z", "j"), (r"mb$", "m"), (r"(?<=[aeiou])r(?=[^aeiou]|$)", "r")]
    for a, b in rules:
        s = re.sub(a, b, s)
    return _skeleton(s)


def _norm(s):
    s = s.lower()
    for a, b in (("ee", "i"), ("oo", "u"), ("aa", "a"), ("ph", "f"), ("th", "t"), ("w", "v"), ("z", "j"), ("sh", "s"), ("ch", "c")):
        s = s.replace(a, b)
    s = re.sub(r"[^a-z]", "", s)
    s = re.sub(r"(?<=[aeiou])y(?=[aeiou])", "", s)
    return re.sub(r"(.)\1+", r"\1", s)


SPELL_RULES = (
    (r"tion(s?)$", r"shan\1"), (r"sion(s?)$", r"shan\1"), (r"age(s?)$", r"ej\1"), (r"ture(s?)$", r"char\1"),
    (r"ai", "e"), (r"ay", "e"), (r"igh", "ai"), (r"ea", "i"), (r"ee", "i"), (r"oo", "u"), (r"ou", "au"),
    (r"ck", "k"), (r"qu", "kv"), (r"ph", "f"), (r"wh", "v"), (r"ch", "C"), (r"sh", "S"), (r"th", "T"),
    (r"([^aeiou])i([^aeiou])e(s?)$", r"\1ai\2\3"), (r"^i([^aeiou])e(s?)$", r"ai\1\2"),
    (r"a([^aeiou])e(s?)$", r"e\1\2"), (r"o([^aeiou])e(s?)$", r"o\1\2"), (r"u([^aeiou])e(s?)$", r"yu\1\2"),
    (r"c(?=[eiy])", "s"), (r"c", "k"), (r"x", "ks"), (r"y$", "i"), (r"([^aeiou])e(s?)$", r"\1\2"), (r"w", "v"),
    (r"C", "ch"), (r"S", "sh"), (r"T", "th"),
)


def eng_spell(word):
    """Respell an English word roughly the way it comes out of Telugu/Hindi script (time -> taim, game -> gem)."""
    s = word.lower()
    if s.isdigit():
        s = num_words(int(s))
    s = re.sub(r"[^a-z]", "", s)
    for a, b in (("charact", "karakt"), ("chor", "kor"), ("chem", "kem"), ("tech", "tek"), ("sch", "sk"), ("mech", "mek")):
        s = s.replace(a, b)
    for a, b in SPELL_RULES:
        s = re.sub(a, b, s)
    return s


def _sim(a, b):
    """a: romanized Indic, b: English word."""
    return difflib.SequenceMatcher(None, _norm(a), _norm(eng_spell(b))).ratio()


def _lev1(a, b):
    """Levenshtein distance <= 1."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    if len(a) > len(b):
        a, b = b, a
    return any(b[:i] + b[i + 1:] == a for i in range(len(b)))


def load_list(path):
    out = []
    if path and os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line)
    return out


class Lexicon:
    def __init__(self, extra_words=(), stopwords=()):
        words = load_list(os.path.join(HERE, "lexicon_en.txt")) + list(extra_words)
        self.by_key = {}
        for w in words:
            k = eng_key(w)
            if len(k) >= 2:
                self.by_key.setdefault(k, []).append(w)
        self.stop = set(x.lower() for line in stopwords for x in line.split())
        self.proper = {w.lower(): w for w in words if w[:1].isupper() or any(c.isdigit() for c in w)}

    def lookup(self, roman, lang):
        k = indic_key(roman)
        ends_consonant = bool(re.search(r"[^aeiou]$", roman.lower()))
        min_len = 2 if (lang == "hi" or ends_consonant) else 3
        if len(k) < min_len or roman.lower() in self.stop:
            return None
        cands = self.by_key.get(k)
        if not cands:
            return None
        best = max(cands, key=lambda w: _sim(roman, w))
        s = _sim(roman, best)
        if lang == "te":
            # native Telugu words end in a vowel; English words in Telugu script usually don't
            if (ends_consonant and s >= (0.6 if len(k) >= 3 else 0.75)) or (len(k) >= 4 and s >= 0.7):
                return best
            return None
        return best if s >= (0.85 if len(k) <= 2 else 0.7) else None


def fuse(words, hints, lex, lang, window=0.7):
    """words: [{'s','e','w'(roman),'p'}]; hints: [{'s','e','w'}] from the English pass.
    Replaces romanized English words with their English spelling. Marks fixed words with 'en': True."""
    hint_tokens = []
    for i, h in enumerate(hints):
        w = re.sub(r"[^A-Za-z0-9']", "", h["w"])
        if w:
            hint_tokens.append((h["s"], h["e"], w))
    # two-word joins ("head shot" -> హెడ్‌షాట్)
    joins = []
    for a, b in zip(hint_tokens, hint_tokens[1:]):
        if b[0] - a[1] < 0.3:
            joins.append((a[0], b[1], a[2] + b[2], a[2] + " " + b[2]))
    for w in words:
        roman = w["w"]
        if not w.get("indic") or roman.lower() in lex.stop:
            continue
        k = indic_key(roman)
        mid = (w["s"] + w["e"]) / 2
        best, best_score = None, 0
        for s, e, t in hint_tokens:
            if e < w["s"] - window or s > w["e"] + window:
                continue
            ek = eng_key(t)
            score = 0
            if ek == k and len(k) >= 2:
                score = 2 + _sim(roman, t)
            elif len(k) >= 4 and len(ek) >= 4 and _lev1(ek, k) and _sim(roman, t) >= 0.5:
                score = 1 + _sim(roman, t)
            elif len(k) <= 1 and _norm(roman) == _norm(t):
                score = 1.5
            if score:
                score -= min(abs((s + e) / 2 - mid), 2) * 0.3
                if score > best_score:
                    best, best_score = t, score
        for s, e, joined, shown in joins:
            if e < w["s"] - window or s > w["e"] + window:
                continue
            if eng_key(joined) == k and len(k) >= 4:
                sc = 2.2 + _sim(roman, joined)
                if sc > best_score:
                    best, best_score = shown, sc
        # Telugu native words end in a vowel; English ones written in Telugu script mostly don't.
        if best is not None and lang == "te" and re.search(r"[aeiou]$", roman.lower()) and _sim(roman, best.replace(" ", "")) < 0.75:
            best = None
        if best is None:
            lw = lex.lookup(roman, lang)
            if lw:
                best = lw
        if best is not None:
            if best.isdigit() or best == "I":
                w["w"] = best
            else:
                w["w"] = " ".join(lex.proper.get(x.lower(), x.lower()) for x in best.split())
            w["en"] = True
    return words
