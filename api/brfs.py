"""GET /brfs over the `brfs` read model (Cosmos container partitioned by orgnr; type "brf" documents).

The contract is the data repo's: the same parameters, sorts, 400 messages and response shape, one row per BRF
with at least one current listing. homesight adds, for BRFs whose annual reports are extracted, the verified
layer (brf_report_*, brf_land_tenure, brf_stambyte_*), lists those BRFs even when nothing is for sale, and
adds the `has_report` filter.

Every filter and sort is a plain read of a precomputed field (tools/readmodel/brfs.py). The name and kommun
sorts read a rank computed at load time in the data repo's collation (Postgres en_US), which Cosmos' binary
string order cannot reproduce. SORTS and FILTERS also generate the container's indexing policy.
"""
import params as p

# sort key -> (document field, direction); every sort ends with brf_org_number ASC as tiebreak and puts rows
# without the field last (NULLS LAST in the data repo).
SORTS = {
    "name_asc":               ("name_rank", "ASC"),
    "name_desc":              ("name_rank", "DESC"),
    "kommun_asc":             ("kommun_rank", "ASC"),
    "kommun_desc":            ("kommun_rank", "DESC"),
    "units_asc":              ("brf_num_units", "ASC"),
    "units_desc":             ("brf_num_units", "DESC"),
    "construction_year_asc":  ("brf_construction_year", "ASC"),
    "construction_year_desc": ("brf_construction_year", "DESC"),
    "loan_asc":               ("brf_loan_per_m2", "ASC"),
    "loan_desc":              ("brf_loan_per_m2", "DESC"),
    "price_asc":              ("brf_kr_per_m2_2026", "ASC"),
    "price_desc":             ("brf_kr_per_m2_2026", "DESC"),
    "price_trend_asc":        ("brf_kr_per_m2_trend", "ASC"),
    "price_trend_desc":       ("brf_kr_per_m2_trend", "DESC"),
    "price_trend_5y_asc":     ("brf_kr_per_m2_trend_5y", "ASC"),
    "price_trend_5y_desc":    ("brf_kr_per_m2_trend_5y", "DESC"),
    "price_forecast_asc":     ("brf_kr_per_m2_forecast", "ASC"),
    "price_forecast_desc":    ("brf_kr_per_m2_forecast", "DESC"),
    "fee_asc":                ("brf_fee_per_m2_2026", "ASC"),
    "fee_desc":               ("brf_fee_per_m2_2026", "DESC"),
    "fee_trend_asc":          ("brf_fee_per_m2_trend", "ASC"),
    "fee_trend_desc":         ("brf_fee_per_m2_trend", "DESC"),
    "fee_trend_5y_asc":       ("brf_fee_per_m2_trend_5y", "ASC"),
    "fee_trend_5y_desc":      ("brf_fee_per_m2_trend_5y", "DESC"),
    "fee_forecast_asc":       ("brf_fee_per_m2_forecast", "ASC"),
    "fee_forecast_desc":      ("brf_fee_per_m2_forecast", "DESC"),
    "pct_for_sale_asc":       ("brf_pct_for_sale", "ASC"),
    "pct_for_sale_desc":      ("brf_pct_for_sale", "DESC"),
    "active_count_asc":       ("active_listing_count", "ASC"),
    "active_count_desc":      ("active_listing_count", "DESC"),
    "active_avg_price_asc":   ("active_avg_kr_per_m2", "ASC"),
    "active_avg_price_desc":  ("active_avg_kr_per_m2", "DESC"),
}


def _parse_has_report(raw):
    """homesight only: true = BRFs whose annual reports are extracted and verified (the brf_report_* fields
    are set), false = the others."""
    if raw is None:
        return None
    value = raw.strip().lower()
    if value not in ("true", "false"):
        raise ValueError("has_report must be true or false")
    return value == "true"


