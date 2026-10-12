"""The `sales_stats` read model: GET /sales/stats/avg-price-per-sqm, one finished response per filter combination.

The data repo stores the `analytical` rollup (sum and count of kr/m² per sold year and room count) and
re-aggregates the matching cells on every request. Here every combination of the two optional filters (year or
none, rooms or none) that has data is aggregated at load time into its finished response, so a request is a
point read of one document (1 RU). A combination without a document is the data API's 404.

Same arithmetic as populate_analytical + _avg_price_per_sqm: every staged sale with a kr/m², the average
rounded half away from zero, the date range of those sales.
"""
from fractions import Fraction

from . import stats
from .advertisements import market


def doc_id(year, rooms):
    """The document id of a filter combination, as the API computes it from the parsed parameters."""
    return f"{'all' if year is None else year}_{'all' if rooms is None else float(rooms)}"


def build():
    _, sales = market()
    cells = {}
    for s in sales:
        if s["kr_per_m2"] is None:
            continue
        # every combination this sale falls in (a sale without rooms only where rooms is not filtered)
        for key in {(s["yr"], s["rooms"]), (s["yr"], None), (None, s["rooms"]), (None, None)}:
            c = cells.setdefault(key, [0, 0, s["sold_date"], s["sold_date"]])
            c[0] += s["kr_per_m2"]
            c[1] += 1
            c[2] = min(c[2], s["sold_date"])
            c[3] = max(c[3], s["sold_date"])
    return [{
        "id": doc_id(year, rooms),
        "avg_kr_per_m2": stats.round_fraction(Fraction(total, n)),
        "sample_count": n,
        "from_date": first,
        "to_date": last,
        "filters": {"year": year, "rooms": rooms},
    } for (year, rooms), (total, n, first, last) in cells.items()
        if rooms is None or 0 < rooms <= 20]   # the rooms filter accepts (0, 20]; other cells are unreachable
