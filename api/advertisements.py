"""GET /advertisements over the `advertisements` read model (Cosmos container, one document per listing).

The contract is the data repo's: the same parameters, sorts, 400 messages and response shape. Every
filter and sort is a plain read of a precomputed field (built at load time by tools/readmodel/), so a
request is one indexed query, plus a second one only when fewer than `limit` rows have the sort field.

SORTS and FILTERS are the single source of truth for what is indexed: tools/readmodel/containers.py
builds the container's indexing policy from them.
"""
import params as p

# sort key -> (document field, direction); every sort ends with listing_id DESC as tiebreak and puts
# rows without the field last (NULLS LAST in the data repo).
SORTS = {
    "listed_date":            ("listed_date", "DESC"),
    "price_asc":              ("asking_price_kr", "ASC"),
    "price_desc":             ("asking_price_kr", "DESC"),
    "price_per_sqm_asc":      ("kr_per_m2", "ASC"),
    "price_per_sqm_desc":     ("kr_per_m2", "DESC"),
    "rooms_asc":              ("rooms", "ASC"),
    "rooms_desc":             ("rooms", "DESC"),
    "area_asc":               ("living_area_m2", "ASC"),
    "area_desc":              ("living_area_m2", "DESC"),
    "fee_per_sqm_asc":        ("monthly_fee_per_m2", "ASC"),
    "fee_per_sqm_desc":       ("monthly_fee_per_m2", "DESC"),
    "fee_asc":                ("monthly_fee_kr", "ASC"),
    "fee_desc":               ("monthly_fee_kr", "DESC"),
    "construction_year_asc":  ("construction_year", "ASC"),
    "construction_year_desc": ("construction_year", "DESC"),
    "brf_units_asc":          ("brf_num_units", "ASC"),
    "brf_units_desc":         ("brf_num_units", "DESC"),
    "brf_trend_asc":          ("brf_kr_per_m2_trend", "ASC"),
    "brf_trend_desc":         ("brf_kr_per_m2_trend", "DESC"),
    "brf_loan_asc":           ("brf_loan_per_m2", "ASC"),
    "brf_loan_desc":          ("brf_loan_per_m2", "DESC"),
    "brf_price_asc":          ("brf_kr_per_m2_2026", "ASC"),
    "brf_price_desc":         ("brf_kr_per_m2_2026", "DESC"),
    "brf_fee_trend_asc":      ("brf_fee_per_m2_trend", "ASC"),
    "brf_fee_trend_desc":     ("brf_fee_per_m2_trend", "DESC"),
    "brf_fee_asc":            ("brf_fee_per_m2_2026", "ASC"),
    "brf_fee_desc":           ("brf_fee_per_m2_2026", "DESC"),
    "brf_pct_for_sale_asc":   ("brf_pct_for_sale", "ASC"),
    "brf_pct_for_sale_desc":  ("brf_pct_for_sale", "DESC"),
    "brf_price_trend_5y_asc":  ("brf_kr_per_m2_trend_5y", "ASC"),
    "brf_price_trend_5y_desc": ("brf_kr_per_m2_trend_5y", "DESC"),
    "brf_price_forecast_asc":  ("brf_kr_per_m2_forecast", "ASC"),
    "brf_price_forecast_desc": ("brf_kr_per_m2_forecast", "DESC"),
    "brf_fee_trend_5y_asc":    ("brf_fee_per_m2_trend_5y", "ASC"),
    "brf_fee_trend_5y_desc":   ("brf_fee_per_m2_trend_5y", "DESC"),
    "brf_fee_forecast_asc":    ("brf_fee_per_m2_forecast", "ASC"),
    "brf_fee_forecast_desc":   ("brf_fee_per_m2_forecast", "DESC"),
    "school_index_1_asc":      ("avg_school_socio_index_1", "ASC"),
    "school_index_1_desc":     ("avg_school_socio_index_1", "DESC"),
    "school_index_3_asc":      ("avg_school_socio_index_3", "ASC"),
    "school_index_3_desc":     ("avg_school_socio_index_3", "DESC"),
    "school_index_asc":        ("avg_school_socio_index", "ASC"),
    "school_index_desc":       ("avg_school_socio_index", "DESC"),
    "school_forecast_1_asc":   ("avg_school_socio_index_1_forecast", "ASC"),
    "school_forecast_1_desc":  ("avg_school_socio_index_1_forecast", "DESC"),
    "school_forecast_3_asc":   ("avg_school_socio_index_3_forecast", "ASC"),
    "school_forecast_3_desc":  ("avg_school_socio_index_3_forecast", "DESC"),
    "school_forecast_asc":     ("avg_school_socio_index_forecast", "ASC"),
    "school_forecast_desc":    ("avg_school_socio_index_forecast", "DESC"),
}
STRING_SORT_FIELDS = {"listed_date"}   # ISO dates; every other sort field is a number

