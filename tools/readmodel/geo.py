"""The geo hierarchy: the `kommuns`, `areas` and `cities` read models (GET /kommuns, /areas, /cities), and the
benchmarks each listing carries from its område, kommun and city.

A port of the data repo's src/db/import_booli.py: populate_kommuns / populate_areas / populate_cities (with
_fill_geo_scope and _fill_geo_sold_history), populate_kommun_price_stats and flatten_geo_benchmarks, with the
same column names and the same rounding (see stats.py). A level is built from the current listings (counts,
asking kr/m², size, avgift and loan per m²) and from every completed sale (per-year sold kr/m² per boende type,
avgift/m² and turnover of bostadsrätter, with 3y/5y trends and a forecast). Each child then gets its
ancestors' summaries copied down (city_* on a kommun; kommun_* and city_* on an area and a listing), so one
row answers "how does this compare with its kommun".

The school block (avg/median_school_socio_index, school_socio_index_*) is built from the listings' nearest
schools (tools/readmodel/schools.py): _geo_school_select and _fill_geo_school_history.
"""
import functools
import hashlib
import unicodedata
from fractions import Fraction

from . import schools, stats
from .advertisements import BRF_FORECAST_TARGET, SALES_LATEST, SALES_WINDOWS, SALES_YEARS, market

SFX = ("br", "villa", "radhus", "fritids")
FORM_BUCKET = {"lägenhet": "br", "villa": "villa", "radhus": "radhus", "parhus": "radhus", "kedjehus": "radhus",
               "fritidshus": "fritids"}
SCHOOL_YEARS = (2023, 2024, 2025, 2026)
LEVELS = {"cities": ("city",), "kommuns": ("kommun",), "areas": ("kommun", "area_name")}

# column sets, named as in the data repo
GEO_TYPE_COLUMNS = (
    tuple(f"listing_count_{s}" for s in SFX)
    + tuple(c for s in SFX for c in (f"avg_kr_per_m2_{s}", f"median_kr_per_m2_{s}", f"median_living_area_m2_{s}"))
    + ("avg_fee_per_m2_br", "median_fee_per_m2_br", "avg_loan_per_m2_br", "median_loan_per_m2_br"))
GEO_SCHOOL_COLUMNS = ("avg_school_socio_index", "median_school_socio_index")
GEO_STAT_COLUMNS = tuple(c for c in GEO_TYPE_COLUMNS if not c.startswith("listing_count_"))
KOMMUN_CITY_COPYDOWN_SOURCE = GEO_STAT_COLUMNS + ("units_in_scope_br", "pct_for_sale_br") + GEO_SCHOOL_COLUMNS


def _price_summary(sfx):
    return ((f"sold_kr_per_m2_{sfx}_{SALES_LATEST}",) + tuple(f"sold_kr_per_m2_{sfx}_trend_{n}y" for n in SALES_WINDOWS)
            + (f"sold_kr_per_m2_{sfx}_forecast",))


SOLD_FEE_SUMMARY = ((f"sold_fee_per_m2_br_{SALES_LATEST}",)
                    + tuple(f"sold_fee_per_m2_br_trend_{n}y" for n in SALES_WINDOWS) + ("sold_fee_per_m2_br_forecast",))
SOLD_TURNOVER_SUMMARY = (f"turnover_br_{SALES_LATEST}",) + tuple(f"turnover_br_agg_{n}y" for n in SALES_WINDOWS)
SOLD_PRICE_SUMMARY_COLLAPSED = ((f"sold_kr_per_m2_{SALES_LATEST}",)
                                + tuple(f"sold_kr_per_m2_trend_{n}y" for n in SALES_WINDOWS) + ("sold_kr_per_m2_forecast",))
SCHOOL_GEO_SUMMARY = ((f"school_socio_index_{max(SCHOOL_YEARS)}",)
                      + tuple(f"school_socio_index_trend_{n}y" for n in SALES_WINDOWS) + ("school_socio_index_forecast",))
GEO_HISTORY_SUMMARY = (tuple(c for s in SFX for c in _price_summary(s)) + SOLD_FEE_SUMMARY + SOLD_TURNOVER_SUMMARY
                       + SCHOOL_GEO_SUMMARY)
