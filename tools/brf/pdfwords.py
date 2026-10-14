"""Words with bounding boxes for one PDF page, shared by locate.py and verify_sources.py.

Boxes are in PDF points from the top-left corner. Pages without a text layer are OCR'd with
tesseract at 300 dpi. OCR output is cached in <repo>/.cache/pdfwords/ (keyed by the file's
sha256), so every script sees exactly the same words for a scanned page.
"""
import hashlib
import json
import os
import re

import logging

import pdfplumber

logging.getLogger("pdfminer").setLevel(logging.ERROR)  # harmless font warnings ("Could get FontBBox …")

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), *[".."] * 2, ".cache", "pdfwords")
_sha = {}


def sha256(path):
    if path not in _sha:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        _sha[path] = h.hexdigest()
    return _sha[path]


def _ocr(page, res=300, psm=3):
    import pytesseract

    img = page.to_image(resolution=res).original
    data = pytesseract.image_to_data(img, lang="swe+eng", config=f"--psm {psm}",
                                     output_type=pytesseract.Output.DICT)
    k = 72 / res
    return [
        {"text": t, "x0": data["left"][i] * k, "top": data["top"][i] * k,
         "x1": (data["left"][i] + data["width"][i]) * k, "bottom": (data["top"][i] + data["height"][i]) * k}
        for i, t in enumerate(data["text"]) if t.strip()
    ]


# e-signing services stamp every page of a scanned report with a line of text ("Transaktion 0922… Signerat
# AW, BM"), so the page has a text layer although its content is only an image; such lines (and a bare
# page number) do not count as text when deciding to OCR.
_STAMP = re.compile(r"^(Transaktion \d{8,} Signerat\b.*|Detta dokumentet har signerats genom digitala signeringstjänsten GetAccept\.|Fingerprint: [0-9a-f]{20,}|Electronically signed .*|https://sign\.visma\.net/\S+.*|\d{1,3})$")


def _content_words(ws):
    return [w for line in lines_of(ws) if not _STAMP.match(" ".join(x["text"] for x in line)) for w in line if re.search(r"\w", w["text"])]


def _force_ocr(path):
    """True when the document's metadata.yaml says `ocr: force`: its text layer is unreliable (for example
    ligature glyphs missing: "Ne oomsä ning"), so every page is read by OCR instead."""
    meta = os.path.join(os.path.dirname(os.path.abspath(path)), "metadata.yaml")
    if not os.path.exists(meta):
        return False
    try:
        import yaml
        with open(meta, encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}).get("ocr") == "force"
    except Exception:
        return False


def is_scanned(path, pageno):
    """True when page `pageno` is read by OCR: from the OCR cache when the page has been read, else decided
    from its text layer as words() would, without running OCR."""
    cache = os.path.join(os.path.normpath(CACHE), sha256(path), f"{pageno}.json")
    if os.path.exists(cache):
        with open(cache, encoding="utf-8") as f:
            return json.load(f)["ocr"]
    if _force_ocr(path):
        return True
    with pdfplumber.open(path) as pdf:
        ws = pdf.pages[pageno - 1].extract_words(keep_blank_chars=False, use_text_flow=False)
    return len(_content_words(ws)) < 5


def page_count(path):
    with pdfplumber.open(path) as pdf:
        return len(pdf.pages)


def words(path, pageno, second_pass=False):
    """Return (words, ocr_flag) for 1-based page `pageno`.

    second_pass=True re-OCRs a scanned page at 400 dpi as one uniform text block (psm 6), which
    often reads table columns that the default pass garbles. Text-layer pages are unaffected.
    """
    if second_pass and not words(path, pageno)[1]:
        return words(path, pageno)
    name = f"{pageno}.psm6.json" if second_pass else f"{pageno}.json"
    cache = os.path.join(os.path.normpath(CACHE), sha256(path), name)
    if os.path.exists(cache):
        with open(cache, encoding="utf-8") as f:
            d = json.load(f)
        return d["words"], d["ocr"]
    with pdfplumber.open(path) as pdf:
        page = pdf.pages[pageno - 1]
        ws = [{k: w[k] for k in ("text", "x0", "top", "x1", "bottom")}
              for w in page.extract_words(keep_blank_chars=False, use_text_flow=False)]
        ocr = len(_content_words(ws)) < 5 or second_pass or _force_ocr(path)
        if ocr:
            ws = _ocr(page, 400, 6) if second_pass else _ocr(page)
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    with open(cache, "w", encoding="utf-8") as f:
        json.dump({"words": ws, "ocr": ocr}, f, ensure_ascii=False)
    return ws, ocr


