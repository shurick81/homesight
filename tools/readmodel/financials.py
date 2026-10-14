"""The `financial_year` read model: one document per BRF and fiscal year, built from the verified values in
annual_reviews/brfs/<orgnr>/ (financial_events, financial_states, property_states, general_states).

Served by GET /brfs/{orgnr}/financials, and the latest year is copied onto every listing of the BRF
(brf_report_* in the advertisements read model). Everything a reader would compute (totals, per-m² figures,
ratios) is computed here, once, at load time.

Conventions in the documents: amounts in SEK; costs as positive amounts; cash-flow items with their sign
(+ cash in, − cash out); per-m² figures over the bostadsrätt living area of the same year (the latest
earlier year when that year's report does not give it). Every stored value keeps its place in the report
(`values`: page and box), and the document its source URL, so any number can be checked.
"""
import csv
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
BRFS = REPO / "annual_reviews" / "brfs"
RAW = REPO / "raw" / "annual_reviews" / "brfs"

# financial_events category -> field (costs are stored negative; flipped to positive in `costs`)
INCOME = {0: "net_revenue_kr", 17: "annual_fees_kr", 18: "rents_kr", 19: "other_net_revenue_kr",
          20: "vacancy_losses_kr", 4: "other_operating_income_kr"}
COSTS = {1: "operating_kr", 16: "planned_maintenance_kr", 5: "other_external_kr", 6: "personnel_kr",
         7: "interest_paid_kr", 56: "income_tax_kr",
         57: "ground_rent_kr"}                  # tomträttsavgäld: shown on its own, but part of operating_kr
CASH_FLOW = {15: "interest_received_kr", 8: "investments_kr", 3: "financing_kr",
             13: "change_receivables_kr", 14: "change_other_receivables_kr", 9: "change_receivables_kr",
             11: "change_payables_kr", 12: "change_other_liabilities_kr", 10: "change_liabilities_kr"}
BREAKDOWN_PARENTS = {1, 2, 5, 6}           # their children form the cost breakdown


def _rows(path):
    return list(csv.DictReader(path.open(encoding="utf-8"))) if path.exists() else []


def _categories(name):
    return {int(r["id"]): (r["name"], int(r["parent"]) if r["parent"] else None)
            for r in _rows(BRFS / f"{name}_categories.csv")}


EVENT_CATS, STATE_CATS = _categories("event"), _categories("state")
PROP_CATS, GENERAL_CATS = _categories("property"), _categories("general")


def _box(r):
    vals = [r.get(k) for k in ("x", "y", "width", "height")]
    return [float(v) for v in vals] if all(vals) else None


def _source_urls(org):
    out = {}
    for meta in (RAW / org).glob("*/metadata.yaml"):
        m = yaml.safe_load(meta.open(encoding="utf-8")) or {}
        out[int(meta.parent.name)] = {"file": m.get("file"), "url": (m.get("source") or {}).get("url") or None}
    return out


CASH_TOLERANCE = 25                           # kr, as tools/brf/validate.py
LIQUIDITY, DEBT = (2, 3, 4, 5), (0, 1)
TOP_EVENTS = {c for c, (_, parent) in EVENT_CATS.items() if parent is None}

# quality label of a year, worst first; warnings say why it is not "verified"
QUALITY = ("unverified", "check_failed", "verified_with_caveats", "verified")


def _scanned_pages(pdf, pages):
    """The cited pages of `pdf` that are read by OCR (tools/brf/pdfwords.py decides as the verifier did)."""
    sys.path.insert(0, str(REPO / "tools" / "brf"))
    import pdfwords
    try:
        return {p for p in pages if pdfwords.is_scanned(str(pdf), p)}
    except Exception:                         # an unreadable file: say nothing rather than guess
        return set()


