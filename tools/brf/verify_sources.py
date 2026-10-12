#!/usr/bin/env python3
"""Check every extracted value against the PDF it cites.

Usage:
    verify_sources.py annual_reviews/brfs/<orgnr> [--years 2016-2025] [--crosscheck] [--fix] [--scale 1]

For each row of financial_events.csv / financial_states.csv / property_states.csv:
    OK          the amount is printed inside the cited box (sign ignored: notes print costs positive)
    SUM         the amounts printed inside the cited box add up to the value (summed categories)
    MOVED       the amount is on the cited page but outside the cited box
    WRONG-DOC   the row cites the annual report of another fiscal year; take it from the year's own report
    COLUMN      the amount is found, but another amount is printed left of it on the same line:
                it was probably read from the previous-year (comparison) column
    PAGE n      the amount is not on the cited page but on page n of the same file
    MISSING     the amount is not found in the file (wrong value, or OCR misread: check the image)
    NO-SOURCE   the cited file is not under raw/
    EYE         not machine-readable, but listed in <brf>/checked_by_eye.csv as confirmed on the page image
For general_states.csv: OK when every name/value token is found on the cited page, else TEXT?.

Per year it also looks in the year's own report for the two totals of the cash view (the waterfall in an
analysis page): the sum of the top-level financial_events ("Årets kassaflöde") and the year-end liquidity
L = states 2+3+4+5 ("Likvida medel vid årets slut"). PRINTED = found within ±2 kr (reports round each line);
NOT PRINTED is a warning: the report may not print it, or a top-level event is missing or wrong.

--crosscheck  also look for each value in the *next* year's report (its comparison column).
              CONFIRMED means two documents print the same number; restatements show up here.
--fix         rewrite boxes (and pages, for PAGE n with a single match) to the located position.
--scale S     the CSV boxes are in units of S x PDF points (legacy viewer pixels: 1.5). With --fix
              every box in the files is converted to PDF points.
"""
import argparse
import csv
import difflib
import glob
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict

import yaml

from pdfwords import numbers, page_count, words

FIN = ("financial_events.csv", "financial_states.csv")
PROPERTY = "property_states.csv"  # counts and areas: several quantities share a line, so no column test
NUMERIC = FIN + (PROPERTY,)
WORDS = {"en": 1, "ett": 1, "två": 2, "tre": 3, "fyra": 4, "fem": 5, "sex": 6, "sju": 7, "åtta": 8,
         "nio": 9, "tio": 10, "elva": 11, "tolv": 12}
BOX = ("x", "y", "width", "height")


def parse_years(spec):
    if not spec:
        return None
    a, _, b = spec.partition("-")
    return set(range(int(a), int(b or a) + 1))


def raw_index(repo, orgnr):
    """basename -> path, and fiscal year -> annual report path, from raw/*/brfs/<orgnr>/."""
    files, reports = {}, {}
    for meta in glob.glob(os.path.join(repo, "raw", "*", "brfs", orgnr, "*", "metadata.yaml")):
        d = os.path.dirname(meta)
        m = yaml.safe_load(open(meta, encoding="utf-8")) or {}
        for pdf in glob.glob(os.path.join(d, "*.pdf")):
            files[os.path.basename(pdf)] = pdf
        if "/annual_reviews/" in d and m.get("file"):
            fy = m.get("fiscal_year") or int(os.path.basename(d))
            reports[int(fy)] = os.path.join(d, m["file"])
    return files, reports


def box_of(r, scale):
    try:
        x, y, w, h = (float(r[k]) / scale for k in BOX)
    except ValueError:
        return None
    return {"x0": x, "top": y, "x1": x + w, "bottom": y + h}


def near(a, b, tol=6):
    return not (a["x1"] + tol < b["x0"] or b["x1"] + tol < a["x0"] or
                a["bottom"] + tol < b["top"] or b["bottom"] + tol < a["top"])


def dist(a, b):
    return abs((a["x0"] + a["x1"]) - (b["x0"] + b["x1"])) / 2 + abs((a["top"] + a["bottom"]) - (b["top"] + b["bottom"])) / 2


