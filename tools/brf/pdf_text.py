#!/usr/bin/env python3
"""Dump the text of a PDF page by page, so values can be read and cited by page.

Usage:
    pdf_text.py FILE.pdf [--pages 3-9,12] [--grep REGEX]

Each line is prefixed with its 1-based page number ("p12| ...").
Pages without a text layer (scans) are OCR'd with tesseract (swe+eng, cached by pdfwords.py) and
marked "[OCR]" in the page header.
"""
import argparse
import re
import sys

import pdfplumber

from pdfwords import lines_of, words  # words() decides OCR (also for e-sign-stamped scans)


def parse_pages(spec, n):
    if not spec:
        return list(range(1, n + 1))
    pages = []
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-")
            pages.extend(range(int(a), int(b) + 1))
        else:
            pages.append(int(part))
    return [p for p in pages if 1 <= p <= n]


def ocr_text(path, pn):
    """Text of a scanned page, rebuilt from the cached OCR words (same words locate.py sees)."""
    ws, _ = words(path, pn)
    return "\n".join(" ".join(w["text"] for w in ln) for ln in lines_of(ws))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--pages")
    ap.add_argument("--grep", help="only print lines matching this regex (case-insensitive)")
    args = ap.parse_args()
    rx = re.compile(args.grep, re.I) if args.grep else None

    with pdfplumber.open(args.pdf) as pdf:
        for pn in parse_pages(args.pages, len(pdf.pages)):
            page = pdf.pages[pn - 1]
            text = page.extract_text(layout=False) or ""
            ocr = len(text.strip()) < 30 or words(args.pdf, pn)[1]   # same decision as locate/verify
            if ocr:
                text = ocr_text(args.pdf, pn)
            if not rx:
                print(f"===== page {pn} ({page.width:.0f}x{page.height:.0f}pt){' [OCR]' if ocr else ''}")
            for line in text.splitlines():
                line = re.sub(r"\s{2,}", " ", line).strip()
                if line and (not rx or rx.search(line)):
                    print(f"p{pn}| {line}")


if __name__ == "__main__":
    sys.exit(main())
