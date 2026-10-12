"""The `advertisements` read model: one self-contained document per listing, as served by GET /advertisements.

A port of the data repo's src/db/import_booli.py (import_advertisements, import_sales and flatten_all):
the same flat columns, the same derivations, run once at load time so that a request is a single indexed
query with no joins and no computation. Each flatten_* function below is named after, and documents any
difference from, its SQL counterpart. The geo benchmarks (flatten_geo_benchmarks: area_*, kommun_*, city_*)
are added by geo.py. Not yet ported (the columns are absent from the documents): flatten_schools /
flatten_school_aggregates (school_*, avg_school_socio_index*).

Inputs are the whole Booli snapshot in market/booli/ (every kommun the data repo covers).
"""
import csv
import functools
import gzip
import json
import re
from fractions import Fraction
from pathlib import Path

from . import financials, stats

REPO = Path(__file__).resolve().parents[2]
MARKET = REPO / "market"
BOOLI = MARKET / "booli"

# Same constants as the data repo (import_booli.py), so the column names and windows agree.
CITY_BY_KOMMUN = {k: "Stockholm" for k in (
    "Botkyrka", "Danderyd", "Ekerö", "Haninge", "Huddinge", "Järfälla", "Lidingö", "Nacka", "Norrtälje",
    "Nykvarn", "Nynäshamn", "Salem", "Sigtuna", "Sollentuna", "Solna", "Stockholm", "Sundbyberg",
    "Södertälje", "Tyresö", "Täby", "Upplands-Bro", "Upplands Väsby", "Vallentuna", "Vaxholm", "Värmdö",
    "Österåker")}
FORECAST_HORIZON_YEARS = 3
BRF_PRICE_YEARS = range(2021, 2027)
BRF_PRICE_LATEST = max(BRF_PRICE_YEARS)
BRF_FEE_YEARS = range(2012, 2027)
BRF_FORECAST_TARGET = BRF_PRICE_LATEST + FORECAST_HORIZON_YEARS
SALES_YEARS = BRF_PRICE_YEARS
SALES_LATEST = max(SALES_YEARS)
SALES_WINDOWS = (3, 5)
BRF_PROJECTIONS = {   # metric -> latest year, trend windows, forecast window, decimals (None = int)
    "kr_per_m2": {"latest": BRF_PRICE_LATEST, "windows": (3, 5), "forecast_window": 5, "decimals": None},
    "fee_per_m2": {"latest": max(BRF_FEE_YEARS), "windows": (3, 5, 15), "forecast_window": 5, "decimals": 1},
}
_FALLBACK_FORM = {"bostadsrätt": "lägenhet", "villa": "villa", "radhus": "radhus", "fritidshus": "fritidshus"}
_FALLBACK_TENURE = {"bostadsrätt": "bostadsrätt", "villa": "äganderätt", "fritidshus": "äganderätt"}