def fmt_box(b):
    return [str(round(b["x0"])), str(round(b["top"])), str(round(b["x1"] - b["x0"])), str(round(b["bottom"] - b["top"]))]


def find_sum(nums, target, box, max_lines=12, max_terms=6):
    """Find 2..max_terms amounts in one column, within max_lines consecutive lines, that add up to
    target (a category that sums several note lines). Prefer the window closest to `box`.
    Returns the union box of the terms, or None."""
    full = [n for n in nums if n["maximal"]]
    cols = defaultdict(list)
    for n in full:  # amounts are right-aligned: group by right edge
        cols[round(n["x1"] / 12)].append(n)
    best = None
    for col in cols.values():
        col.sort(key=lambda n: n["top"])
        for i in range(len(col)):
            window = col[i:i + max_lines]
            first = window[0]
            # subsets that start at window[0], so each window is a distinct start line
            rest = window[1:]
            stack = [(0, abs(first["value"]), [first])]
            while stack:
                k, total, terms = stack.pop()
                if total == target and len(terms) > 1:
                    u = {"x0": min(t["x0"] for t in terms), "top": min(t["top"] for t in terms),
                         "x1": max(t["x1"] for t in terms), "bottom": max(t["bottom"] for t in terms)}
                    d = dist(u, box) if box else 0
                    if best is None or d < best[0]:
                        best = (d, u)
                    continue
                if total >= target or len(terms) >= max_terms:
                    continue
                for j in range(k, len(rest)):
                    stack.append((j + 1, total + abs(rest[j]["value"]), terms + [rest[j]]))
    return best[1] if best else None


def later_column(nums, hit):
    """True when another amount (>= 100, so not a note number) is printed left of `hit` on the
    same line: the value was then taken from a comparison column, not the fiscal year's own."""
    return any(n["maximal"] and abs(n["value"]) >= 100 and abs(n["top"] - hit["top"]) < 4
               and n["x1"] < hit["x0"] for n in nums)


def check_amount(path, page, amount, box, second_pass=False, columns=True):
    """Return (status, located box or None, page)."""
    target = abs(amount)
    ws, ocr = words(path, page, second_pass)
    nums = numbers(ws)
    if target <= 12:  # small counts are often written out: "nio hyresrätter"
        folded = {fold(k): n for k, n in WORDS.items()}
        nums = nums + [{**w, "value": folded[fold(w["text"]).strip(",.")], "maximal": True}
                       for w in ws if fold(w["text"]).strip(",.") in folded]
    hits = [n for n in nums if abs(n["value"]) == target]
    if hits:
        if box:
            hits.sort(key=lambda n: (not near(n, box), not n["maximal"], dist(n, box)))
            status = "OK" if near(hits[0], box) else "MOVED"
        else:  # no box yet: prefer the fiscal year's own (first) column
            hits.sort(key=lambda n: (later_column(nums, n), not n["maximal"], n["top"]))
            status = "MOVED"
        if columns and later_column(nums, hits[0]):
            status = "COLUMN"
        return status, hits[0], page
    s = find_sum(nums, target, box)
    if s:
        return "SUM", s, page
    if ocr and not second_pass:
        st = check_amount(path, page, amount, box, second_pass=True, columns=columns)
        if st[0] != "MISSING" and not st[0].startswith("PAGE"):
            return st
    other = []
    for p in range(1, page_count(path) + 1):
        if p != page and any(abs(n["value"]) == target for n in numbers(words(path, p)[0])):
            other.append(p)
    if len(other) == 1:
        n = next(n for n in numbers(words(path, other[0])[0]) if abs(n["value"]) == target)
        return f"PAGE {other[0]}", n, other[0]
    if other:
        return "PAGE " + "/".join(map(str, other)), None, page
    return "MISSING", None, page