# query parameter -> (parser, document field, operator), in the data repo's order. `sort`, the radius
# triple and `limit` are handled separately.
FILTERS = {
    "property_type":          (p._parse_property_type, "property_type", "="),
    "object_form":            (p._parse_object_form, "object_form", "="),
    "tenure":                 (p._parse_tenure, "tenure", "="),
    "kommun":                 (p._parse_kommun, "kommun", "="),
    "omrade":                 (p._parse_omrade, "area_name", "="),
    "min_rooms":              (p._parse_rooms, "rooms", ">="),
    "min_price":              (lambda v: p._parse_nonneg_int(v, "min_price"), "asking_price_kr", ">="),
    "max_price":              (lambda v: p._parse_nonneg_int(v, "max_price"), "asking_price_kr", "<="),
    "max_kr_per_m2":          (lambda v: p._parse_nonneg_int(v, "max_kr_per_m2"), "kr_per_m2", "<="),
    "min_area_m2":            (lambda v: p._parse_area(v, "min_area_m2"), "living_area_m2", ">="),
    "max_area_m2":            (lambda v: p._parse_area(v, "max_area_m2"), "living_area_m2", "<="),
    "min_construction_year":  (p._parse_construction_year, "construction_year", ">="),
    "max_construction_year":  (p._parse_construction_year, "construction_year", "<="),
    "max_fee_per_sqm":        (lambda v: p._parse_index(v, "max_fee_per_sqm"), "monthly_fee_per_m2", "<="),
    "max_monthly_fee":        (lambda v: p._parse_nonneg_int(v, "max_monthly_fee"), "monthly_fee_kr", "<="),
    "brf_org_number":         (p._parse_org_number, "brf_org_number", "="),
    "min_brf_units":          (lambda v: p._parse_nonneg_int(v, "min_brf_units"), "brf_num_units", ">="),
    "max_brf_loan_per_m2":    (lambda v: p._parse_nonneg_int(v, "max_brf_loan_per_m2"), "brf_loan_per_m2", "<="),
    "min_brf_trend":          (lambda v: p._parse_int(v, "min_brf_trend"), "brf_kr_per_m2_trend", ">="),
    "max_brf_fee_trend":      (lambda v: p._parse_number(v, "max_brf_fee_trend"), "brf_fee_per_m2_trend", "<="),
    "min_brf_price_trend_5y": (lambda v: p._parse_int(v, "min_brf_price_trend_5y"), "brf_kr_per_m2_trend_5y", ">="),
    "min_brf_price_forecast": (lambda v: p._parse_nonneg_int(v, "min_brf_price_forecast"), "brf_kr_per_m2_forecast", ">="),
    "max_brf_fee_trend_5y":   (lambda v: p._parse_number(v, "max_brf_fee_trend_5y"), "brf_fee_per_m2_trend_5y", "<="),
    "max_brf_fee_forecast":   (lambda v: p._parse_index(v, "max_brf_fee_forecast"), "brf_fee_per_m2_forecast", "<="),
    "min_brf_pct_for_sale":   (lambda v: p._parse_number(v, "min_brf_pct_for_sale"), "brf_pct_for_sale", ">="),
    "max_brf_pct_for_sale":   (lambda v: p._parse_number(v, "max_brf_pct_for_sale"), "brf_pct_for_sale", "<="),
    "min_school_index":       (lambda v: p._parse_index(v, "min_school_index"), "avg_school_socio_index", ">="),
    "min_school_index_1":     (lambda v: p._parse_index(v, "min_school_index_1"), "avg_school_socio_index_1", ">="),
    "min_school_index_3":     (lambda v: p._parse_index(v, "min_school_index_3"), "avg_school_socio_index_3", ">="),
}
SPATIAL_FIELD = "geo"          # GeoJSON point; `location` {lat, lng} is the served form
# rows with an equal sort value are ordered by the data repo's row id, newest first (ORDER BY ..., id DESC).
# Cosmos reserves `id` for the document key (= listing_id), so the row id is stored as `seq`, served as `id`.
TIEBREAK_FIELD = "seq"
HIDDEN_FIELDS = {"id", "geo", "brf_booli_id"}   # brf_booli_id is Booli's internal id; never served


