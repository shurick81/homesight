#!/usr/bin/env python3
"""Build source_viewer.json for source_viewer.html: every extracted value with its PDF box, per BRF and document.

Usage: build_viewer_data.py   (reads annual_reviews/brfs/**, writes source_viewer.json in the repo root)
"""
import csv, json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BRFS = ROOT / "annual_reviews/brfs"
RAW = ROOT / "raw/annual_reviews/brfs"
OUT = ROOT / "source_viewer.json"
TABLES = [("financial_events", "event_categories.csv", "amount", "Händelser"),
          ("financial_states", "state_categories.csv", "amount", "Tillstånd"),
          ("general_states", "general_categories.csv", "value", "Allmänt"),
          ("property_states", "property_categories.csv", "amount", "Fastighet")]

def read(p):
    return list(csv.DictReader(p.open(encoding="utf-8"))) if p.exists() else []

def box(r):
    return [int(r["page"]), *(float(r[k]) if r[k] else None for k in ("x", "y", "width", "height"))]   # box may be missing

def main():
    cats = {t: {r["id"]: r for r in read(BRFS / c)} for t, c, _, _ in TABLES}
    brfs = []
    for d in sorted(BRFS.glob("*-*")):
        if not d.is_dir():
            continue
        org = d.name
        docs = {p.name: str(p.relative_to(ROOT)) for p in sorted(RAW.glob(f"{org}/*/*.pdf"))}
        fields = []   # [file, group, year, label, value, page, x, y, w, h]
        def add(r, group, year, label, value):
            if r["file"] in docs and r["page"]:
                fields.append([r["file"], group, year, label, value, *box(r)])
        for t, cfile, vcol, group in TABLES:
            for r in read(d / f"{t}.csv"):
                c = cats[t].get(r["category_id"], {})
                name = c.get("name", f"#{r['category_id']}")
                parent = cats[t].get(c.get("parent", ""), {}).get("name")
                add(r, group, r["year"], f"{parent} › {name}" if parent else name, r[vcol])
        for r in read(d / "pipe_works.csv"):
            yrs = r["year_from"] if r["year_from"] == r["year_to"] else f"{r['year_from']}–{r['year_to']}"
            add(r, "Rörarbeten", yrs, f"{r['kind']} ({r['scope'] or '–'}, {r['status']})", r["text"])
        if not fields:
            continue
        name = next((r["name"] for r in read(BRFS / "index.csv") if r["orgnr"] == org), org)
        used = sorted({f[0] for f in fields})
        brfs.append({"orgnr": org, "name": name, "docs": {f: docs[f] for f in used}, "fields": fields})
    OUT.write_text(json.dumps(brfs, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{OUT.name}: {len(brfs)} BRFs, {sum(len(b['fields']) for b in brfs)} fields")

main()