def fold(s):
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def check_text(path, page, value, second_pass=False):
    """Every name/word of the value (length >= 3) must be on the page, allowing OCR typos.
    Dates only need their year: reports print fiscal years in many formats."""
    ws, ocr = words(path, page, second_pass)
    keyed = [(re.sub(r"[^\w-]", "", fold(w["text"])), w) for w in ws]
    vocab = {k for k, _ in keyed} | {y for k, _ in keyed for y in re.findall(r"(?:19|20)\d\d", k)}
    value = re.sub(r"((?:19|20)\d\d)-\d\d-\d\d", r"\1", re.sub(r"\(.*?\)", "", value))
    want = [t for t in re.findall(r"[\w-]+", fold(value)) if len(t) >= 3]
    found, miss = [], []
    for t in want:
        m = t if t in vocab else (difflib.get_close_matches(t, vocab, n=1, cutoff=0.8) or [None])[0]
        if m is None:
            miss.append(t)
        else:
            found += [w for k, w in keyed if k == m or m in re.findall(r"(?:19|20)\d\d", k)][:1]
    if miss and ocr and not second_pass:
        st = check_text(path, page, value, second_pass=True)
        if len(st[0]) < len("TEXT? " + ",".join(miss)):
            return st
    loc = None
    if found and not miss:
        loc = {"x0": min(w["x0"] for w in found), "top": min(w["top"] for w in found),
               "x1": max(w["x1"] for w in found), "bottom": max(w["bottom"] for w in found)}
    return ("OK" if not miss else "TEXT? " + ",".join(miss)), loc, page


