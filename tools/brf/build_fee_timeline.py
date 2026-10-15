#!/usr/bin/env python3
"""Build fee_timeline.json for fee_timeline.html: årsavgift per m² and the average sale price per m² over the
last 15 years for the largest BRFs of a municipality.

Usage: build_fee_timeline.py [--municipality Tyresö] [--top 10] [--years 15]
       (reads annual_reviews/brfs/**, writes fee_timeline.json in the repo root)

Årsavgift per m² = financial_events 17 Årsavgifter / property_states 8 Bostadsrättsyta, as the HSB reports compute
it. Each year uses that year's bostadsrättsyta; a year without one uses the nearest earlier stored year, else the
nearest later one, else index.csv's living_area_m2 (Bostadsyta totalt for older reports without a split). Stored
areas under half the BRF's area are not building totals and are skipped. BRFs are ranked by index.csv's
living_area_m2. Only years up to verified_through are used. A fiscal year that is not 12 months long (general_states
0 Räkenskapsår, e.g. Solhöjden's 18-month 2017) is scaled to 12 months.

Sale price per m² = the mean of final_price_kr / living_area_m2 over the year's sales of the BRF in the Booli snapshot
(market/booli/, unverified listing data; Booli links each sale to its BRF, mapped to an orgnr by brf_details).
Years with fewer than MIN_SALES sales are left out. The same mean over every bostadsrätt sale in the municipality
(Booli's kommun) is the reference line.

Economy per m² (debt at year end, net interest paid, board/staff/admin, Driftkostnader, investments) uses compare.py's definitions and
its total-area basis (Total yta, else the sum of the areas, else Bostadsyta totalt + Lokalyta), the area the
reports' own kr/kvm key figures use. As for the fee, a year without an area uses the nearest stored year, areas under
half the BRF's are skipped, and a BRF that later reports a split basis uses that basis for its older years too
(Sjötungan before 2020). Flows are scaled to 12 months; debt is a balance and is not. Investments are left out
for a year whose report has no cash-flow statement (no investing or financing line).

View: serve the repo root over http (fetch doesn't load from file://), e.g.
  python3 -m http.server 8001 --bind 127.0.0.1   # then open http://127.0.0.1:8001/fee_timeline.html
"""
import argparse, csv, gzip, json, re, sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import compare  # noqa: E402  (the single definition of the cost buckets and the total area)

ROOT = Path(__file__).resolve().parents[2]
BRFS = ROOT / "annual_reviews/brfs"
BOOLI = ROOT / "market/booli"
OUT = ROOT / "fee_timeline.json"
MIN_SALES = 3

def read(p):
    return list(csv.DictReader(p.open(encoding="utf-8"))) if p.exists() else []

def read_gz(p):
    with gzip.open(p, "rt", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))

def sale_prices(orgnrs, municipality, first_year, last_year):
    """{orgnr: [{year, n, kr_m2}]}: mean sale price per m² per year from the Booli snapshot; the key
    municipality holds the same over every sale in the municipality."""
    org_of = {r["booli_brf_id"]: r["org_number"] for r in read_gz(BOOLI / "brf_details_booli.csv.gz")}
    by = {}
    for r in read_gz(BOOLI / "sales_booli.csv.gz"):
        org = org_of.get(r["booli_brf_id"])
        if r["object_type"] != "bostadsrätt":
            continue
        try:
            price, area, year = float(r["final_price_kr"]), float(r["living_area_m2"]), int(r["sold_date"][:4])
        except ValueError:
            continue
        if not (price > 0 and area > 0 and first_year <= year <= last_year):
            continue
        if org in orgnrs:
            by.setdefault(org, {}).setdefault(year, []).append(price / area)
        if r["kommun"] == municipality:
            by.setdefault(municipality, {}).setdefault(year, []).append(price / area)
    return {org: [{"year": y, "n": len(v), "kr_m2": round(sum(v) / len(v))}
                  for y, v in sorted(years.items()) if len(v) >= MIN_SALES]
            for org, years in by.items()}

METRICS = {  # key: (compare.column line, is a flow)
    "debt": ("Debt at year end", False),
    "interest": ("Interest (net paid)", True),  # interest paid minus received: some reports net them
    "admin": ("Board, staff & admin", True),
    "drift": (None, True),  # Driftkostnader as reported (event 1)
    "invest": ("Investments", True),
}

def economy(org, years, spans, base):
    """{metric: [{year, kr, area, v}]} per m² of total area for the BRF's verified years."""
    ev, st, pr = (compare.load(org, f) for f in ("financial_events.csv", "financial_states.csv", "property_states.csv"))
    areas = {y: a for y in pr for a, how in [compare.area(pr[y])] if a and a >= 0.5 * base}
    split = {y: a for y, a in areas.items() if compare.area(pr[y])[1] != "bostadsyta totalt + lokalyta (older basis)"}
    areas = split or areas
    out = {k: [] for k in METRICS}
    for y in years:
        if y not in ev:
            continue
        col = compare.column(ev[y], st.get(y, {}))
        area, _ = area_for(y, areas, None)
        if not area:
            continue
        for key, (line, flow) in METRICS.items():
            if key == "debt" and y not in st:
                continue
            if key == "invest" and compare.INVEST not in ev[y] and compare.FINANCING not in ev[y]:
                continue  # no cash-flow statement that year: investments unknown, not zero
            kr = -ev[y].get(compare.DRIFT, 0) if line is None else col[line]
            scale = 12 / spans.get(y, 12) if flow else 1
            out[key].append({"year": y, "kr": kr, "area": area, "months": spans.get(y, 12) if flow else 12,
                             "v": round(kr * scale / area)})
    return out

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

    brfs, last_year, spans_of = [], 0, {}
    for r in index[:a.top]:
        org, base = r["orgnr"], float(r["living_area_m2"])
        verified = int(r["verified_through"] or 0)
        stored = {int(p["year"]): float(p["amount"]) for p in read(BRFS / org / "property_states.csv")
                  if p["category_id"] == "8" and float(p["amount"]) >= 0.5 * base}
        spans = {int(g["year"]): months(g["value"]) for g in read(BRFS / org / "general_states.csv") if g["category_id"] == "0"}
        spans_of[org] = spans
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
    sales = sale_prices({b["orgnr"] for b in brfs}, a.municipality, first_year, last_year)
    for b in brfs:
        b["points"] = [p for p in b["points"] if p["year"] >= first_year]
        b["sales"] = sales.get(b["orgnr"], [])
        b["economy"] = economy(b["orgnr"], [p["year"] for p in b["points"]], spans_of[b["orgnr"]], b["area"])
    snapshot = re.search(r"sales:\s*\n\s*file:.*\n\s*snapshot:\s*(\S+)", (ROOT / "market/sources.yaml").read_text(encoding="utf-8"))
    OUT.write_text(json.dumps({"municipality": a.municipality, "first_year": first_year, "last_year": last_year,
                               "built": date.today().isoformat(),
                               "sales_snapshot": snapshot.group(1) if snapshot else None, "min_sales": MIN_SALES,
                               "municipality_sales": sales.get(a.municipality, []), "unranked": unranked, "brfs": brfs},
                              ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    n, m = sum(len(b["points"]) for b in brfs), sum(len(b["sales"]) for b in brfs)
    for k in METRICS:
        print(f"  {k}: {sum(len(b['economy'][k]) for b in brfs)} BRF-years")
    print(f"{OUT.relative_to(ROOT)}: {len(brfs)} BRFs, {n} fee years, {m} sale-price years, {first_year}–{last_year}")

if __name__ == "__main__":
    main()