# query parameter -> (parser, document field, operator), in the data repo's order (`sort` after omrade and
# `limit` last are handled separately). brf_name_search is a case-insensitive substring.
FILTERS = {
    "brf_name_search":        (p._parse_brf_name_search, "brf_name", "CONTAINS"),
    "kommun":                 (p._parse_kommun, "kommun", "="),
    "omrade":                 (p._parse_omrade, "area_name", "="),
    "min_brf_units":          (lambda v: p._parse_nonneg_int(v, "min_brf_units"), "brf_num_units", ">="),
    "min_construction_year":  (p._parse_construction_year, "brf_construction_year", ">="),
    "max_construction_year":  (p._parse_construction_year, "brf_construction_year", "<="),
    "max_brf_loan_per_m2":    (lambda v: p._parse_nonneg_int(v, "max_brf_loan_per_m2"), "brf_loan_per_m2", "<="),
    "min_brf_price_trend_5y": (lambda v: p._parse_int(v, "min_brf_price_trend_5y"), "brf_kr_per_m2_trend_5y", ">="),
    "min_brf_price_forecast": (lambda v: p._parse_nonneg_int(v, "min_brf_price_forecast"), "brf_kr_per_m2_forecast", ">="),
    "max_brf_fee_trend_5y":   (lambda v: p._parse_number(v, "max_brf_fee_trend_5y"), "brf_fee_per_m2_trend_5y", "<="),
    "max_brf_fee_forecast":   (lambda v: p._parse_index(v, "max_brf_fee_forecast"), "brf_fee_per_m2_forecast", "<="),
    "min_brf_pct_for_sale":   (lambda v: p._parse_number(v, "min_brf_pct_for_sale"), "brf_pct_for_sale", ">="),
    "max_brf_pct_for_sale":   (lambda v: p._parse_number(v, "max_brf_pct_for_sale"), "brf_pct_for_sale", "<="),
    "min_active_listings":    (lambda v: p._parse_nonneg_int(v, "min_active_listings"), "active_listing_count", ">="),
    "has_report":             (_parse_has_report, "brf_report_available", "="),
}
TIEBREAK_FIELD = "brf_org_number"
HIDDEN_FIELDS = {"id", "type", "orgnr", "name_rank", "kommun_rank", "brf_report_available", "report"}


def parse(qs):
    """Validated parameters, or ValueError with the 400 message."""
    args = {name: parser(qs.get(name)) for name, (parser, _, _) in FILTERS.items()}
    args["sort"] = p._parse_sort(qs.get("sort"), SORTS, "name_asc")
    args["limit"] = p._parse_limit(qs.get("limit"))
    return args


def clean(doc, hidden=HIDDEN_FIELDS):
    return {k: v for k, v in doc.items() if not k.startswith("_") and k not in hidden}


def query(container, args, qs):
    """(rows, request charge in RU). Field names come only from SORTS/FILTERS; values are parameters."""
    conds, qparams = ["c.type = 'brf'"], []
    for i, (name, (_, field, op)) in enumerate(FILTERS.items()):
        if args[name] is None:
            continue
        if op == "CONTAINS":
            # ILIKE '%term%' in the data repo: a literal, case-insensitive substring (the parser's escaped
            # form is only echoed)
            conds.append(f"CONTAINS(c.{field}, @f{i}, true)")
            qparams.append({"name": f"@f{i}", "value": qs.get(name).strip()})
        else:
            conds.append(f"c.{field} {op} @f{i}")
            qparams.append({"name": f"@f{i}", "value": args[name]})
    field, direction = SORTS[args["sort"]]
    has_field = f"IS_NUMBER(c.{field})"

    def run(where, order, limit):
        sql = (f"SELECT * FROM c WHERE {' AND '.join(where)} ORDER BY {order} "
               f"OFFSET 0 LIMIT {int(limit)}")
        items = list(container.query_items(sql, parameters=qparams, enable_cross_partition_query=True))
        charge = float(container.client_connection.last_response_headers.get("x-ms-request-charge", 0))
        return items, charge

    rows, ru = run(conds + [has_field], f"c.{field} {direction}, c.{TIEBREAK_FIELD} ASC", args["limit"])
    if len(rows) < args["limit"]:   # NULLS LAST: then the rows without the sort field
        rest, ru2 = run(conds + [f"NOT {has_field}"], f"c.{TIEBREAK_FIELD} ASC", args["limit"] - len(rows))
        rows, ru = rows + rest, ru + ru2
    return [clean(r) for r in rows], ru


def response(rows, args):
    filters = {k: args[k] for k in ("brf_name_search", "kommun", "omrade")}
    filters["sort"] = args["sort"]
    filters.update({k: args[k] for k in FILTERS if k not in filters})
    filters["limit"] = args["limit"]
    return {"brfs": rows, "count": len(rows), "filters": filters}