def _warnings(org, y, verified, ev, st, restated, eye, values, source, manual):
    """[{code, message}] for year y: what the checks could not establish, or found wrong."""
    out = []

    def warn(code, message):
        out.append({"code": code, "message": message})

    if not verified:
        warn("unverified", "Extracted but not yet verified against the report.")
    cur, prev = st.get(y, {}), st.get(y - 1)
    if not any(c in cur for c in LIQUIDITY):
        warn("cash_not_stored", "The report has no cash flow statement (or no liquidity figure), so cash is not "
                                "stored and the cash flow cannot be checked.")
    elif prev is None and not any(c in restated.get(y, {}) for c in LIQUIDITY):
        warn("no_opening_balance", "The previous year's report is not held, so this year's cash and debt "
                                   "movements are not checked against its closing balances.")
    else:
        opening = (sum(restated[y][c] for c in LIQUIDITY if c in restated[y])
                   if any(c in restated.get(y, {}) for c in LIQUIDITY)
                   else sum((prev or {}).get(c, 0) for c in LIQUIDITY))
        flows = sum(a for c, a in ev.get(y, {}).items() if c in TOP_EVENTS)
        diff = flows - (sum(cur.get(c, 0) for c in LIQUIDITY) - opening)
        if abs(diff) > CASH_TOLERANCE:
            warn("cash_check_failed", f"The year's cash flows do not add up to the change in liquidity (off by "
                                      f"{diff} kr): a value may be wrong or missing.")
    if eye:
        warn("checked_by_eye", f"{len(eye)} value(s) could not be matched automatically (sums over several lines, "
                               f"garbled scans) and were confirmed by eye on the page.")
    if source and source.get("file"):
        pdf = RAW / org / str(y) / source["file"]
        cited = {v["page"] for v in values}
        scanned = _scanned_pages(pdf, cited) if pdf.exists() else set()
        n = sum(1 for v in values if v["page"] in scanned)
        if n:
            warn("scanned", f"{n} of {len(values)} values were read from scanned pages by OCR; a misread digit "
                            f"that also fits the report's totals is unlikely but possible.")
    if manual:
        warn("note", str(manual).strip())
    return out


def _quality(warnings):
    codes = {w["code"] for w in warnings}
    if "unverified" in codes:
        return "unverified"
    if "cash_check_failed" in codes:
        return "check_failed"
    return "verified_with_caveats" if codes else "verified"


def _per_m2(amount, area):
    return round(amount / area, 1) if amount is not None and area else None


