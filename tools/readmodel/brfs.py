"""The `brfs` read model behind GET /brfs and GET /brfs/{orgnr}: one "brf" document per BRF.

A port of the data repo's refresh_brfs: one row per BRF with at least one current listing, collapsed from
its listings (the brf_* block, kommun, område, city and the geo benchmarks are the same on every listing of a
BRF; active_* summarise the listings). homesight adds:

- the verified layer: brf_report_* (latest verified year), brf_land_tenure and brf_stambyte_*, already on
  the listings, and `report`: the BRF's summary from annual_reviews/brfs/index.csv (served by
  GET /brfs/{orgnr} only);
- a row for every BRF with an extracted report even when nothing is for sale, built from its Booli details
  and sales (tools/readmodel/advertisements.py: unlisted_brf);
- a deterministic choice of the listing a row takes its område from (_representative);
- name_rank / kommun_rank: the row's place in the data repo's collation (Postgres en_US), which the name and
  kommun sorts read, since Cosmos orders strings bytewise.
"""
import csv
from collections import Counter
from pathlib import Path

from . import advertisements, geo, stats

REPO = Path(__file__).resolve().parents[2]
INDEX_CSV = REPO / "annual_reviews" / "brfs" / "index.csv"

INT_FIELDS = {"built_from", "built_to", "apartments", "living_area_m2", "extracted_fy", "verified_through"}
LIST_FIELDS = {"stamspolning_done"}   # ';'-separated years in the CSV
GEO_FIELDS = ("kommun", "area_name", "city")
GEO_PREFIXES = ("area_", "kommun_", "city_")


def _value(field, raw):
    raw = raw.strip()
    if raw == "":
        return None
    if field in INT_FIELDS:
        return int(raw)
    if field in LIST_FIELDS:
        return [int(y) for y in raw.split(";")]
    return raw


def summaries():
    """orgnr -> the BRF's summary from index.csv (the BRFs with extracted reports)."""
    with open(INDEX_CSV, encoding="utf-8") as f:
        return {row["orgnr"]: {k: _value(k, v) for k, v in row.items()} for row in csv.DictReader(f)}


def _row_fields(doc):
    """The columns refresh_brfs copies from a listing: the BRF block (not Booli's internal id), kommun,
    område, city and the geo benchmarks."""
    return {k: v for k, v in doc.items()
            if (k.startswith("brf_") and k != "brf_booli_id") or k in GEO_FIELDS or k.startswith(GEO_PREFIXES)}


def _representative(listings):
    """The listing a BRF's row takes its område, kommun and geo benchmarks from: one in the område where most
    of its listings are, of its most common type of home, lowest listing id first. (The data repo takes an
    arbitrary listing, (array_agg(c))[1], which differs for a BRF whose listings span områden.)"""
    area = Counter(a["area_name"] for a in listings)
    form = Counter(a["object_form"] for a in listings)
    return min(listings, key=lambda a: (-area[a["area_name"]], -form[a["object_form"]], int(a["listing_id"])))


def _dense_rank(rows, field):
    keys = sorted({geo.collation_key(r[field]) for r in rows if r[field] is not None})
    rank = {k: i for i, k in enumerate(keys)}
    for r in rows:
        r[f"{field}_rank"] = rank[geo.collation_key(r[field])] if r[field] is not None else None


def build():
    ads = geo.advertisements()
    by_org = {}
    for a in sorted(ads, key=lambda a: int(a["listing_id"])):
        if a["brf_org_number"] is not None:
            by_org.setdefault(a["brf_org_number"], []).append(a)
    rows = []
    for org, listings in by_org.items():
        row = _row_fields(_representative(listings))
        prices = [a["kr_per_m2"] for a in listings if a["kr_per_m2"] is not None]
        row.update(active_listing_count=len(listings), active_min_kr_per_m2=min(prices, default=None),
                   active_max_kr_per_m2=max(prices, default=None),
                   active_avg_kr_per_m2=stats.round_fraction(stats.exact_mean(prices)))
        rows.append(row)

    # BRFs with an extracted report and no listing
    reports = summaries()
    booli_id = {b["org_number"]: bid for bid, b in advertisements.staged()[0].items() if b["org_number"]}
    for org in sorted(set(reports) - set(by_org)):
        if org not in booli_id:
            continue   # not on Booli: no market data to build a row from
        doc = advertisements.unlisted_brf(booli_id[org])
        geo.copy_down([doc], geo.levels())
        rows.append(dict(_row_fields(doc), active_listing_count=0, active_min_kr_per_m2=None,
                         active_max_kr_per_m2=None, active_avg_kr_per_m2=None))

    for r in rows:
        r["brf_report_available"] = r["brf_report_fiscal_year"] is not None
    for field in ("brf_name", "kommun"):
        _dense_rank(rows, field)
    for r in rows:
        r["name_rank"], r["kommun_rank"] = r.pop("brf_name_rank"), r.pop("kommun_rank")
    return [dict(geo._jsonb_order(r), id="brf", type="brf", orgnr=r["brf_org_number"],
                 report=reports.get(r["brf_org_number"]))
            for r in rows]
