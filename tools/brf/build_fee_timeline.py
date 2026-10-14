#!/usr/bin/env python3
"""Build fee_timeline.json for fee_timeline.html: årsavgift per m² over the last 15 years for the largest BRFs
of a municipality.

Usage: build_fee_timeline.py [--municipality Tyresö] [--top 10] [--years 15]
       (reads annual_reviews/brfs/**, writes fee_timeline.json in the repo root)

Årsavgift per m² = financial_events 17 Årsavgifter / property_states 8 Bostadsrättsyta, as the HSB reports compute
it. Each year uses that year's bostadsrättsyta; a year without one uses the nearest earlier stored year, else the
nearest later one, else index.csv's living_area_m2 (Bostadsyta totalt for older reports without a split). Stored
areas under half the BRF's area are not building totals and are skipped. BRFs are ranked by index.csv's
living_area_m2. Only years up to verified_through are used. A fiscal year that is not 12 months long (general_states
0 Räkenskapsår, e.g. Solhöjden's 18-month 2017) is scaled to 12 months.

View: serve the repo root over http (fetch doesn't load from file://), e.g.
  python3 -m http.server 8001 --bind 127.0.0.1   # then open http://127.0.0.1:8001/fee_timeline.html
"""
import argparse, csv, json, re
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BRFS = ROOT / "annual_reviews/brfs"
OUT = ROOT / "fee_timeline.json"

def read(p):
    return list(csv.DictReader(p.open(encoding="utf-8"))) if p.exists() else []

def short_name(name):
    n = re.sub(r"^(HSB |Riksbyggen )?(Bostadsrättsföreningen|Bostadsrättsförening|Brf|BRF)\s+", "", name)
    return re.sub(r"\s+i Tyresö$", "", n)

def months(span):
    d = re.findall(r"(\d{4})-(\d{2})-\d{2}", span or "")
    if len(d) != 2:
        return 12
    return (int(d[1][0]) - int(d[0][0])) * 12 + int(d[1][1]) - int(d[0][1]) + 1

def area_for(year, stored, fallback):
    if year in stored:
        return stored[year], "same year"
    earlier = [y for y in stored if y < year]
    if earlier:
        return stored[max(earlier)], f"from {max(earlier)}"
    later = [y for y in stored if y > year]
    if later:
        return stored[min(later)], f"from {min(later)}"
    return fallback, "index"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--municipality", default="Tyresö")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--years", type=int, default=15)
    a = ap.parse_args()

    index = [r for r in read(BRFS / "index.csv") if r["municipality"] == a.municipality and r["living_area_m2"]]
    index.sort(key=lambda r: -float(r["living_area_m2"]))
    unranked = [r["name"] for r in read(BRFS / "index.csv") if r["municipality"] == a.municipality and not r["living_area_m2"]]

    brfs, last_year = [], 0
    for r in index[:a.top]:
        org, base = r["orgnr"], float(r["living_area_m2"])
        verified = int(r["verified_through"] or 0)
        stored = {int(p["year"]): float(p["amount"]) for p in read(BRFS / org / "property_states.csv")
                  if p["category_id"] == "8" and float(p["amount"]) >= 0.5 * base}
        spans = {int(g["year"]): months(g["value"]) for g in read(BRFS / org / "general_states.csv") if g["category_id"] == "0"}
        points = []
        for e in read(BRFS / org / "financial_events.csv"):
            y = int(e["year"])
            if e["category_id"] != "17" or y > verified:
                continue
            area, basis = area_for(y, stored, base)
            fee, mo = float(e["amount"]), spans.get(y, 12)
            points.append({"year": y, "fee": round(fee), "months": mo, "area": round(area), "area_basis": basis,
                           "kr_m2": round(fee * 12 / mo / area), "file": e["file"], "page": int(e["page"])})
            last_year = max(last_year, y)
        points.sort(key=lambda p: p["year"])
        brfs.append({"orgnr": org, "name": r["name"], "short": short_name(r["name"]),
                     "apartments": int(r["apartments"]) if r["apartments"] else None,
                     "area": round(base), "points": points})

    first_year = last_year - a.years + 1
    for b in brfs:
        b["points"] = [p for p in b["points"] if p["year"] >= first_year]
    OUT.write_text(json.dumps({"municipality": a.municipality, "first_year": first_year, "last_year": last_year,
                               "built": date.today().isoformat(), "unranked": unranked, "brfs": brfs},
                              ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    n = sum(len(b["points"]) for b in brfs)
    print(f"{OUT.relative_to(ROOT)}: {len(brfs)} BRFs, {n} BRF-years, {first_year}–{last_year}")

if __name__ == "__main__":
    main()
