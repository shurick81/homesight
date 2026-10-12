"""Every container of the read model: partition key, indexing policy and the builder of its documents.
tools/cloud/provision.py creates a database from this and loads it.

Indexing follows the data repo's rule (index what a filter or sort reads, nothing else): the
advertisements and brfs policies are generated from the API's own SORTS and FILTERS, so a new sort or filter is
indexed by the next provision. Everything else in a document (photos, attributes, per-year history) is
stored but not indexed, which keeps writes cheap.
"""
import importlib.util
import sys
from pathlib import Path

from . import brfs, financials, geo, sales_stats

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "api"))


def _api_module(name):
    spec = importlib.util.spec_from_file_location(f"api_{name}", REPO / "api" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api_ads = _api_module("advertisements")
api_brfs = _api_module("brfs")


def brfs_indexing_policy():
    """The /brfs sorts and filters, `type` (rows and fiscal years share the container), and a composite index
    per sort direction with brf_org_number ASC as tiebreak."""
    sort_fields = sorted({f for f, _ in api_brfs.SORTS.values()})
    fields = sorted(set(sort_fields) | {f for _, f, _ in api_brfs.FILTERS.values()}
                    | {api_brfs.TIEBREAK_FIELD, "type"})
    tiebreak = {"path": f"/{api_brfs.TIEBREAK_FIELD}", "order": "ascending"}
    return {
        "indexingMode": "consistent",
        "automatic": True,
        "includedPaths": [{"path": f"/{f}/?"} for f in fields],
        "excludedPaths": [{"path": "/*"}],
        "compositeIndexes": [[{"path": f"/{f}", "order": order}, tiebreak]
                             for f in sort_fields for order in ("ascending", "descending")],
    }


def advertisements_indexing_policy():
    sort_fields = sorted({f for f, _ in api_ads.SORTS.values()})
    fields = sorted(set(sort_fields) | {f for _, f, _ in api_ads.FILTERS.values()} | {api_ads.TIEBREAK_FIELD})
    tiebreak = {"path": f"/{api_ads.TIEBREAK_FIELD}", "order": "descending"}
    return {
        "indexingMode": "consistent",
        "automatic": True,
        "includedPaths": [{"path": f"/{f}/?"} for f in fields],
        "excludedPaths": [{"path": "/*"}],
        "spatialIndexes": [{"path": f"/{api_ads.SPATIAL_FIELD}/*", "types": ["Point"]}],
        # ORDER BY <field> ASC|DESC, listing_id DESC needs a composite index per direction
        "compositeIndexes": [[{"path": f"/{f}", "order": order}, tiebreak]
                             for f in sort_fields for order in ("ascending", "descending")],
    }


CONTAINERS = {
    "advertisements": {"partition_key": "/kommun", "indexing_policy": advertisements_indexing_policy(),
                       "build": geo.advertisements},
    # one "brf" row per BRF (GET /brfs, /brfs/{orgnr}) + one "financial_year" document per BRF and verified
    # year (GET /brfs/{orgnr}/financials)
    "brfs": {"partition_key": "/orgnr", "indexing_policy": brfs_indexing_policy(),
             "build": lambda: brfs.build() + financials.build()},
    # the geo levels: one finished response row each, `order` = the data API's sort order; tiny
    "kommuns": {"partition_key": "/kommun", "indexing_policy": None, "build": geo.kommuns},
    "areas": {"partition_key": "/kommun", "indexing_policy": None, "build": geo.areas},
    "cities": {"partition_key": "/city", "indexing_policy": None, "build": geo.cities},
    # one finished /sales/stats/avg-price-per-sqm response per (year, rooms) combination, read by id
    "sales_stats": {"partition_key": "/id", "indexing_policy": None,
                    "build": sales_stats.build},
}
