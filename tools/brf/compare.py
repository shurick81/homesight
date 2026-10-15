#!/usr/bin/env python3
"""Compare where BRFs spend their money, per m² or as shares.

Usage:
    compare.py 712400-1467=Sjötungan 712400-0089=Bergfoten [--years 2024-2025] [--basis m2|income|running|outflows]
               [--csv out.csv]

Reads the verified CSVs in annual_reviews/brfs/<orgnr>/ and prints one column per (BRF, year).

Bases:
    m2        kr per m² of total area (default). Area per year from property_states.csv: the printed
              "Total yta" (7), else the sum of its parts (8-11), else Bostadsyta totalt + Lokalyta (12+10).
    income    % of income (Nettoomsättning + Övriga intäkter)
    running   % of running costs (everything except investments and loan repayment)
    outflows  % of all money out (running costs + investments + net loan repayment)

Note lines are grouped into BUCKETS because reports itemise differently (one report merges
"Fastighetsskötsel och förvaltning", another lists them apart). Driftkostnader not covered by a
bucket's categories is put in "Other operating", so running costs always add up to the reported totals.
"""
import argparse
import csv
import os
import sys

BRFS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "annual_reviews", "brfs")

# bucket -> financial_events category ids (sign: costs are stored negative)
INCOME = ("Income", (0, 4))
BUCKETS = [
    ("Heating", (23,)),
    ("Interest (net paid)", (7, 15)),
    ("Property care & management", (2, 29)),  # 2 includes 50-55 (skötsel, städ, hiss, besiktning, mark, snö)
    ("Repairs & maintenance", (21, 31, 32, 16)),
    ("Water", (24,)),
    ("Electricity", (22,)),
    ("Board, staff & admin", (5, 6)),  # Övriga externa kostnader + Personalkostnader
    ("Waste", (25,)),
    ("Insurance", (26,)),
    ("Property tax", (28,)),
    ("Cable-TV & broadband", (27,)),
    ("Other operating", (30,)),
]
DRIFT, DRIFT_LISTED = 1, (2, 29, 23, 24, 22, 21, 31, 32, 25, 28, 26, 27, 30)
INVEST, FINANCING = 8, 3
LIQUIDITY, DEBT = (2, 3, 4, 5), (0, 1)


def parse_years(spec):
    a, _, b = spec.partition("-")
    return list(range(int(a), int(b or a) + 1))


def load(orgnr, fname):
    out = {}
    path = os.path.join(BRFS, orgnr, fname)
    if os.path.exists(path):
        for r in csv.DictReader(open(path, encoding="utf-8")):
            out.setdefault(int(r["year"]), {})[int(r["category_id"])] = int(r["amount"])
    return out


def area(prop):
    if not prop:
        return None, "no property_states"
    if 7 in prop:
        return prop[7], "total yta"
    if 8 in prop or 9 in prop:  # lokalyta (10) alone is not a split basis: older reports pair it with 12
        return sum(prop.get(c, 0) for c in (8, 9, 10, 11)), "sum of areas"
    if 12 in prop:
        return prop[12] + prop.get(10, 0), "bostadsyta totalt + lokalyta (older basis)"
    return None, "no area"


def column(ev, st):
    """Positive amounts (kr) per line for one BRF-year."""
    d = {"Income": sum(ev.get(c, 0) for c in INCOME[1])}
    for name, cats in BUCKETS:
        d[name] = -sum(ev.get(c, 0) for c in cats)
    d["Other operating"] += -ev.get(DRIFT, 0) + sum(ev.get(c, 0) for c in DRIFT_LISTED)  # unlisted Driftkostnader
    d["Running costs"] = sum(d[name] for name, _ in BUCKETS)
    d["Investments"] = -ev.get(INVEST, 0)
    d["Net loan repayment"] = max(-ev.get(FINANCING, 0), 0)
    d["Net new borrowing"] = max(ev.get(FINANCING, 0), 0)
    d["Outflows"] = d["Running costs"] + d["Investments"] + d["Net loan repayment"]
    d["Debt at year end"] = sum(st.get(c, 0) for c in DEBT)
    d["Liquid funds at year end"] = sum(st.get(c, 0) for c in LIQUIDITY)
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("brfs", nargs="+", help="orgnr or orgnr=Name")
    ap.add_argument("--years", required=True)
    ap.add_argument("--basis", choices=("m2", "income", "running", "outflows"), default="m2")
    ap.add_argument("--csv", help="also write the table to this file")
    args = ap.parse_args()

    cols, notes = [], []
    for spec in args.brfs:
        orgnr, _, name = spec.partition("=")
        ev, st, pr = load(orgnr, "financial_events.csv"), load(orgnr, "financial_states.csv"), \
            load(orgnr, "property_states.csv")
        for y in parse_years(args.years):
            if y not in ev:
                notes.append(f"{name or orgnr} {y}: no financial_events, skipped")
                continue
            d = column(ev[y], st.get(y, {}))
            m2, how = area(pr.get(y))
            if args.basis == "m2":
                if not m2:
                    notes.append(f"{name or orgnr} {y}: {how}, skipped")
                    continue
                div = m2
                notes.append(f"{name or orgnr} {y}: {m2} m² ({how})")
            else:
                div = {"income": d["Income"], "running": d["Running costs"], "outflows": d["Outflows"]}[args.basis] / 100
            cols.append((f"{name or orgnr} {y}", d, div))

    if args.basis == "m2":
        lines = ["Income"] + [n for n, _ in BUCKETS] + ["Running costs", "Investments", "Net loan repayment",
                                                         "Net new borrowing", "Debt at year end",
                                                         "Liquid funds at year end"]
        unit = "kr per m²"
    else:
        lines = [n for n, _ in BUCKETS] + (["Investments", "Net loan repayment"] if args.basis == "outflows" else [])
        lines += ["Running costs"] if args.basis != "running" else []
        unit = f"% of {args.basis}"

    rows = [[line] + [round(d[line] / div, 1 if args.basis != "m2" else None) for _, d, div in cols] for line in lines]
    w = max(len(line) for line in lines) + 2
    print(f"{unit:<{w}}" + "".join(f"{label:>17}" for label, _, _ in cols))
    for r in rows:
        print(f"{r[0]:<{w}}" + "".join(f"{v:>17}" for v in r[1:]))
    totals = {"income": "Income", "running": "Running costs", "outflows": "Outflows"}
    if args.basis != "m2":
        print(f"{'(total, MSEK)':<{w}}" + "".join(f"{d[totals[args.basis]] / 1e6:>17.1f}" for _, d, _ in cols))
    for n in notes:
        print("  " + n)
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            wr = csv.writer(f)
            wr.writerow([unit] + [label for label, _, _ in cols])
            wr.writerows(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
