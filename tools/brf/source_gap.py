#!/usr/bin/env python3
"""List the documents a BRF's report sources offer that we do not hold yet.

Run it before saying a report "was not found", and after adding a BRF. For every BRF it checks

  * each listing page recorded in the BRF's profile.yaml (`sources:`, a list of {url, note}): the association's own
    "årsredovisningar" page, an HSB page, a document archive;
  * allabrf.se (tools/brf/allabrf.py finds the BRF's /dokument page);
  * svenskbrf.se (the /forening/<ort>/<slug>/dokument/arsredovisning/ page, found from the BRF's name).

and prints every document link whose file is not already an imported report's source (raw/.../metadata.yaml `source.url`,
compared by download id or file name, not by the whole URL: signed links change). Link text and file name are printed
so the year can be read; check the org number and fiscal year inside the PDF before importing (import_document.py).

Usage:
    source_gap.py                  every BRF in tracked.csv
    source_gap.py 712400-0139 ...  only these
    source_gap.py --no-web ...     only the pages in profile.yaml (no allabrf / svenskbrf lookups)

profile.yaml:
    sources:
      - {url: https://www.brfbjorkbacken.se/sidor/arsredovisningar, note: "own site, all years 2007-"}
"""
import argparse
import csv
import re
import subprocess
import sys
import time
import unicodedata
import urllib.parse
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
BRFS = REPO / "annual_reviews" / "brfs"
RAW = REPO / "raw" / "annual_reviews" / "brfs"
UA = "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Chrome/120 Safari/537.36"

# a link is a document when its URL looks like one of these
DOC = re.compile(r"download\.php\?id=|\.pdf\b|/system/files/|showfile|/contentassets/|active_storage/blobs|"
                 r"fetchfile|/laddaner/|/wp-content/uploads/|drive\.google\.com/uc|/file/d/", re.I)
SKIP = re.compile(r"stadgar|energideklaration|ekonomisk[-_ ]plan|ordningsregler|hyresavtal|blankett", re.I)


def get(url):
    out = subprocess.run(["curl", "-sL", "-m", "40", "-A", UA, "-w", "\n%{http_code}", url], capture_output=True)
    body, _, code = out.stdout.decode("utf-8", "replace").rpartition("\n")
    time.sleep(0.8)                                    # be gentle: small sites block IPs that crawl
    return code, body


def key(url):
    """What identifies a document across signed or re-hosted URLs: the download id, else the decoded file name."""
    u = urllib.parse.unquote(url)
    m = re.search(r"download\.php\?id=(\d+)", u)
    if m:
        return "id:" + m.group(1)
    m = re.search(r"[?&](?:filID|id)=([\w-]+)", u)
    if m:
        return "id:" + m.group(1)
    name = urllib.parse.urlparse(u).path.rsplit("/", 1)[-1]
    name = unicodedata.normalize("NFC", name).lower().replace("+", " ")
    return "file:" + name


def held(orgnr):
    """{key: fiscal_year} of the documents imported for the BRF."""
    out = {}
    for meta in (RAW / orgnr).glob("*/metadata.yaml"):
        m = yaml.safe_load(meta.open(encoding="utf-8")) or {}
        url = (m.get("source") or {}).get("url")
        if url:
            out[key(url)] = meta.parent.name
        out["file:" + str(m.get("file", "")).lower()] = meta.parent.name
    return out


def links(page_url, html):
    """[(absolute url, text)] for the document links of a page."""
    out = []
    for href, text in re.findall(r"""<a\b[^>]*?href=["']([^"']+)["'][^>]*>(.*?)</a>""", html, re.S | re.I):
        url = urllib.parse.urljoin(page_url, href.replace("&amp;", "&"))
        if DOC.search(url):
            out.append((url, re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()))
    # allabrf prints its links in single quotes outside <a> text
    for href in re.findall(r"""['"](/?rails/active_storage/blobs/redirect/[^'"]+)['"]""", html):
        out.append((urllib.parse.urljoin(page_url, "/" + href.lstrip("/")), ""))
    return list(dict.fromkeys(out))


def slug(s):
    s = s.lower().translate(str.maketrans("åäöé", "aaoe"))
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def pages_for(orgnr, name, no_web):
    """[(label, url)] of the listing pages to check."""
    pages = []
    prof_path = BRFS / orgnr / "profile.yaml"
    if prof_path.exists():
        prof = yaml.safe_load(prof_path.open(encoding="utf-8")) or {}
        pages += [("profile", s["url"]) for s in prof.get("sources") or []]
    if not no_web:
        sys.path.insert(0, str(Path(__file__).parent))
        import allabrf
        a_slug, _ = allabrf.find(orgnr, name)
        if a_slug:
            pages.append(("allabrf", f"{allabrf.BASE}/{a_slug}/dokument"))
        core = slug(re.sub(r"(?i)\b(hsb|brf|riksbyggen|bostadsrättsförening|bostadsrättsföreningen|i tyresö)\b", "", name))
        if core:
            pages.append(("svenskbrf", f"https://www.svenskbrf.se/forening/tyreso/{core}-i-tyreso/dokument/arsredovisning/"))
    return pages


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("orgnr", nargs="*")
    ap.add_argument("--no-web", action="store_true")
    args = ap.parse_args()
    with (BRFS / "tracked.csv").open(encoding="utf-8") as f:
        tracked = {r["orgnr"]: r["name"] for r in csv.DictReader(f)}
    todo = args.orgnr or list(tracked)
    total = 0
    for org in todo:
        name = tracked.get(org, org)
        have = held(org)
        new = []
        pages = pages_for(org, name, args.no_web)
        for label, url in pages:
            code, html = get(url)
            if code != "200":
                continue
            for link, text in links(url, html):
                if key(link) in have or SKIP.search(urllib.parse.unquote(link) + " " + text):
                    continue
                new.append((label, text, link))
        years = sorted({y for y in have.values()})
        print(f"{org} {name}: holds {', '.join(years) or 'nothing'}; checked {len(pages)} page(s); {len(new)} not imported")
        for label, text, link in dict.fromkeys(new):
            seen = [y for y in re.findall(r"20\d\d", text) if y in years]
            tag = f" (year {seen[0]} held)" if seen and label != "profile" else ""
            print(f"   [{label}] {text[:60]!r:64}{tag} {link}")
        total += len(new)
    print(f"{total} document link(s) not imported")


if __name__ == "__main__":
    main()
