#!/usr/bin/env python3
"""Sanity-check a BRF's extracted CSVs.

Usage:
    validate.py annual_reviews/brfs/<orgnr> [--years 2016-2025]

Checks: headers, known category ids, numeric amounts, duplicate (year, category),
missing source fields, the arithmetic identities between parent and child
event categories (e.g. Driftkostnader vs the sum of its sub-items), and the
identities that chain consecutive years (see annual_reviews/brfs/README.md):

  CASH  sum of top-level financial_events(Y) == L(Y) - L(Y-1),  L = states 2+3+4+5
        (an ERROR when off by more than CASH_TOLERANCE kr)
  DEBT  event 3 (financing)(Y) == D(Y) - D(Y-1),  D = states 0+1
        (INFO when off: the difference is financing that is not loans, e.g. insatser)
  AREA  property total yta == sum of the area rows under it, when the report prints a total (ERROR when off)
  PROP  a property count or area that differs from the previous year (INFO: a real change or a new basis)

restatements.csv (optional, same columns as financial_states plus `note`): opening balances that year Y's own
report restates (its KF "Likvida medel vid årets början", or a restated comparison column), with their source.
When it has liquidity (or debt) rows for Y, those are Y's opening for CASH (or DEBT) instead of the previous
report's closing balances, and the difference is printed as RESTATED (INFO): a reclassification the report
made, not an extraction error. Rows are verified against the page like any other value (verify_sources.py).
"""
import argparse
import csv
import os
import sys
from collections import defaultdict

HEADERS = {
    "financial_events.csv": ["year", "category_id", "amount", "file", "page", "x", "y", "width", "height"],
    "financial_states.csv": ["year", "category_id", "amount", "file", "page", "x", "y", "width", "height"],
    "general_states.csv": ["year", "category_id", "value", "file", "page", "x", "y", "width", "height"],
    "property_states.csv": ["year", "category_id", "amount", "file", "page", "x", "y", "width", "height"],
}
CATS = {
    "financial_events.csv": "event_categories.csv",
    "financial_states.csv": "state_categories.csv",
    "general_states.csv": "general_categories.csv",
    "property_states.csv": "property_categories.csv",
}


LIQUIDITY = (2, 3, 4, 5)
DEBT = (0, 1)
FINANCING = 3
CASH_TOLERANCE = 25  # kr; rounding in the printed reports


def parse_years(spec):
    if not spec:
        return None
    a, _, b = spec.partition("-")
    return set(range(int(a), int(b or a) + 1))


