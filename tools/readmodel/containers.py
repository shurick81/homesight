"""Every container of the read model: partition key, indexing policy and the builder of its documents.
tools/cloud/provision.py creates a database from this and loads it.

Indexing follows the data repo's rule (index what a filter or sort reads, nothing else): the
advertisements policy is generated from the API's own SORTS and FILTERS, so a new sort or filter is
indexed by the next provision. Everything else in a document (photos, attributes, per-year history) is
stored but not indexed, which keeps writes cheap.
"""
import importlib.util
import sys
from pathlib import Path

from . import brfs, financials, geo, sales_stats

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "api"))
_spec = importlib.util.spec_from_file_location("api_advertisements", REPO / "api" / "advertisements.py")
api_ads = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(api_ads)


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
    # one BRF summary (type brf_summary) + one document per fiscal year (type financial_year) per BRF;
    # small, so everything is indexed
    "brfs": {"partition_key": "/orgnr", "indexing_policy": None,
             "build": lambda: brfs.build() + financials.build()},
    # the geo levels: one finished response row each, `order` = the data API's sort order; tiny
    "kommuns": {"partition_key": "/kommun", "indexing_policy": None, "build": geo.kommuns},
    "areas": {"partition_key": "/kommun", "indexing_policy": None, "build": geo.areas},
    "cities": {"partition_key": "/city", "indexing_policy": None, "build": geo.cities},
    # one finished /sales/stats/avg-price-per-sqm response per (year, rooms) combination, read by id
    "sales_stats": {"partition_key": "/id", "indexing_policy": None,
                    "build": sales_stats.build},
}