def _rows(path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def to_int(v):
    return int(float(v)) if v else None


def to_float(v):
    return float(v) if v else None


def to_bool(v):
    return None if not v else str(v).strip().lower() in ("1", "true", "yes")


def _tenure_from_attributes(attrs_json):
    if not attrs_json:
        return None
    try:
        tf = (json.loads(attrs_json).get("tenure_form") or "").strip().lower()
    except (ValueError, TypeError):
        return None
    if tf == "bostadsrätt":
        return "bostadsrätt"
    return "äganderätt" if tf else None


def derive_axes(row, linked_to_brf):
    """(object_form, tenure), as derive_axes in the data repo."""
    legacy = (row.get("property_type") or row.get("object_type") or "").strip().lower()
    form = (row.get("object_form") or "").strip().lower() or _FALLBACK_FORM.get(legacy)
    tenure = ((row.get("tenure") or "").strip().lower()
              or _tenure_from_attributes(row.get("attributes_json"))
              or ("bostadsrätt" if linked_to_brf else None)
              or _FALLBACK_TENURE.get(legacy))
    return form or None, tenure or None


def street_name_from(address, street_number):
    """'Sikvägen 29A' -> 'Sikvägen'. The data repo strips len(str(street_number)) + 1 characters, which
    leaves a stray digit before an entrance letter ('Sikvägen 2'); this strips the whole house number."""
    if street_number:
        return re.sub(r"\s+\d+\s*[A-Za-z]{0,2}$", "", address.strip())
    return address


# ---------- staging (import_brf_details, import_sales, build_residence_staging) ----------

def import_brf_details():
    """booli_brf_id -> BRF facts (stg_brf); first row wins on a repeated id."""
    out = {}
    for r in _rows(BOOLI / "brf_details_booli.csv.gz"):
        bid = r["booli_brf_id"]
        if not bid or bid in out:
            continue
        out[bid] = {
            "name": r["name"] or f"BRF {bid}", "org_number": r["org_number"] or None,
            "construction_year": to_int(r["construction_year"]), "num_units": to_int(r["num_units"]),
            "num_rental_units": to_int(r["num_rental_units"]),
            "total_living_area_m2": to_int(r["total_living_area_m2"]),
            "plot_ownership": r["plot_ownership"] or None, "is_genuine": to_bool(r["is_genuine"]),
            "total_loan_kr": to_int(r["total_loan_kr"]), "annual_report_year": to_int(r["annual_report_year"]),
            "has_valid_annual_report": to_bool(r["has_valid_annual_report"]),
            "economy": json.loads(r["economy_json"]) if r["economy_json"] else None,
        }
    return out


def _address_links():
    """market/address_links.csv (kept by hand, with evidence): lower(address) -> booli_brf_id. Not in the
    data repo: links for BRFs that Booli has no apartment register for. None with LINKING = "data-repo"."""
    if LINKING == "data-repo":
        return {}
    booli_id = {b["org_number"]: bid for bid, b in import_brf_details().items() if b["org_number"]}
    return {r["address"].strip().lower(): booli_id[r["orgnr"]]
            for r in _rows(MARKET / "address_links.csv") if r["orgnr"] in booli_id}


def import_sales(links):
    """Every completed sale (stg_sales): deduped first-wins by listing_id, sold_date required. brf_booli_id is
    Booli's link, else, for a sale Booli left unlinked, market/address_links.csv."""
    seen, out = set(), []
    for r in _rows(BOOLI / "sales_booli.csv.gz"):
        if not r["listing_id"] or not r["sold_date"] or r["listing_id"] in seen:
            continue
        seen.add(r["listing_id"])
        kommun = r["kommun"] or None
        object_form, tenure = derive_axes(r, linked_to_brf=bool(r["booli_brf_id"].strip()))
        out.append({
            "sold_date": r["sold_date"], "yr": int(r["sold_date"][:4]),
            "rooms": to_float(r["rooms"]),
            "kr_per_m2": to_int(r["kr_per_m2"]), "monthly_fee_kr": to_int(r["monthly_fee_kr"]),
            "living_area_m2": to_float(r["living_area_m2"]),
            "brf_booli_id": r["booli_brf_id"] or links.get(r["address"].strip().lower()),
            "kommun": kommun, "object_type": r["object_type"] or None,
            "object_form": object_form, "tenure": tenure,
            "area_name": r["area_name"] or None, "city": CITY_BY_KOMMUN.get(kommun) if kommun else None,
        })
    return out


def sales_by_brf(sales):
    out = {}
    for s in sales:
        if s["brf_booli_id"]:
            out.setdefault(s["brf_booli_id"], []).append(s)
    return out


def relaxed(addr_key):
    """'sikvägen 29a' -> 'sikvägen 29' (_relaxed_addr_key)."""
    return re.sub(r"(\d+)[a-z]$", r"\1", addr_key)


# "homesight" (default) or "data-repo": tools/cloud/parity.py --data-repo-linking links listings exactly as the
# data repo does, so that every remaining difference is one of computation
LINKING = "homesight"


def build_residence_staging(links):
    """The maps a listing is linked to its BRF by (build_residence_staging): Booli residence id -> BRF,
    full address -> BRF (residence register, then allabrf links, then market/address_links.csv; first wins)
    and the letter-stripped address -> BRF where that is unambiguous across every BRF. With LINKING =
    "data-repo": no market/address_links.csv, and the letter-stripped map only from lettered addresses."""
    by_residence, by_address, brfs_by_relaxed, lettered = {}, {}, {}, {}
    for r in _rows(BOOLI / "brf_residences_booli.csv.gz"):
        bid, addr = r["booli_brf_id"], r["address"].strip().lower()
        if not bid:
            continue
        by_residence.setdefault(r["residence_booli_id"], bid)
        if addr:
            by_address.setdefault(addr, bid)
            brfs_by_relaxed.setdefault(relaxed(addr), set()).add(bid)
            if relaxed(addr) != addr:
                lettered[relaxed(addr)] = bid
    for r in _rows(BOOLI / "brf_address_links_allabrf.csv.gz"):
        if r["booli_brf_id"].strip() and r["address"].strip():
            by_address.setdefault(r["address"].strip().lower(), r["booli_brf_id"].strip())
    if LINKING == "data-repo":
        return None, by_address, {k: b for k, b in lettered.items() if len(brfs_by_relaxed[k]) == 1}
    for addr, bid in links.items():
        by_address.setdefault(addr, bid)
    by_relaxed = {k: next(iter(v)) for k, v in brfs_by_relaxed.items() if len(v) == 1}
    return by_residence, by_address, by_relaxed


def link_advertisement_brf(row, by_residence, by_address, by_relaxed):
    """booli_brf_id of a listing, or None (link_advertisement_brf). Differs from the data repo, which keys on
    street_name + street_number and so loses the entrance letter (and, through its street_name_from, the
    street name of a lettered multi-digit address): this first tries the Booli residence id in the listing
    URL, then the address as printed, then the address without its entrance letter. It links more listings,
    never differently."""
    if LINKING == "data-repo":
        sn = to_int(re.match(r"\d*", row.get("street_number") or "").group() or None)
        if sn is None:
            return None
        name = row["address"][:-(len(str(sn)) + 1)]
        return (by_address.get((name + " " + str(sn)).strip().lower())
                or by_relaxed.get((name.strip() + " " + str(sn)).lower()))
    m = re.search(r"/bostad/(\d+)", row["source_url"] or "")
    key = row["address"].strip().lower()
    return ((m and by_residence.get(m.group(1))) or by_address.get(key) or by_address.get(relaxed(key))
            or by_relaxed.get(relaxed(key)))


def import_advertisements(by_residence, by_address, by_relaxed):
    """One document per current listing, every kommun in the snapshot (last row wins per listing_id)."""
    docs = {}
    for r in _rows(BOOLI / "advertisements_booli.csv.gz"):
        if not r["listing_id"]:
            continue
        sn = to_int(re.match(r"\d*", r.get("street_number") or "").group() or None)
        object_form, tenure = derive_axes(r, linked_to_brf=False)   # the listing export carries no BRF link
        lat, lng = to_float(r["lat"]), to_float(r["lng"])
        docs[r["listing_id"]] = {
            "id": r["listing_id"],
            "listing_id": r["listing_id"],
            "listed_date": r["listed_date"] or None,
            "asking_price_kr": to_int(r["asking_price_kr"]),
            "property_type": r["property_type"] or None,
            "object_form": object_form,
            "tenure": tenure,
            "street_name": street_name_from(r["address"], sn),
            "street_number": sn,
            "apartment_number": r["apartment_number"] or None,
            "rooms": to_float(r["rooms"]),
            "living_area_m2": to_float(r["living_area_m2"]),
            "kr_per_m2": to_int(r["kr_per_m2"]),
            "monthly_fee_kr": to_int(r["monthly_fee_kr"]),
            "monthly_fee_per_m2": None,
            "construction_year": to_int(r["construction_year"]),
            "broker_agency": r["broker_agency"] or None,
            "description": None,
            "attributes": json.loads(r["attributes_json"]) if r["attributes_json"] else None,
            "photo_urls": json.loads(r["photo_urls"]) if r["photo_urls"] else None,
            "source_url": r["source_url"] or None,
            "location": {"lat": lat, "lng": lng} if lat is not None and lng is not None else None,
            # GeoJSON copy of location for the spatial index (radius search); not served
            "geo": {"type": "Point", "coordinates": [lng, lat]} if lat is not None and lng is not None else None,
            "kommun": r.get("kommun") or "Tyresö",
            "area_name": r["area_name"] or None,
            "city": None,
            "brf_booli_id": link_advertisement_brf(r, by_residence, by_address, by_relaxed),
        }
    return list(docs.values())


# ---------- flattening (flatten_all, in the data repo's order) ----------

BRF_COLUMNS = ("name", "org_number", "construction_year", "num_units", "num_rental_units",
               "total_living_area_m2", "plot_ownership", "is_genuine", "total_loan_kr", "annual_report_year",
               "has_valid_annual_report", "economy")


def flatten_brf(ads, brfs):
    for a in ads:
        b = brfs.get(a["brf_booli_id"], {})
        for c in BRF_COLUMNS:
            a[f"brf_{c}"] = b.get(c)


def flatten_brf_price_trend(ads, sales):
    """brf_kr_per_m2_trend: slope of kr/m² over sold date (kr/m² per year), all of the BRF's sales;
    NULL with < 2 priced sales or an implausible slope (|slope| >= 1e7)."""
    trend = {}
    for bid, ss in sales.items():
        slope, _ = stats.regr([(stats.years_since_epoch(s["sold_date"]), s["kr_per_m2"])
                               for s in ss if s["kr_per_m2"] is not None])
        if slope is not None and abs(slope) < 1e7:
            trend[bid] = stats.round_double(slope)
    for a in ads:
        a["brf_kr_per_m2_trend"] = trend.get(a["brf_booli_id"])


def flatten_brf_price_history(ads, sales):
    """brf_kr_per_m2_<year>: the BRF's average kr/m² per year; NULL for a year without a priced sale."""
    for a in ads:
        ss = sales.get(a["brf_booli_id"], [])
        for y in BRF_PRICE_YEARS:
            a[f"brf_kr_per_m2_{y}"] = stats.round_fraction(
                stats.exact_mean([s["kr_per_m2"] for s in ss if s["yr"] == y]))


def flatten_brf_sales_history(ads, sales):
    """brf_num_sales_<year>, brf_turnover_<year> (% of brf_num_units sold) and brf_turnover_agg_<n>y."""
    for a in ads:
        ss = sales.get(a["brf_booli_id"])
        units = a["brf_num_units"]
        for y in SALES_YEARS:
            n = None if ss is None else sum(1 for s in ss if s["yr"] == y)
            a[f"brf_num_sales_{y}"] = n
        for y in SALES_YEARS:
            n = a[f"brf_num_sales_{y}"]
            a[f"brf_turnover_{y}"] = (stats.round_numeric(n * 100 / units, 1)
                                      if n is not None and units else None)
        for w in SALES_WINDOWS:
            n = None if ss is None else sum(1 for s in ss if s["yr"] > SALES_LATEST - w)
            a[f"brf_turnover_agg_{w}y"] = stats.round_numeric(n * 100 / units, 1) if n is not None and units else None


def _fee_points(ss):
    return [s for s in ss if s["monthly_fee_kr"] is not None and s["living_area_m2"]]


def flatten_brf_fee_trend(ads, sales):
    """brf_fee_per_m2_trend: slope of avgift/m² over sold date (kr/m²/month per year); NULL with < 2
    fee-bearing sales or |slope| >= 1e4."""
    trend = {}
    for bid, ss in sales.items():
        slope, _ = stats.regr([(stats.years_since_epoch(s["sold_date"]), s["monthly_fee_kr"] / s["living_area_m2"])
                               for s in _fee_points(ss)])
        if slope is not None and abs(slope) < 1e4:
            trend[bid] = stats.round_double_numeric(slope, 1)
    for a in ads:
        a["brf_fee_per_m2_trend"] = trend.get(a["brf_booli_id"])


def flatten_brf_fee_history(ads, sales):
    """brf_fee_per_m2_<year>: the BRF's average avgift/m² per year (1 decimal)."""
    for a in ads:
        ss = _fee_points(sales.get(a["brf_booli_id"], []))
        for y in BRF_FEE_YEARS:
            a[f"brf_fee_per_m2_{y}"] = stats.round_fraction(stats.exact_mean(
                [Fraction(s["monthly_fee_kr"]) / Fraction(str(s["living_area_m2"])) for s in ss if s["yr"] == y]), 1)


def flatten_brf_projections(ads, sales):
    """brf_<metric>_trend_<n>y (slope over the per-year averages of the last n years, per year) and
    brf_<metric>_forecast (the forecast-window fit, extrapolated to BRF_FORECAST_TARGET)."""
    per_brf = {}
    for bid, ss in sales.items():
        series = {
            "kr_per_m2": [(s["yr"], s["kr_per_m2"]) for s in ss if s["kr_per_m2"] is not None],
            # monthly_fee_kr::numeric / living_area_m2, exact as in Postgres
            "fee_per_m2": [(s["yr"], Fraction(s["monthly_fee_kr"]) / Fraction(str(s["living_area_m2"])))
                           for s in _fee_points(ss)],
        }
        cols = {}
        for metric, spec in BRF_PROJECTIONS.items():
            by_year = {}
            for yr, v in series[metric]:
                by_year.setdefault(yr, []).append(v)
            yravg = sorted((yr, float(stats.exact_mean(vs))) for yr, vs in by_year.items())

            def rnd(v):
                return stats.round_double(v) if spec["decimals"] is None else stats.round_double_numeric(v, spec["decimals"])

            for n in spec["windows"]:
                slope, _ = stats.regr_two_pass([(yr, v) for yr, v in yravg if yr > spec["latest"] - n])
                cols[f"brf_{metric}_trend_{n}y"] = rnd(slope)
            slope, icpt = stats.regr_two_pass([(yr, v) for yr, v in yravg if yr > spec["latest"] - spec["forecast_window"]])
            cols[f"brf_{metric}_forecast"] = None if slope is None else rnd(icpt + slope * BRF_FORECAST_TARGET)
        per_brf[bid] = cols
    names = [f"brf_{m}_trend_{n}y" for m, s in BRF_PROJECTIONS.items() for n in s["windows"]] + \
            [f"brf_{m}_forecast" for m in BRF_PROJECTIONS]
    for a in ads:
        cols = per_brf.get(a["brf_booli_id"], {})
        for c in names:
            a[c] = cols.get(c)


def flatten_brf_loan_per_m2(ads):
    for a in ads:
        loan, area = a["brf_total_loan_kr"], a["brf_total_living_area_m2"]
        a["brf_loan_per_m2"] = stats.round_numeric(loan / area, 1) if loan is not None and area else None


def flatten_fee_per_m2(ads):
    for a in ads:
        fee, area = a["monthly_fee_kr"], a["living_area_m2"]
        a["monthly_fee_per_m2"] = stats.round_numeric(fee / area, 1) if fee is not None and area else None


def flatten_brf_pct_for_sale(ads):
    """brf_pct_for_sale: the BRF's listings now as a % of brf_num_units."""
    count = {}
    for a in ads:
        count[a["brf_booli_id"]] = count.get(a["brf_booli_id"], 0) + 1
    for a in ads:
        units = a["brf_num_units"]
        a["brf_pct_for_sale"] = stats.round_numeric(count[a["brf_booli_id"]] * 100 / units, 1) if units else None


INDEX_CSV = REPO / "annual_reviews" / "brfs" / "index.csv"


def flatten_brf_report(ads):
    """brf_report_*: the BRF's own annual report (latest verified year), from annual_reviews/brfs/ via
    tools/readmodel/financials.py, plus land tenure and the stambyte assessment from index.csv. Not in the data
    repo: this is homesight's verified layer. NULL for a BRF whose reports are not extracted."""
    years = financials.all_brfs()
    index = {r["orgnr"]: r for r in _rows(INDEX_CSV)}
    for a in ads:
        org = a["brf_org_number"]
        d = financials.latest(years.get(org, {}))
        i = index.get(org, {})
        bs, pm = (d or {}).get("balance_sheet", {}), (d or {}).get("per_m2", {})
        a.update({
            "brf_report_fiscal_year": d["fiscal_year"] if d else None,
            "brf_report_verified": d["verified"] if d else None,
            "brf_report_url": ((d or {}).get("report") or {}).get("url"),
            "brf_report_loans_kr": bs.get("loans_kr"),
            "brf_report_loans_per_m2": pm.get("loans_kr"),
            "brf_report_cash_kr": bs.get("cash_kr"),
            "brf_report_annual_fees_per_m2": pm.get("annual_fees_kr"),
            "brf_report_operating_costs_per_m2": pm.get("operating_costs_kr"),
            "brf_report_interest_paid_per_m2": pm.get("interest_paid_kr"),
            "brf_report_interest_share_of_fees_pct": ((d or {}).get("ratios") or {}).get("interest_share_of_annual_fees_pct"),
            "brf_report_ground_rent_kr": ((d or {}).get("costs") or {}).get("ground_rent_kr"),
            "brf_land_tenure": i.get("land_tenure") or None,
            "brf_stambyte_last": i.get("stambyte_last") or None,
            "brf_stambyte_next": i.get("stambyte_next") or None,
            "brf_stambyte_next_status": i.get("stambyte_next_status") or None,
        })


def flatten_city(ads):
    for a in ads:
        a["city"] = CITY_BY_KOMMUN.get(a["kommun"])


@functools.lru_cache(maxsize=1)
def market():
    """(advertisements, sales): every listing fully flattened except the geo benchmarks
    (tools/readmodel/geo.py adds those), and every staged sale. Cached: the advertisements and the geo
    containers are built from the same pass."""
    links = _address_links()
    brfs = import_brf_details()
    all_sales = import_sales(links)
    sales = sales_by_brf(all_sales)
    ads = import_advertisements(*build_residence_staging(links))
    flatten_brf(ads, brfs)
    flatten_brf_price_trend(ads, sales)
    flatten_brf_price_history(ads, sales)
    flatten_brf_sales_history(ads, sales)
    flatten_brf_fee_trend(ads, sales)
    flatten_brf_fee_history(ads, sales)
    flatten_brf_projections(ads, sales)
    flatten_brf_loan_per_m2(ads)
    flatten_fee_per_m2(ads)
    flatten_brf_pct_for_sale(ads)
    flatten_city(ads)
    flatten_brf_report(ads)
    return ads, all_sales