def lines_of(ws):
    """Group words into lines by vertical position, each line sorted left to right."""
    lines = []
    for w in sorted(ws, key=lambda w: (round(w["top"]), w["x0"])):
        for ln in lines:
            if abs(ln[0]["top"] - w["top"]) < 3:
                ln.append(w)
                break
        else:
            lines.append([w])
    for ln in lines:
        ln.sort(key=lambda w: w["x0"])
    return lines


def union(ws):
    return {"x0": min(w["x0"] for w in ws), "top": min(w["top"] for w in ws),
            "x1": max(w["x1"] for w in ws), "bottom": max(w["bottom"] for w in ws)}


# "36", "104", "36104947", OCR variants "32.973" / "3614"; optional sign or parentheses
_NUM = re.compile(r"^[(−–-]?\d[\d.,]*[)]?$")


_LOOKALIKE = str.maketrans("BOolIS", "800115")


def _clean(t):
    """Undo common OCR misreads in a numeric token: '-B11' -> '-811', '“811' -> '811'."""
    t = t.strip("“”\"'‘’~*")
    t = re.sub(r"(?<=\d)(m2|m²|m\?|kvm|kr)$", "", t)  # unit glued to the number: "385m?" (OCR of 385m²)
    if re.fullmatch(r"[(−–-]?[\dBOolIS.,]+[)]?", t) and sum(c.isdigit() for c in t) >= max(1, len(t) // 2):
        t = t[0] + t[1:].translate(_LOOKALIKE) if t[0] in "(−–-" else t.translate(_LOOKALIKE)
    return t


def numbers(ws):
    """Printed amounts on a page: runs of digit groups on one line ("36 104 947", "-1 234").

    Returns dicts with value (int, sign as printed), box, text and `maximal`. A maximal run is
    the whole printed amount; its sub-runs are returned too (maximal=False), because OCR does
    not always separate two adjacent columns.
    """
    def is_num(w):
        return bool(_NUM.match(w["text"]))

    def continues(prev, w):
        # a continuation group is 3 digits (6 when OCR merged two groups: "63 914918"), unsigned, close to the previous group.
        # The previous group may be longer than 3 digits: OCR often drops the first space ("3614 918").
        return (len(re.sub(r"\D", "", w["text"])) in (3, 6) and w["text"][0] not in "(−–-"
                and w["x0"] - prev["x1"] <= 10 and not prev["text"].endswith(")"))

    def entry(run, maximal):
        text = " ".join(w["text"] for w in run)
        digits = re.sub(r"\D", "", text)
        neg = run[0]["text"][0] in "(−–-"
        return {"value": -int(digits) if neg else int(digits), "text": text, "maximal": maximal, **union(run)}

    ws = [{**w, "text": _clean(w["text"])} for w in ws]
    ws = [w for w in ws if w["text"]]
    out = []
    for ln in lines_of(ws):
        i = 0
        while i < len(ln):
            if not is_num(ln[i]):
                i += 1
                continue
            j = i
            while j + 1 < len(ln) and is_num(ln[j + 1]) and continues(ln[j], ln[j + 1]):
                j += 1
            run = ln[i:j + 1]
            out.append(entry(run, True))
            for a in range(len(run)):
                for b in range(a + 1, len(run) + 1):
                    if (a, b) != (0, len(run)):
                        out.append(entry(run[a:b], False))
            i = j + 1
    return out
