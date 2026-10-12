#!/usr/bin/env python3
"""Find the bounding box of text on a PDF page, in PDF points (top-left origin).

Usage:
    locate.py FILE.pdf PAGE "54 426 020" ["-32 451 639" ...] [--near "Nettoomsättning"] [--union]

For each search string, prints every match as:
    x,y,width,height    <matched text>   [line: <full text line>]
Rounded to whole points, ready for the file,page,x,y,width,height CSV columns.

--near TEXT   keep only the match on the same line as TEXT (or closest to it vertically),
              useful when the same number appears more than once on a page.
--union       print one box that covers the chosen match of every search string
              (use it for values that are sums of several lines).

Scanned pages (no text layer) are OCR'd with tesseract (cached, see pdfwords.py); boxes are in points.
"""
import argparse
import re
import sys

from pdfwords import lines_of, words


def norm(s):
    return re.sub(r"[\s   ]+", " ", s.replace("−", "-").replace("–", "-")).strip()


def find(lines, needle):
    """Match needle against consecutive words of each line."""
    needle = norm(needle)
    hits = []
    for ln in lines:
        texts = [norm(w["text"]) for w in ln]
        for i in range(len(ln)):
            acc = ""
            for j in range(i, len(ln)):
                acc = (acc + " " + texts[j]).strip()
                if acc == needle or (j == i and needle in acc and len(needle) > 3):
                    ws = ln[i:j + 1]
                    hits.append({
                        "x0": min(w["x0"] for w in ws), "top": min(w["top"] for w in ws),
                        "x1": max(w["x1"] for w in ws), "bottom": max(w["bottom"] for w in ws),
                        "text": acc, "line": " ".join(texts),
                    })
                    break
                if len(acc) > len(needle):
                    break
    return hits


def fmt(b):
    return f'{round(b["x0"])},{round(b["top"])},{round(b["x1"] - b["x0"])},{round(b["bottom"] - b["top"])}'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("page", type=int)
    ap.add_argument("needles", nargs="+")
    ap.add_argument("--near")
    ap.add_argument("--union", action="store_true")
    args = ap.parse_args()

    lines = lines_of(words(args.pdf, args.page)[0])

    anchor = None
    if args.near:
        a = find(lines, args.near)
        if not a:
            print(f"! --near text not found: {args.near!r}", file=sys.stderr)
        else:
            anchor = a[0]

    chosen = []
    for n in args.needles:
        hits = find(lines, n)
        if anchor and hits:
            hits.sort(key=lambda h: abs(h["top"] - anchor["top"]))
            hits = hits[:1]
        if not hits:
            print(f"! not found: {n!r}")
            continue
        for h in hits:
            print(f'{fmt(h)}\t{h["text"]}\t[line: {h["line"]}]')
        chosen.append(hits[0])

    if args.union and chosen:
        u = {"x0": min(b["x0"] for b in chosen), "top": min(b["top"] for b in chosen),
             "x1": max(b["x1"] for b in chosen), "bottom": max(b["bottom"] for b in chosen)}
        print(f"UNION\t{fmt(u)}")


if __name__ == "__main__":
    sys.exit(main())