def parse(qs):
    """Validated parameters, or ValueError with the 400 message."""
    args = {name: parser(qs.get(name)) for name, (parser, _, _) in FILTERS.items()}
    args["sort"] = p._parse_sort(qs.get("sort"), SORTS, "listed_date")
    args["near_lat"] = p._parse_coord(qs.get("near_lat"), "near_lat", -90, 90)
    args["near_lng"] = p._parse_coord(qs.get("near_lng"), "near_lng", -180, 180)
    args["radius_m"] = p._parse_radius_m(qs.get("radius_m"))
    args["limit"] = p._parse_limit(qs.get("limit"))
    geo = (args["near_lat"], args["near_lng"], args["radius_m"])
    if any(v is not None for v in geo) and not all(v is not None for v in geo):
        raise ValueError("near_lat, near_lng and radius_m must be provided together")
    return args


def _clean(doc):
    """A stored document as served: Cosmos fields and internal fields dropped, `seq` served as `id`."""
    out = {"id": doc.get("seq")}
    out.update((k, v) for k, v in doc.items() if not k.startswith("_") and k not in HIDDEN_FIELDS | {"seq"})
    return out


def query(container, args):
    """(rows, request charge in RU). Field names come only from SORTS/FILTERS; values are parameters."""
    conds, qparams = [], []
    for i, (name, (_, field, op)) in enumerate(FILTERS.items()):
        if args[name] is not None:
            conds.append(f"c.{field} {op} @f{i}")
            qparams.append({"name": f"@f{i}", "value": args[name]})
    if args["radius_m"] is not None:
        conds.append(f"ST_DISTANCE(c.{SPATIAL_FIELD}, "
                     "{'type': 'Point', 'coordinates': [@lng, @lat]}) <= @radius")
        qparams += [{"name": "@lng", "value": args["near_lng"]}, {"name": "@lat", "value": args["near_lat"]},
                    {"name": "@radius", "value": args["radius_m"]}]
    field, direction = SORTS[args["sort"]]
    has_field = f"{'IS_STRING' if field in STRING_SORT_FIELDS else 'IS_NUMBER'}(c.{field})"
    # A single-kommun request stays in one partition (the container is partitioned by kommun).
    scope = ({"partition_key": args["kommun"]} if args["kommun"] is not None
             else {"enable_cross_partition_query": True})

    def run(where, order, limit):
        sql = (f"SELECT * FROM c WHERE {' AND '.join(where)} ORDER BY {order} "
               f"OFFSET 0 LIMIT {int(limit)}")
        items = list(container.query_items(sql, parameters=qparams, **scope))
        charge = float(container.client_connection.last_response_headers.get("x-ms-request-charge", 0))
        return items, charge

    rows, ru = run(conds + [has_field], f"c.{field} {direction}, c.{TIEBREAK_FIELD} DESC", args["limit"])
    if len(rows) < args["limit"]:   # NULLS LAST: then the rows without the sort field
        rest, ru2 = run(conds + [f"NOT {has_field}"], f"c.{TIEBREAK_FIELD} DESC", args["limit"] - len(rows))
        rows, ru = rows + rest, ru + ru2
    return [_clean(r) for r in rows], ru


def response(rows, args):
    filters = {k: args[k] for k in ("property_type", "object_form", "tenure", "kommun", "omrade", "min_rooms")}
    filters["sort"] = args["sort"]
    filters.update({k: args[k] for k in FILTERS if k not in filters})
    filters.update({k: args[k] for k in ("near_lat", "near_lng", "radius_m", "limit")})
    return {"advertisements": rows, "count": len(rows), "filters": filters}