ADS_HISTORY_SUMMARY = SOLD_PRICE_SUMMARY_COLLAPSED + SOLD_FEE_SUMMARY + SOLD_TURNOVER_SUMMARY + SCHOOL_GEO_SUMMARY
SOLD_SELF_COLUMNS = (
    tuple(f"num_sales_br_{y}" for y in SALES_YEARS) + tuple(f"turnover_br_{y}" for y in SALES_YEARS)
    + tuple(f"sold_kr_per_m2_{s}_{y}" for s in SFX for y in SALES_YEARS)
    + tuple(f"sold_obs_{s}_{y}" for s in SFX for y in SALES_YEARS)
    + tuple(f"sold_fee_per_m2_br_{y}" for y in SALES_YEARS)
    + tuple(c for s in SFX for c in _price_summary(s)[1:]) + SOLD_FEE_SUMMARY[1:] + SOLD_TURNOVER_SUMMARY[1:])
SCHOOL_SELF_COLUMNS = tuple(f"school_socio_index_{y}" for y in SCHOOL_YEARS) + SCHOOL_GEO_SUMMARY[1:]
HIDDEN_FIELDS = ("id", "order")      # stored, not served


def collation_key(s):
    """Sort key emulating the data repo's Postgres collation (en_US): letters compared without accents and
    case, spaces and punctuation ignored; then accents, then case, decide ties ('Älgö' < 'Alphyddan')."""
    s = s or ""
    decomposed = [unicodedata.normalize("NFD", ch) for ch in s]
    base = "".join(d[0].lower() for d in decomposed if d[0].isalnum())
    accents = tuple(d[1:] for d in decomposed if d[0].isalnum())
    case = tuple(d[0].isupper() for d in decomposed if d[0].isalnum())
    ignored = sum(1 for d in decomposed if not d[0].isalnum())
    return base, accents, case, ignored, s


def _bucket(form):
    return FORM_BUCKET.get(form)


def _key(row, keys):
    k = tuple(row.get(c) for c in keys)
    return None if any(v is None for v in k) else k


def _snapshot(ads):
    """_geo_agg_select over one level's listings."""
    out = {}
    for s in SFX:
        rows = [a for a in ads if _bucket(a["object_form"]) == s]
        out[f"listing_count_{s}"] = len(rows)
        out[f"avg_kr_per_m2_{s}"] = stats.round_fraction(stats.exact_mean([a["kr_per_m2"] for a in rows]))
        out[f"median_kr_per_m2_{s}"] = stats.round_double(stats.percentile_cont([a["kr_per_m2"] for a in rows]))
        out[f"median_living_area_m2_{s}"] = stats.round_double_numeric(
            stats.percentile_cont([a["living_area_m2"] for a in rows]), 1)
    br = [a for a in ads if a["tenure"] == "bostadsrätt"]
    for col, field in (("fee", "monthly_fee_per_m2"), ("loan", "brf_loan_per_m2")):
        out[f"avg_{col}_per_m2_br"] = stats.round_fraction(stats.exact_mean([a[field] for a in br]), 1)
        out[f"median_{col}_per_m2_br"] = stats.round_double_numeric(stats.percentile_cont([a[field] for a in br]), 1)
    out["avg_school_socio_index"] = stats.round_fraction(
        stats.exact_mean([a["avg_school_socio_index"] for a in ads]), 1)
    out["median_school_socio_index"] = stats.round_double_numeric(
        stats.percentile_cont([a["avg_school_socio_index"] for a in ads]), 1)
    return out


def _units_in_scope(ads):
    """_fill_geo_scope: Σ brf_num_units over the level's distinct BRFs."""
    distinct = {(a["brf_org_number"], a["brf_num_units"]) for a in ads
                if a["brf_org_number"] is not None and a["brf_num_units"] is not None}
    return sum(u for _, u in distinct) if distinct else None


def _fit(points, window):
    """regr over (yr, v) points with yr in the last `window` years: (slope, intercept)."""
    return stats.regr([(yr, float(v)) for yr, v in points if v is not None and yr > SALES_LATEST - window])


