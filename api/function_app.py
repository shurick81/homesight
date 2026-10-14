"""homesight API: read-only access to the BRF data, built for LLM agents.

Azure Functions (Python v2 model) on Flex Consumption, reading Cosmos DB (serverless) with the app's
managed identity. The spec is served at /openapi.json. All endpoints are public (anonymous); the cost of
traffic is bounded by the instance cap in infra/main.bicep.

Read model: one Cosmos container per resource, each document a finished response row (built at load time
by tools/readmodel/), so a request is one indexed query with no joins or computation. COSMOS_DATABASE
names the live copy (homesight_blue or homesight_green); see tools/cloud/provision.py.
"""
import json
import logging
import os
import re
import time
from pathlib import Path

import azure.functions as func

import advertisements
import brfs
from params import _parse_brf_name_search, _parse_kommun, _parse_rooms, _parse_year

API_VERSION = "0.11.4"
HERE = Path(__file__).parent
ORGNR_RE = re.compile(r"^\d{6}-\d{4}$")
CORS = {"Access-Control-Allow-Origin": "*"}   # public read-only data, no credentials

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

_database = None


def container(name):
    """Cosmos container client for one resource; the client is created once per instance."""
    global _database
    if _database is None:
        from azure.cosmos import CosmosClient
        from azure.identity import DefaultAzureCredential

        client = CosmosClient(os.environ["COSMOS_ENDPOINT"], credential=DefaultAzureCredential())
        _database = client.get_database_client(os.environ["COSMOS_DATABASE"])
    return _database.get_container_client(name)


def _clean(doc):
    """Drop Cosmos system fields and the storage-only id/type."""
    return {k: v for k, v in doc.items() if not k.startswith("_") and k not in ("id", "type")}


def _json(body, status=200):
    return func.HttpResponse(json.dumps(body, ensure_ascii=False, default=str), status_code=status,
                             mimetype="application/json", charset="utf-8", headers=CORS)


def _error(status, message):
    return _json({"error": message}, status)


@app.route(route="health", methods=["GET"])
def health(req: func.HttpRequest) -> func.HttpResponse:
    return _json({"status": "ok", "version": API_VERSION})


BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:7071")


def _load_spec():
    """The spec as served: version from API_VERSION, server from PUBLIC_BASE_URL (set by infra/main.bicep)."""
    spec = json.loads((HERE / "openapi.json").read_text(encoding="utf-8"))
    spec["info"]["version"] = API_VERSION
    spec["servers"] = [{"url": BASE_URL}]
    return json.dumps(spec, ensure_ascii=False)


SPEC = _load_spec()