def brf_years(org):
    """{year: document} for one BRF."""
    prof = yaml.safe_load((BRFS / org / "profile.yaml").open(encoding="utf-8")) or {}
    verified_through = int(prof.get("verified_through") or 0)
    verified_from = int(prof.get("verified_from") or 0)     # older years held but not verified (Sjötungan)
    tables = {t: _rows(BRFS / org / f"{t}.csv")
              for t in ("financial_events", "financial_states", "property_states", "general_states")}
    years = sorted({int(r["year"]) for r in tables["financial_events"]})
    state_years = {int(r["year"]) for r in tables["financial_states"]}
    sources = _source_urls(org)

    def by_year(table, key="amount"):
        out = {}
        for r in tables[table]:
            out.setdefault(int(r["year"]), {})[int(r["category_id"])] = (
                int(r[key]) if key == "amount" else r[key])
        return out

    ev, st, pr, gs = (by_year("financial_events"), by_year("financial_states"),
                      by_year("property_states"), by_year("general_states", "value"))
    areas = {y: (p.get(8) or p.get(12)) for y, p in pr.items() if p.get(8) or p.get(12)}
    restated = {}
    for r in _rows(BRFS / org / "restatements.csv"):
        restated.setdefault(int(r["year"]), {})[int(r["category_id"])] = int(r["amount"])
    eye = {(r["table"], int(r["year"]), int(r["category_id"])) for r in _rows(BRFS / org / "checked_by_eye.csv")}
    manual = {int(k): v for k, v in (prof.get("warnings") or {}).items()}

    docs = {}
    for y in years:
        e, s, p, g = ev.get(y, {}), st.get(y, {}), pr.get(y, {}), gs.get(y, {})
        area_year = max((ay for ay in areas if ay <= y), default=None) or min(areas, default=None)
        area = areas.get(area_year)
        income = {f: e[c] for c, f in INCOME.items() if c in e}
        costs = {f: -e[c] for c, f in COSTS.items() if c in e}
        cash_flow = {f: e[c] for c, f in CASH_FLOW.items() if c in e}
        loans = [s[c] for c in (0, 1) if c in s]
        liquidity = [s[c] for c in (2, 3, 4, 5) if c in s]
        balance = {
            "loans_kr": sum(loans) if loans else None,
            "loans_short_term_kr": s.get(0), "loans_long_term_kr": s.get(1),
            "cash_kr": sum(liquidity) if liquidity else None,
            "equity_kr": s.get(6), "total_assets_kr": s.get(7),
        }
        breakdown = [{"category_id": c, "category": EVENT_CATS[c][0], "parent": EVENT_CATS[EVENT_CATS[c][1]][0],
                      "amount_kr": -a}
                     for c, a in sorted(e.items()) if EVENT_CATS.get(c, (None, None))[1] in BREAKDOWN_PARENTS]
        fees, interest = income.get("annual_fees_kr"), costs.get("interest_paid_kr")
        doc = {
            "id": f"fy-{y}", "type": "financial_year", "orgnr": org, "name": prof.get("name"),
            "fiscal_year": y, "period": g.get(0),
            "verified": verified_from <= y <= verified_through,
            "opening_balance_held": (y - 1) in state_years,
            "report": sources.get(y),
            "income": income, "costs": costs, "cash_flow": cash_flow, "balance_sheet": balance,
            "cost_breakdown": breakdown,
            "property": {
                "apartments": p.get(0), "rental_apartments": p.get(1), "premises": p.get(2),
                "living_area_m2": area, "living_area_year": area_year, "total_area_m2": p.get(7),
            },
            "per_m2": {
                "annual_fees_kr": _per_m2(fees, area),
                "operating_costs_kr": _per_m2(costs.get("operating_kr"), area),
                "interest_paid_kr": _per_m2(interest, area),
                "loans_kr": _per_m2(balance["loans_kr"], area),
                "cash_kr": _per_m2(balance["cash_kr"], area),
                "ground_rent_kr": _per_m2(costs.get("ground_rent_kr"), area),
            },
            "ratios": {
                "interest_share_of_annual_fees_pct": round(100 * interest / fees, 1) if interest and fees else None,
                "annual_fees_share_of_net_revenue_pct":
                    round(100 * fees / income["net_revenue_kr"], 1) if fees and income.get("net_revenue_kr") else None,
            },
            "board": {"chair": g.get(1), "members": g.get(3), "auditors": g.get(5)},
            "land": g.get(13),
            "values": [
                {"table": t, "category_id": int(r["category_id"]),
                 "category": cats.get(int(r["category_id"]), ("?",))[0],
                 "value": r.get("amount", r.get("value")), "page": int(r["page"]), "box": _box(r)}
                for t, cats in (("financial_events", EVENT_CATS), ("financial_states", STATE_CATS),
                                ("property_states", PROP_CATS), ("general_states", GENERAL_CATS))
                for r in tables[t] if int(r["year"]) == y
            ],
        }
        for v in doc["values"]:
            v["checked_by_eye"] = (v["table"], y, v["category_id"]) in eye
        doc["warnings"] = _warnings(org, y, doc["verified"], ev, st, restated,
                                    [v for v in doc["values"] if v["checked_by_eye"]], doc["values"],
                                    doc["report"], manual.get(y))
        doc["quality"] = _quality(doc["warnings"])
        docs[y] = doc
    return docs


def all_brfs():
    """{orgnr: {year: document}} for every BRF with a profile and extracted values."""
    return {p.parent.name: brf_years(p.parent.name) for p in sorted(BRFS.glob("*/profile.yaml"))
            if (p.parent / "financial_events.csv").exists()}


def build():
    return [d for years in all_brfs().values() for d in years.values()]


def latest(years):
    """The document a listing should carry: the latest verified year, else the latest extracted."""
    verified = [y for y, d in years.items() if d["verified"]]
    return years[max(verified)] if verified else (years[max(years)] if years else None)