def _sold_history(sales, units):
    """_fill_geo_sold_history for one level row; `sales` are the level's sales."""
    out = dict.fromkeys(SOLD_SELF_COLUMNS)
    # (1) bostadsrätt: number of sales, avgift/m², turnover
    br = [s for s in sales if s["tenure"] == "bostadsrätt"]
    if br:
        by_year = {}
        for s in br:
            by_year.setdefault(s["yr"], []).append(s)
        n = {y: len(v) for y, v in by_year.items()}
        fee = {}
        for y, v in by_year.items():
            vals = [Fraction(s["monthly_fee_kr"]) / Fraction(str(s["living_area_m2"])) for s in v
                    if s["monthly_fee_kr"] is not None and s["living_area_m2"] is not None and s["living_area_m2"] > 0]
            fee[y] = stats.round_fraction(sum(vals) / len(vals), 1) if vals else None
        for y in SALES_YEARS:
            out[f"num_sales_br_{y}"] = n.get(y, 0)
            out[f"sold_fee_per_m2_br_{y}"] = fee.get(y)
        points = sorted(fee.items())
        for w in SALES_WINDOWS:
            out[f"sold_fee_per_m2_br_trend_{w}y"] = stats.round_double_numeric(_fit(points, w)[0], 1)
        slope, icpt = _fit(points, 5)
        out["sold_fee_per_m2_br_forecast"] = (None if slope is None
                                              else stats.round_double_numeric(icpt + slope * BRF_FORECAST_TARGET, 1))
        if units:
            for y in SALES_YEARS:
                out[f"turnover_br_{y}"] = stats.round_fraction(Fraction(n.get(y, 0) * 100, units), 1)
            for w in SALES_WINDOWS:
                agg = sum(c for y, c in n.items() if y > SALES_LATEST - w)
                out[f"turnover_br_agg_{w}y"] = stats.round_fraction(Fraction(agg * 100, units), 1)
    # (2) sold kr/m² per boende type
    for sfx in SFX:
        typed = [s for s in sales if _bucket(s["object_form"]) == sfx]
        if not typed:
            continue
        by_year = {}
        for s in typed:
            by_year.setdefault(s["yr"], []).append(s["kr_per_m2"])
        price = {y: stats.round_fraction(stats.exact_mean(v)) for y, v in by_year.items()}
        for y in SALES_YEARS:
            if y in by_year:
                out[f"sold_kr_per_m2_{sfx}_{y}"] = price[y]
                out[f"sold_obs_{sfx}_{y}"] = sum(1 for v in by_year[y] if v is not None)
        points = sorted(price.items())
        for w in SALES_WINDOWS:
            out[f"sold_kr_per_m2_{sfx}_trend_{w}y"] = stats.round_double(_fit(points, w)[0])
        slope, icpt = _fit(points, 5)
        out[f"sold_kr_per_m2_{sfx}_forecast"] = None if slope is None else stats.round_double(
            icpt + slope * BRF_FORECAST_TARGET)
    return out


def _school_history(ads):
    """_fill_geo_school_history: per edition, the mean over the level's listings of each listing's 5-nearest
    average for that edition; 3y/5y trends and a FORECAST_YEAR projection over that series."""
    out = {}
    series = []
    for y in SCHOOL_YEARS:
        v = stats.round_fraction(stats.exact_mean([schools.avg_index(a, year=y) for a in ads]), 1)
        out[f"school_socio_index_{y}"] = v
        if v is not None:
            series.append((y, v))
    latest = max(SCHOOL_YEARS)
    for n in SALES_WINDOWS:
        slope, _ = stats.regr([(y, v) for y, v in series if y > latest - n])
        out[f"school_socio_index_trend_{n}y"] = stats.round_double_numeric(slope, 1)
    slope, icpt = stats.regr([(y, v) for y, v in series if y > latest - 5])
    out["school_socio_index_forecast"] = (None if slope is None
                                          else stats.round_double_numeric(icpt + slope * schools.FORECAST_YEAR, 1))
    return out


def _level(name, ads, sales):
    keys = LEVELS[name]
    groups, sold = {}, {}
    for a in ads:
        k = _key(a, keys)
        if k:
            groups.setdefault(k, []).append(a)
    for s in sales:
        k = _key(s, keys)
        if k in groups:
            sold.setdefault(k, []).append(s)
    rows = {}
    for k, rs in groups.items():
        row = dict(zip(keys, k))
        row["listing_count"] = len(rs)
        if name != "cities":
            row["city"] = max((a["city"] for a in rs if a["city"] is not None), default=None)
        row.update(_snapshot(rs))
        units = _units_in_scope(rs)
        row["units_in_scope_br"] = units
        row["pct_for_sale_br"] = (stats.round_fraction(Fraction(row["listing_count_br"] * 100, units), 1)
                                  if units else None)
        row.update(_sold_history(sold.get(k, []), units))
        row.update(_school_history(rs))
        rows[k] = row
    return rows


