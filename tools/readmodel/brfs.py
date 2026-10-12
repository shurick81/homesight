"""The `brfs` read model behind GET /brfs and GET /brfs/{orgnr}: one summary document per BRF, from
annual_reviews/brfs/index.csv (the verified annual-report data)."""
import csv
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
INDEX_CSV = REPO / "annual_reviews" / "brfs" / "index.csv"

INT_FIELDS = {"built_from", "built_to", "apartments", "living_area_m2", "extracted_fy", "verified_through"}
LIST_FIELDS = {"stamspolning_done"}   # ';'-separated years in the CSV


def _value(field, raw):
    raw = raw.strip()
    if raw == "":
        return None
    if field in INT_FIELDS:
        return int(raw)
    if field in LIST_FIELDS:
        return [int(y) for y in raw.split(";")]
    return raw


def build():
    with open(INDEX_CSV, encoding="utf-8") as f:
        return [dict({k: _value(k, v) for k, v in row.items()},
                     id="summary", type="brf_summary", source="annual_reviews/brfs/index.csv")
                for row in csv.DictReader(f)]
