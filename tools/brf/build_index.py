#!/usr/bin/env python3
"""Build annual_reviews/brfs/index.csv: one row per BRF in the dataset, summarising what is held for it.

Usage: build_index.py   (reads every annual_reviews/brfs/<orgnr>/profile.yaml; no hand-edited columns)
"""
import csv, datetime, re

import yaml
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BRFS = ROOT / "annual_reviews/brfs"
INDEX = BRFS / "index.csv"
COLS = ["orgnr", "name", "municipality", "built_from", "built_to", "built_source", "land_tenure", "apartments", "living_area_m2",
        "fiscal_year", "reports_held", "extracted_fy", "verified_through",
        "stambyte_last", "stambyte_next", "stambyte_next_status", "stambyte_next_confidence",
        "stamspolning_done", "stamspolning_next", "stamspolning_regular"]
REGULAR_GAP = 6   # stamspolning is recommended every 3–5 years; a gap above 6 is not regular

def rows_of(org, name):
    p = BRFS / org / name
    return list(csv.DictReader(p.open(encoding="utf-8"))) if p.exists() else []

def stambyte(sb):
    """Summary columns from the `stambyte` block of profile.yaml (empty when there is none)."""
    if not sb:
        return {}
    last, nxt = sb.get("last") or {}, sb.get("next") or {}
    span = f"{nxt.get('from')}–{nxt.get('to')}" if nxt.get("to") != nxt.get("from") else str(nxt.get("from"))
    kind = last.get("kind", "")
    lastv = "none" if last.get("years") is None else str(last["years"]) + ("" if kind == "stambyte" else f" ({kind})")
    return {"stambyte_last": lastv,
            "stambyte_next": span if nxt.get("from") else "",
            "stambyte_next_status": nxt.get("status", ""), "stambyte_next_confidence": nxt.get("confidence", "")}

def years_in(value):
    """All years in a printed build year, as integers: '1966-68' -> [1966, 1968], '1964 och 1966' -> [1964, 1966]."""
    out = []
    for m in re.finditer(r"((?:19|20)\d\d)(?:\s*[-–]\s*(\d\d)(?!\d))?", value):
        out.append(int(m.group(1)))
        if m.group(2):
            out.append(int(m.group(1)[:2] + m.group(2)))
    return out

def built(org):
    """First and last build year from the reports (general_states 11 Byggnadsår, else 12 Värdeår), and its source."""
    vals = {}
    for r in rows_of(org, "general_states.csv"):
        if r["category_id"] in ("11", "12"):
            vals.setdefault(r["category_id"], []).append((int(r["year"]), r["value"]))
    for cat, label in (("11", "byggnadsår"), ("12", "värdeår")):
        if cat in vals:
            year, value = max(vals[cat])
            ys = years_in(value)
            if ys:
                return min(ys), max(ys), f"report {label} ({value}, FY{year})"
    return "", "", ""

def land_tenure(org):
    """äganderätt / tomträtt / arrende from the latest general_states 13 Markinnehav (stored as printed)."""
    vals = [(int(r["year"]), r["value"]) for r in rows_of(org, "general_states.csv") if r["category_id"] == "13"]
    if not vals:
        return ""
    v = max(vals)[1].lower()
    if "friköp" in v:            # a tomträtt that has been bought out is owned
        return "äganderätt"
    for key in ("tomträtt", "arrende"):
        if key in v:
            return key
    if "äganderätt" in v or "äger" in v or "ägs" in v or "mark" in v:   # "Ingående anskaffningsvärde mark": land is an asset
        return "äganderätt"
    return ""

def size(org):
    """Apartments (property_states 0) and living area (8 Bostadsrättsyta, else 12 Bostadsyta) of the latest year."""
    latest = {}
    for r in rows_of(org, "property_states.csv"):
        if int(r["year"]) >= latest.get(r["category_id"], (0, ""))[0]:
            latest[r["category_id"]] = (int(r["year"]), r["amount"])
    area = latest.get("8") or latest.get("12") or (0, "")
    return {"apartments": latest.get("0", (0, ""))[1], "living_area_m2": area[1]}

def stamspolning(org):
    """Years done, next planned and a regularity verdict from the stamspolning rows of pipe_works.csv."""
    if not (BRFS / org / "pipe_works.csv").exists():
        return {}
    now = datetime.date.today().year
    rows = [r for r in rows_of(org, "pipe_works.csv") if r["kind"] == "stamspolning"]
    done = sorted({int(r["year_to"]) for r in rows if r["status"] == "done"})
    planned = sorted({int(r["year_from"]) for r in rows if r["status"] in ("planned", "decided") and int(r["year_from"]) >= now})
    seq = done + [y for y in planned if not done or y > done[-1]]
    gaps = [b - a for a, b in zip(seq, seq[1:])]
    if not done:
        verdict = "planned only" if planned else "none recorded"
    elif len(seq) >= 2 and max(gaps) <= REGULAR_GAP and (now - done[-1] <= REGULAR_GAP or planned):
        verdict = "regular"
    elif len(done) >= 2:
        verdict = "lapsed" if now - done[-1] > REGULAR_GAP else "irregular"
    else:
        verdict = "once"
    return {"stamspolning_done": ";".join(map(str, done)), "stamspolning_next": ";".join(map(str, planned)),
            "stamspolning_regular": verdict}

def span(years):
    """Contiguous runs of years: [2019, 2020, 2023, 2024, 2025] -> '2019–2020;2023–2025'."""
    runs = []
    for y in years:
        if runs and y == runs[-1][1] + 1:
            runs[-1][1] = y
        else:
            runs.append([y, y])
    return ";".join(str(a) if a == b else f"{a}–{b}" for a, b in runs)

rows = []
for p in sorted(BRFS.glob("*/profile.yaml")):
    org, prof = p.parent.name, yaml.safe_load(p.open(encoding="utf-8")) or {}
    held = sorted(int(d.name) for d in (ROOT / "raw/annual_reviews/brfs" / org).glob("[0-9]*") if any(d.glob("*.pdf")))
    done = sorted({int(x["year"]) for x in rows_of(org, "financial_events.csv")})
    row = {"orgnr": org, "name": prof.get("name", ""), "municipality": prof.get("municipality", ""),
           "fiscal_year": prof.get("fiscal_year", ""), "reports_held": span(held),
           "extracted_fy": str(done[-1]) if done else "", "verified_through": prof.get("verified_through") or ""}
    row["built_from"], row["built_to"], row["built_source"] = built(org)
    row["land_tenure"] = land_tenure(org)
    row.update(size(org))
    row.update(stambyte(prof.get("stambyte")))
    row.update(stamspolning(org))
    rows.append(row)
with INDEX.open("w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, COLS, restval=""); w.writeheader(); w.writerows(rows)
print(len(rows), "BRFs written to", INDEX.relative_to(ROOT))

import build_coverage                              # extraction/verification changes the coverage table too
out = build_coverage.build()[0]
print("coverage table written to", out.relative_to(ROOT))