def load_cats(path):
    with open(path, newline="", encoding="utf-8") as f:
        return {int(r["id"]): (r["name"], int(r["parent"]) if r["parent"] else None) for r in csv.DictReader(f)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("brf_dir")
    ap.add_argument("--years", help="a year or a range, e.g. 2024 or 2016-2025")
    args = ap.parse_args()
    years = parse_years(args.years)
    cats_dir = os.path.dirname(os.path.normpath(args.brf_dir))
    problems = 0

    def bad(msg):
        nonlocal problems
        problems += 1
        print("ERROR", msg)

    events = defaultdict(dict)
    all_events, all_states = defaultdict(dict), defaultdict(dict)  # every year, for the cross-year checks
    all_property = defaultdict(dict)
    for fname, header in HEADERS.items():
        path = os.path.join(args.brf_dir, fname)
        if not os.path.exists(path):
            print("WARN  missing", fname)
            continue
        cats = load_cats(os.path.join(cats_dir, CATS[fname]))
        seen = set()
        with open(path, newline="", encoding="utf-8") as f:
            rd = csv.DictReader(f)
            if rd.fieldnames != header:
                bad(f"{fname}: header {rd.fieldnames} != {header}")
            for i, r in enumerate(rd, start=2):
                cid = int(r["category_id"])
                if fname != "general_states.csv":
                    try:
                        store = {"financial_events.csv": all_events, "financial_states.csv": all_states,
                                 "property_states.csv": all_property}[fname]
                        store[int(r["year"])][cid] = int(r["amount"])
                    except ValueError:
                        pass
                if years and int(r["year"]) not in years:
                    continue
                key = (r["year"], cid)
                where = f"{fname}:{i} year={r['year']} cat={cid}"
                if cid not in cats:
                    bad(f"{where}: unknown category id")
                if key in seen:
                    bad(f"{where}: duplicate (year, category_id)")
                seen.add(key)
                if not r["file"] or not r["page"]:
                    bad(f"{where}: missing file/page")
                if fname != "general_states.csv":
                    try:
                        amt = int(r["amount"])
                    except ValueError:
                        bad(f"{where}: amount {r['amount']!r} is not an integer")
                        continue
                    if fname == "financial_events.csv":
                        events[int(r["year"])][cid] = amt
                elif not r["value"]:
                    print(f"WARN  {where}: empty value")

    # parent/child consistency for event categories
    ecats = load_cats(os.path.join(cats_dir, "event_categories.csv"))
    children = defaultdict(list)
    for cid, (_, parent) in ecats.items():
        if parent is not None:
            children[parent].append(cid)
    for year, vals in sorted(events.items()):
        for parent, kids in children.items():
            present = [k for k in kids if k in vals]
            if parent in vals and present:
                s = sum(vals[k] for k in present)
                diff = vals[parent] - s
                if diff:
                    pct = abs(diff) / max(abs(vals[parent]), 1) * 100
                    print(f"INFO  {year} {ecats[parent][0]} = {vals[parent]}, children sum = {s}, "
                          f"unassigned = {diff} ({pct:.1f}%)")

    # opening balances restated in a year's own report (see the module docstring)
    restated = defaultdict(dict)
    rpath = os.path.join(args.brf_dir, "restatements.csv")
    if os.path.exists(rpath):
        for r in csv.DictReader(open(rpath, encoding="utf-8")):
            restated[int(r["year"])][int(r["category_id"])] = int(r["amount"])

    def opening(y, prev, cats):
        """Opening balance of `cats` for year y: restated in y's report if recorded, else last year's closing."""
        closing = sum(prev.get(c, 0) for c in cats)
        rows = {c: a for c, a in restated.get(y, {}).items() if c in cats}
        if not rows:
            return closing
        value = sum(rows.values())
        if value != closing:
            print(f"INFO  RESTATED {y}: opening {'liquidity' if cats == LIQUIDITY else 'debt'} {value} in the "
                  f"{y} report vs {closing} closing in the {y - 1} report (diff {value - closing})")
        return value

    # identities between consecutive years
    tops = {cid for cid, (_, parent) in ecats.items() if parent is None}
    for y in sorted(all_states):
        if (years and y not in years) or y - 1 not in all_states or y not in all_events:
            continue
        cur, prev, ev = all_states[y], all_states[y - 1], all_events[y]
        flows = sum(a for cid, a in ev.items() if cid in tops)
        d_cash = sum(cur.get(c, 0) for c in LIQUIDITY) - opening(y, prev, LIQUIDITY)
        diff = flows - d_cash
        line = f"CASH  {y}: events {flows:>12}  vs  change in liquidity {d_cash:>12}  diff {diff:>10}"
        if abs(diff) > CASH_TOLERANCE:
            bad(line)
        else:
            print("OK    " + line)
        if FINANCING in ev:
            d_debt = sum(cur.get(c, 0) for c in DEBT) - opening(y, prev, DEBT)
            gap = ev[FINANCING] - d_debt
            print(f"{'OK' if not gap else 'INFO':5} DEBT  {y}: financing {ev[FINANCING]:>12}  vs  change in loans "
                  f"{d_debt:>12}  non-loan financing {gap:>10}")

    # property: printed total area = its parts; changes between years
    pcats_path = os.path.join(cats_dir, "property_categories.csv")
    if all_property and os.path.exists(pcats_path):
        pcats = load_cats(pcats_path)
        pkids = defaultdict(list)
        for cid, (_, parent) in pcats.items():
            if parent is not None:
                pkids[parent].append(cid)
        for y in sorted(all_property):
            if years and y not in years:
                continue
            vals = all_property[y]
            for parent, kids in pkids.items():
                present = [k for k in kids if k in vals]
                if parent in vals and present:
                    s = sum(vals[k] for k in present)
                    line = f"AREA  {y}: {pcats[parent][0]} {vals[parent]} vs parts {s}"
                    if s != vals[parent]:
                        bad(line)
                    else:
                        print("OK    " + line)
            prev = all_property.get(y - 1, {})
            for cid in sorted(set(vals) & set(prev)):
                if vals[cid] != prev[cid]:
                    print(f"INFO  PROP  {y}: {pcats[cid][0]} {prev[cid]} -> {vals[cid]}")

    print("OK" if not problems else f"{problems} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
