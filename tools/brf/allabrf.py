#!/usr/bin/env python3
"""List the annual reports a BRF has shared publicly on allabrf.se.

allabrf's document list asks visitors to log in with BankID, but every document the association has
made public is linked from its /dokument page as a plain active_storage URL, which downloads without
login. This finds the BRF's page (by trying the usual slugs and the org number) and prints those links.

Usage:
    allabrf.py ORGNR "NAME" [ORGNR "NAME" ...]
    allabrf.py 712400-0808 "HSB Brf Laken i Tyresö"
    allabrf.py 712400-0808 "HSB Brf Laken i Tyresö" --download DIR    # also save the PDFs to DIR/<orgnr>/

The page only says what kind of document each link is in loosely structured text, so check every
download's type and fiscal year in the PDF itself (and the org number) before importing it with
import_document.py. Some links are stadgar, ekonomisk plan or energideklaration.
"""
import argparse
import os
import re
import subprocess
import time

UA = "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Chrome/120 Safari/537.36"
BASE = "https://www.allabrf.se"


def get(url):
    out = subprocess.run(["curl", "-sL", "-m", "30", "-A", UA, "-o", "-", "-w", "\n%{http_code}", url],
                         capture_output=True)
    body, _, code = out.stdout.decode("utf-8", "replace").rpartition("\n")
    return code, body


def slugify(s):
    s = s.lower().translate(str.maketrans("åäöé", "aaoe"))
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def candidates(orgnr, name):
    base = slugify(name)
    core = slugify(re.sub(r"(?i)\b(hsb|brf|riksbyggen|bostadsrättsföreningen|bostadsrättsförening)\b", "", name))
    core = re.sub(r"-i-[a-z]+$", "", core)
    kommun = re.search(r"-i-([a-z]+)$", base)
    # Stockholm associations are named after the postal town: brf-landsort-2-karrtorp-johanneshov
    tails = ["", "-i-" + kommun.group(1)] if kommun else ["", "-i-tyreso", "-i-stockholm", "-stockholm", "-johanneshov",
                                                           "-karrtorp-johanneshov", "-enskede", "-bagarmossen"]
    out = [base, base + "-johanneshov"]
    for prefix in ("brf-", "hsb-brf-", "bostadsrattsforeningen-", ""):
        for tail in tails:
            out.append(prefix + core + tail)
    out.append(orgnr)
    return list(dict.fromkeys(out))


def find(orgnr, name):
    """(slug, [document urls]) for the BRF, or (None, []) when no page with its org number is found."""
    digits = orgnr.replace("-", "")
    for slug in candidates(orgnr, name):
        code, body = get(f"{BASE}/{slug}/dokument")
        time.sleep(0.7)                                   # be gentle: allabrf is a small site
        if code == "200" and digits in body.replace("-", ""):
            links = re.findall(r"""active_storage/blobs/redirect/[^"' <>]+""", body)
            return slug, [f"{BASE}/rails/{l}" for l in dict.fromkeys(links)]
    return None, []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pairs", nargs="+", help="ORGNR NAME pairs")
    ap.add_argument("--download", metavar="DIR")
    args = ap.parse_args()
    if len(args.pairs) % 2:
        ap.error("give ORGNR NAME pairs")
    for orgnr, name in zip(args.pairs[::2], args.pairs[1::2]):
        slug, links = find(orgnr, name)
        print(f"{orgnr} {name}: {slug or 'no allabrf page found'} ({len(links)} public documents)")
        for i, url in enumerate(links):
            print("  ", url)
            if args.download:
                d = os.path.join(args.download, orgnr)
                os.makedirs(d, exist_ok=True)
                subprocess.run(["curl", "-sL", "-m", "90", "-A", UA, "-o", os.path.join(d, f"allabrf_{i}.pdf"), url])


if __name__ == "__main__":
    main()