def amount_in_report(path, amount, cache):
    if path not in cache:
        vals = set()
        for p in range(1, page_count(path) + 1):
            vals.update(abs(n["value"]) for n in numbers(words(path, p)[0]))
        cache[path] = vals
    return abs(amount) in cache[path]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("brf_dir")
    ap.add_argument("--years")
    ap.add_argument("--crosscheck", action="store_true")
    ap.add_argument("--fix", action="store_true")
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--quiet", action="store_true", help="print only rows that are not OK/SUM")
    args = ap.parse_args()

    brf_dir = os.path.normpath(args.brf_dir)
    orgnr = os.path.basename(brf_dir)
    repo = os.path.abspath(os.path.join(brf_dir, "..", "..", ".."))
    files, reports = raw_index(repo, orgnr)
    years = parse_years(args.years)
    report_year = {os.path.basename(p): y for y, p in reports.items()}
    eye = set()  # values a person confirmed on the page image where OCR cannot (checked_by_eye.csv)
    eye_path = os.path.join(brf_dir, "checked_by_eye.csv")
    if os.path.exists(eye_path):
        eye = {(r["table"], r["year"], r["category_id"]) for r in csv.DictReader(open(eye_path, encoding="utf-8"))}
    summary = defaultdict(Counter)
    xcache = {}
    unconfirmed = []

    for fname in NUMERIC + ("restatements.csv", "general_states.csv"):   # restatements: see validate.py
        path = os.path.join(brf_dir, fname)
        if not os.path.exists(path):
            continue
        with open(path, newline="", encoding="utf-8") as f:
            rd = csv.DictReader(f)
            header, rows = rd.fieldnames, list(rd)
        for r in rows:
            in_scope = not years or int(r["year"]) in years
            box = box_of(r, args.scale)
            if args.fix and args.scale != 1 and box:
                r.update(zip(BOX, fmt_box(box)))
            if not in_scope:
                continue
            pdf = files.get(r["file"])
            if not pdf:
                status, loc, page = "NO-SOURCE", None, int(r["page"])
            elif fname == "general_states.csv":
                if not r["value"]:
                    continue
                status, loc, page = check_text(pdf, int(r["page"]), r["value"])
            else:
                status, loc, page = check_amount(pdf, int(r["page"]), int(r["amount"]), box,
                                                 columns=fname != PROPERTY)
            # a value confirmed on the page image wins over OCR, also when OCR finds the number elsewhere (PAGE)
            if status.split()[0] in ("MISSING", "TEXT?", "PAGE") and (fname[:-4], r["year"], r["category_id"]) in eye:
                status = "EYE"
            if fname in NUMERIC and report_year.get(r["file"], int(r["year"])) != int(r["year"]):
                status = "WRONG-DOC"  # cites another fiscal year's report (its comparison column)
            note = ""
            found = status in ("OK", "MOVED", "COLUMN") or (status.startswith("PAGE ") and loc is not None)
            if args.crosscheck and fname in NUMERIC and found:
                nxt = reports.get(int(r["year"]) + 1)
                if nxt and report_year.get(r["file"]) == int(r["year"]):
                    if amount_in_report(nxt, int(r["amount"]), xcache):
                        note = f"  confirmed by {os.path.basename(nxt)}"
                        summary[r["year"]]["confirmed"] += 1
                    else:
                        note = f"  NOT in {os.path.basename(nxt)}"
                        unconfirmed.append((r["year"], fname, r["category_id"], r[header[2]]))
            summary[r["year"]][status.split()[0]] += 1
            if args.fix and loc is not None and (fname in NUMERIC or box is None):
                r.update(zip(BOX, fmt_box(loc)))
                r["page"] = str(page)
            if not args.quiet or status.split()[0] not in ("OK", "SUM", "EYE"):
                val = r[header[2]]
                print(f"{status:10} {fname[:-4]:16} {r['year']} cat={r['category_id']:>2} {val[:60]:>14}  "
                      f"{r['file']} p{r['page']}{note}")
        if args.fix:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=header, lineterminator="\n")
                w.writeheader()
                w.writerows(rows)

    # the two totals of the cash view must be printed in the year's own report
    printed_warn = 0
    tops = {r["id"] for r in csv.DictReader(open(os.path.join(os.path.dirname(brf_dir), "event_categories.csv"),
                                                   encoding="utf-8")) if not r["parent"]}
    flows, liq = defaultdict(int), defaultdict(int)
    for r in csv.DictReader(open(os.path.join(brf_dir, "financial_events.csv"), encoding="utf-8")):
        if r["category_id"] in tops:
            flows[int(r["year"])] += int(r["amount"])
    for r in csv.DictReader(open(os.path.join(brf_dir, "financial_states.csv"), encoding="utf-8")):
        if r["category_id"] in ("2", "3", "4", "5"):
            liq[int(r["year"])] += int(r["amount"])
    print()
    for y in sorted(set(flows) | set(liq)):
        if (years and y not in years) or y not in reports:
            continue
        rep_path = reports[y]
        printed = {}
        for p in range(1, page_count(rep_path) + 1):
            for n in numbers(words(rep_path, p)[0]):
                if n["maximal"]:
                    printed.setdefault(abs(n["value"]), p)
        for label, val in (("årets kassaflöde", flows.get(y)), ("likvida medel vid årets slut", liq.get(y))):
            if val is None:
                continue
            hit = next(((abs(val) + d, d) for d in (0, 1, -1, 2, -2) if abs(val) + d in printed), None)
            if hit:
                print(f"PRINTED     {y} {label:30} {val:>13}  p{printed[hit[0]]}" + (f" (diff {hit[1]:+d} kr)" if hit[1] else ""))
            else:
                printed_warn += 1
                print(f"NOT PRINTED {y} {label:30} {val:>13}  not in {os.path.basename(rep_path)}")

    print("\nyear  " + "  ".join(f"{k:>9}" for k in ("OK", "SUM", "EYE", "MOVED", "COLUMN", "WRONG-DOC", "PAGE", "MISSING", "TEXT?", "NO-SOURCE", "confirmed")))
    for y in sorted(summary):
        c = summary[y]
        print(f"{y}  " + "  ".join(f"{c[k]:>9}" for k in ("OK", "SUM", "EYE", "MOVED", "COLUMN", "WRONG-DOC", "PAGE", "MISSING", "TEXT?", "NO-SOURCE", "confirmed")))
    bad = sum(c[k] for c in summary.values() for k in ("MOVED", "COLUMN", "WRONG-DOC", "PAGE", "MISSING", "TEXT?", "NO-SOURCE"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