def _price_stats(sales):
    """populate_kommun_price_stats: kommun -> [{year, object_type, avg, median, sample_count}], newest year
    first, then by object_type."""
    cells = {}
    for s in sales:
        if s["kr_per_m2"] is not None and s["kommun"] and s["object_type"]:
            cells.setdefault((s["kommun"], s["yr"], s["object_type"]), []).append(s["kr_per_m2"])
    out = {}
    for (kommun, yr, ot), v in sorted(cells.items(), key=lambda kv: (kv[0][0], -kv[0][1], collation_key(kv[0][2]))):
        out.setdefault(kommun, []).append({
            "year": yr, "object_type": ot,
            "avg_kr_per_m2": stats.round_fraction(stats.exact_mean(v)),
            "median_kr_per_m2": stats.round_double(stats.percentile_cont(v)),
            "sample_count": len(v),
        })
    return out


def _collapsed(level_row, col, bucket):
    """The type-collapsed copy of a per-type price summary column for a listing of boende type `bucket`."""
    if col in SOLD_PRICE_SUMMARY_COLLAPSED:
        if bucket is None:
            return None
        return level_row[col.replace("sold_kr_per_m2", f"sold_kr_per_m2_{bucket}", 1)]
    return level_row[col]


def _jsonb_order(row):
    """Keys in the order Postgres' to_jsonb emits them (shorter first, then bytewise), as the data API does."""
    return {k: row[k] for k in sorted(row, key=lambda k: (len(k.encode()), k.encode()))}


def copy_down(rows, levels):
    """flatten_geo_benchmarks onto listing-shaped rows: the same-type benchmark and the history summaries of
    each ancestor (område, kommun, city)."""
    for a in rows:
        bucket = _bucket(a["object_form"])
        for prefix, parent in (("area", levels["areas"].get(_key(a, LEVELS["areas"]))),
                               ("kommun", levels["kommuns"].get(_key(a, LEVELS["kommuns"]))),
                               ("city", levels["cities"].get(_key(a, LEVELS["cities"])))):
            for stat in ("avg_kr_per_m2", "median_kr_per_m2"):
                a[f"{prefix}_{stat}"] = parent[f"{stat}_{bucket}"] if parent and bucket else None
            for col in ADS_HISTORY_SUMMARY:
                a[f"{prefix}_{col}"] = _collapsed(parent, col, bucket) if parent else None


def _doc_id(*parts):
    return hashlib.sha1("\x1f".join(parts).encode()).hexdigest()[:20]


@functools.lru_cache(maxsize=1)
def build_all():
    """(advertisements with geo benchmarks, levels, kommuns, areas, cities): flatten_geo_benchmarks and the
    three populate_* steps."""
    ads, sales = market()
    schools.flatten(ads)
    levels = {name: _level(name, ads, sales) for name in LEVELS}
    cities, kommuns, areas = levels["cities"], levels["kommuns"], levels["areas"]

    # copy-down onto the levels
    for (kommun,), row in kommuns.items():
        c = cities.get((row["city"],))
        for col in KOMMUN_CITY_COPYDOWN_SOURCE + GEO_HISTORY_SUMMARY:
            row[f"city_{col}"] = c[col] if c else None
    for (kommun, _), row in areas.items():
        k, c = kommuns.get((kommun,)), cities.get((row["city"],))
        for col in GEO_HISTORY_SUMMARY:
            row[f"kommun_{col}"] = k[col] if k else None
            row[f"city_{col}"] = c[col] if c else None

    copy_down(ads, levels)

    price_stats = _price_stats(sales)
    for (kommun,), row in kommuns.items():
        row["price_stats"] = price_stats.get(kommun, [])

    def docs(rows, sort_key, id_parts):
        ordered = sorted(rows.values(), key=sort_key)
        return [dict(_jsonb_order(r), id=_doc_id(*id_parts(r)), order=i) for i, r in enumerate(ordered)]

    return (ads, levels,
            docs(kommuns, lambda r: collation_key(r["kommun"]), lambda r: (r["kommun"],)),
            docs(areas, lambda r: (collation_key(r["kommun"]), collation_key(r["area_name"])),
                 lambda r: (r["kommun"], r["area_name"])),
            docs(cities, lambda r: collation_key(r["city"]), lambda r: (r["city"],)))


def advertisements():
    return build_all()[0]


def levels():
    """{"cities"|"kommuns"|"areas": {key: row}}, before ordering, for copy_down."""
    return build_all()[1]


def kommuns():
    return build_all()[2]


def areas():
    return build_all()[3]


def cities():
    return build_all()[4]