@app.route(route="openapi.json", methods=["GET"])
def openapi(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse(SPEC, mimetype="application/json", charset="utf-8",
                             headers={"Cache-Control": "public, max-age=3600"})


@app.route(route="brfs", methods=["GET"])
def list_brfs(req: func.HttpRequest) -> func.HttpResponse:
    start = time.perf_counter()
    try:
        args = brfs.parse(req.params)
    except ValueError as e:
        return _error(400, str(e))
    rows, ru = brfs.query(container("brfs"), args, req.params)
    logging.info(json.dumps({"type": "timing", "path": "/brfs", "sort": args["sort"],
                             "filters": [k for k in brfs.FILTERS if args[k] is not None],
                             "rows": len(rows), "ru": round(ru, 2),
                             "total_ms": round((time.perf_counter() - start) * 1000, 1)}))
    return _json(brfs.response(rows, args))


def _brf_row(orgnr):
    """The BRF's "brf" document (a point read), or None."""
    from azure.cosmos.exceptions import CosmosResourceNotFoundError

    try:
        return container("brfs").read_item(item="brf", partition_key=orgnr)
    except CosmosResourceNotFoundError:
        return None


@app.route(route="brfs/{orgnr}", methods=["GET"])
def get_brf(req: func.HttpRequest) -> func.HttpResponse:
    """The BRF's /brfs row plus `report`: the summary of its extracted annual reports (null if none)."""
    orgnr = req.route_params.get("orgnr", "")
    if not ORGNR_RE.match(orgnr):
        return _error(400, "orgnr must be a Swedish organisationsnummer like 712400-1467")
    doc = _brf_row(orgnr)
    if doc is None:
        return _error(404, f"no BRF with orgnr {orgnr} in this dataset: GET /brfs covers BRFs with an apartment "
                           f"for sale in the covered kommuns, and those with extracted annual reports")
    return _json(brfs.clean(doc, brfs.HIDDEN_FIELDS - {"report"}))


@app.route(route="brfs/{orgnr}/financials", methods=["GET"])
def get_brf_financials(req: func.HttpRequest) -> func.HttpResponse:
    """The BRF's verified annual-report figures, one entry per fiscal year, newest first. One query in the
    BRF's partition; `values` (where in the report each number is) only with ?include=values."""
    orgnr = req.route_params.get("orgnr", "")
    if not ORGNR_RE.match(orgnr):
        return _error(400, "orgnr must be a Swedish organisationsnummer like 712400-1467")
    include = {x.strip() for x in (req.params.get("include") or "").split(",") if x.strip()}
    if include - {"values"}:
        return _error(400, "include must be: values")
    docs = list(container("brfs").query_items(
        "SELECT * FROM c WHERE c.type IN ('financial_year', 'brf')", partition_key=orgnr))
    report = next((d.get("report") for d in docs if d["type"] == "brf"), None)
    if report is None:
        return _error(404, f"no extracted annual reports for orgnr {orgnr}; GET /brfs?has_report=true lists "
                           f"the BRFs that have them")
    years = sorted((d for d in docs if d["type"] == "financial_year"), key=lambda d: -d["fiscal_year"])
    drop = {"orgnr", "name"} | (set() if "values" in include else {"values"})
    return _json({"orgnr": orgnr, "name": report.get("name"),
                  "verified_through": report.get("verified_through"), "count": len(years),
                  "years": [{k: v for k, v in _clean(d).items() if k not in drop} for d in years]})


@app.route(route="advertisements", methods=["GET"])
def list_advertisements(req: func.HttpRequest) -> func.HttpResponse:
    start = time.perf_counter()
    try:
        args = advertisements.parse(req.params)
    except ValueError as e:
        return _error(400, str(e))
    rows, ru = advertisements.query(container("advertisements"), args)
    # one structured line per request: what was asked and what it cost (Application Insights traces)
    logging.info(json.dumps({"type": "timing", "path": "/advertisements", "sort": args["sort"],
                             "filters": [k for k in advertisements.FILTERS if args[k] is not None],
                             "rows": len(rows), "ru": round(ru, 2),
                             "total_ms": round((time.perf_counter() - start) * 1000, 1)}))
    return _json(advertisements.response(rows, args))


# ---------- the geo hierarchy: GET /cities, /kommuns, /areas (the data repo's contract) ----------

def _levels(name, query="SELECT * FROM c", parameters=None, partition_key=None):
    """Every row of a geo container, in the data API's order (stored as `order` at load time)."""
    kwargs = {"partition_key": partition_key} if partition_key else {"enable_cross_partition_query": True}
    docs = container(name).query_items(query, parameters=parameters or [], **kwargs)
    return [{k: v for k, v in d.items() if not k.startswith("_") and k not in ("id", "order")}
            for d in sorted(docs, key=lambda d: d["order"])]


@app.route(route="cities", methods=["GET"])
def list_cities(req: func.HttpRequest) -> func.HttpResponse:
    rows = _levels("cities")
    return _json({"cities": rows, "count": len(rows)})


@app.route(route="kommuns", methods=["GET"])
def list_kommuns(req: func.HttpRequest) -> func.HttpResponse:
    rows = _levels("kommuns")
    return _json({"kommuns": rows, "count": len(rows)})


@app.route(route="areas", methods=["GET"])
def list_areas(req: func.HttpRequest) -> func.HttpResponse:
    try:
        kommun = _parse_kommun(req.params.get("kommun"))
        name_search = _parse_brf_name_search(req.params.get("name_search"))  # echoed escaped, as the data API
    except ValueError as e:
        return _error(400, str(e))
    where, parameters = [], []
    if name_search is not None:
        # ILIKE '%term%': a case-insensitive literal substring
        where.append("CONTAINS(c.area_name, @q, true)")
        parameters.append({"name": "@q", "value": req.params.get("name_search").strip()})
    query = "SELECT * FROM c" + (" WHERE " + " AND ".join(where) if where else "")
    rows = _levels("areas", query, parameters, partition_key=kommun)
    return _json({"areas": rows, "count": len(rows), "filters": {"kommun": kommun, "name_search": name_search}})


# ---------- GET /sales/stats/avg-price-per-sqm (the data repo's contract) ----------

@app.route(route="sales/stats/avg-price-per-sqm", methods=["GET"])
def avg_price_per_sqm(req: func.HttpRequest) -> func.HttpResponse:
    """Average sold kr/m², optionally for one year and/or room count: a point read of the precomputed
    combination (tools/readmodel/sales_stats.py); no document means no sales match."""
    from azure.cosmos.exceptions import CosmosResourceNotFoundError

    try:
        year = _parse_year(req.params.get("year"))
        rooms = _parse_rooms(req.params.get("rooms"))
    except ValueError as e:
        return _error(400, str(e))
    doc_id = f"{'all' if year is None else year}_{'all' if rooms is None else float(rooms)}"
    try:
        doc = container("sales_stats").read_item(item=doc_id, partition_key=doc_id)
    except CosmosResourceNotFoundError:
        return _error(404, "no data found for the given filters")
    body = {k: v for k, v in doc.items() if not k.startswith("_") and k != "id"}
    body["filters"]["rooms"] = rooms   # Cosmos returns 2.0 as 2; the data API echoes the parsed float
    return _json(body)


# ---------- GET /llms.txt: a short guide for LLM agents ----------
# (No GET / discovery: a catch-all route "{*path}" takes precedence over the single-segment routes on this
# host, so every /<name> endpoint answered 404 in 0.7.0's first deploy.)

LLMS_TXT = (HERE / "llms.txt").read_text(encoding="utf-8").replace("{BASE}", BASE_URL)


@app.route(route="llms.txt", methods=["GET"])
def llms_txt(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse(LLMS_TXT, mimetype="text/markdown", charset="utf-8",
                             headers={**CORS, "Cache-Control": "public, max-age=3600"})
