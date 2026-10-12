#!/usr/bin/env python3
"""Scrape sold listings (slutpriser) and listings for sale from Booli.

Ported from the data repo (src/extract/booli/scrape_booli_sales.py, commit 1864e38), with homesight's
paths: it reads and writes the CSVs in .cache/booli/work/, which tools/booli/refresh.py unpacks from and
packs back into market/booli/*.csv.gz; tools/readmodel/ builds the API's read model from those.

A single GraphQL call (getHousingCoopSold) returns the BRF's complete
sold-listing history in one shot — no pagination. Booli is behind Cloudflare,
so we drive it with Playwright + stealth: load the BRF page once, intercept the
GraphQL response, parse it.

The getHousingCoopSold card exposes: sold date/price, kr/m², rooms, living
area, object type, agency, apartment number, the listing URL, and the
property's WGS84 latitude/longitude. It does NOT expose asking price, monthly
fee, price change %, floor, construction year, or amenities — those live on
each listing's detail page (/bostad/{id}) and would need a per-listing fetch.

Modes:
  (default)       BRF-scoped: fetch one förening's full history via Playwright
                  (getHousingCoopSold), write per-listing CSV + raw JSON
  --area-id ID    Kommun-wide: scrape ALL sales in an area (all property types,
                  many BRFs). Self-discovering — intercepts every GraphQL
                  response, detects sold cards, paginates by scrolling, and
                  archives a sample payload so the card shape can be learned.
  --from-raw PATH rebuild the CSV from a previously saved raw JSON payload
                  (no Playwright, no network — handy for re-runs and for
                  backfilling new columns onto an existing capture)
"""

import argparse
import csv
import json
import math
import os
import random
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

BRF_URL_TPL = "https://www.booli.se/bostadsrattsforening/{id}"
GRAPHQL_OP = "getHousingCoopSold"

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]              # tools/booli -> repo root
# homesight: the canonical CSVs live gzipped in market/booli/; tools/booli/refresh.py unpacks them into
# WORK before a run and packs them back after. Caches (id-keyed: what was fetched is not fetched again)
# and raw captures are local runtime state, not in git.
WORK = REPO_ROOT / ".cache" / "booli" / "work"
PROCESSED = WORK
RAW = REPO_ROOT / ".cache" / "booli" / "raw"
CACHE_DIR = REPO_ROOT / ".cache" / "booli" / "cache"
DEFAULT_CSV = PROCESSED / "sales_booli.csv"
DEFAULT_RAW = RAW / "sales_booli_raw.json"
BRF_CACHE = CACHE_DIR / "brf_enrich_cache.json"
DEFAULT_BRF_DETAILS = PROCESSED / "brf_details_booli.csv"
DEFAULT_BRF_RESIDENCES = PROCESSED / "brf_residences_booli.csv"
DEFAULT_ADS_CSV = PROCESSED / "advertisements_booli.csv"
BRF_DETAILS_CACHE = CACHE_DIR / "brf_details_cache.json"
DETAILS_CACHE = CACHE_DIR / "details_cache.json"
SOLD_DETAILS_CACHE = CACHE_DIR / "sold_details_cache.json"

# Jittered (min, max) millisecond pause between per-listing detail fetches in the
# enrichers. Booli rate-throttles a sustained burst of sub-second fetches into a
# footprint-wide block after ~1-1.5k pages; pacing to ~2s keeps a multi-thousand
# backfill under the limit so it completes unattended instead of dying partway.
# Tunable per-run via env (dial UP if a run still trips the throttle):
#   ENRICH_PACE_MIN_MS / ENRICH_PACE_MAX_MS
ENRICH_PACE_MS = (int(os.environ.get("ENRICH_PACE_MIN_MS", "1200")),
                  int(os.environ.get("ENRICH_PACE_MAX_MS", "2600")))
# Bail out of the avgift backfill after this many CONSECUTIVE challenged/error
# fetches — a run of them means the IP tripped a Cloudflare block, so every further
# fetch is a futile hit that only keeps the automation flag warm (July 2026: a
# blocked run ground through ~10k listings before it was killed). Sporadic single
# challenges on a healthy IP won't reach the streak; a real block does within seconds.
SOLD_ENRICH_ABORT_STREAK = int(os.environ.get("SOLD_ENRICH_ABORT_STREAK", "15"))
# Opt-in per-fetch event log for the avgift backfill. When SOLD_ENRICH_EVENTLOG names
# a path, enrich_sold_details appends one JSONL record per detail fetch (listing_id,
# status, fee, latency, attempts) for throttle/latency analysis. No-op when unset, so
# it never affects normal runs.
SOLD_ENRICH_EVENTLOG = os.environ.get("SOLD_ENRICH_EVENTLOG")


def _append_sold_event(record: dict) -> None:
    """Append one JSONL record to SOLD_ENRICH_EVENTLOG (no-op if unset / on error)."""
    if not SOLD_ENRICH_EVENTLOG:
        return
    try:
        with open(SOLD_ENRICH_EVENTLOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _proxy_opts():
    """Playwright proxy config from env, or None when unset (direct connection).

    Booli is behind Cloudflare, which hard-blocks datacenter IP ranges — so AWS
    egress / Elastic-IP rotation does NOT work, and even residential IPs get
    rate-clamped footprint-wide after heavy volume. The reliable path for bulk
    runs is a residential/mobile proxy pool: set BOOLI_PROXY_SERVER (e.g.
    "http://gate.provider.com:7000") plus BOOLI_PROXY_USERNAME / BOOLI_PROXY_PASSWORD
    and every browser launch routes through it. Compute can then live anywhere
    (Fargate, EC2, laptop); only the outbound IP reputation matters. Pacing still
    applies so no single exit IP is burned.
    """
    server = os.environ.get("BOOLI_PROXY_SERVER")
    if not _PROXY_LOGGED[0]:
        _PROXY_LOGGED[0] = True
        print(f"  proxy: {'ON → ' + server if server else 'OFF (direct connection)'}",
              flush=True)
    if not server:
        return None
    opts = {"server": server}
    if os.environ.get("BOOLI_PROXY_USERNAME"):
        opts["username"] = os.environ["BOOLI_PROXY_USERNAME"]
    if os.environ.get("BOOLI_PROXY_PASSWORD"):
        opts["password"] = os.environ["BOOLI_PROXY_PASSWORD"]
    return opts


_PROXY_LOGGED = [False]   # one-shot guard so the proxy mode is logged once per run


# Current for-sale listings (advertisements table).
ADVERT_COLUMNS = [
    "listing_id", "listed_date", "asking_price_kr", "property_type",
    "object_form", "tenure",
    "address", "street_number", "apartment_number",
    "rooms", "living_area_m2", "kr_per_m2", "monthly_fee_kr", "construction_year",
    "broker_agency", "kommun", "area_name", "lat", "lng", "source_url",
    "attributes_json", "photo_urls",
]

BRF_DETAILS_COLUMNS = [
    "booli_brf_id", "name", "org_number",
    "construction_year", "num_units", "num_rental_units",
    "total_living_area_m2", "plot_ownership", "is_genuine",
    "total_loan_kr", "annual_report_year", "has_valid_annual_report",
    "economy_json", "description", "num_addresses", "addresses_json",
]

# Full unit inventory of a BRF (every apartment), one row per residence.
BRF_RESIDENCE_COLUMNS = [
    "booli_brf_id", "residence_booli_id", "address", "street_number",
    "apartment_number", "rooms", "living_area_m2", "floor", "cadastral",
    "lat", "lng", "url",
]

CSV_COLUMNS = [
    "sold_date",
    "address",
    "street_number",
    "rooms",
    "living_area_m2",
    "final_price_kr",
    "asking_price_kr",      # not exposed by getHousingCoopSold
    "kr_per_m2",
    "monthly_fee_kr",       # not exposed by getHousingCoopSold
    "price_change_pct",     # not exposed by getHousingCoopSold
    "broker_agency",
    "listing_id",
    "booli_url",
    "apartment_number",
    "object_type",          # conflated deals.property_type enum (derived; back-compat)
    "object_form",          # raw physical form (lägenhet/radhus/parhus/villa/…) — one axis
    "tenure",               # property-right: bostadsrätt / äganderätt — the other axis
    "lat",                  # WGS84 latitude  (deals.location)
    "lng",                  # WGS84 longitude (deals.location)
    "kommun",               # municipality (deals.kommun)
    "area_name",            # Booli descriptiveAreaName — sub-kommun område/stadsdel
    "booli_brf_id",         # Booli housingCoopId — natural key for brfs
    "brf_name",             # förening name
]

# Booli objectType (Swedish) -> deals.property_type enum. Unknown values map to
# None so the row imports without a type rather than a wrong one; the area
# scraper logs any unmapped value so we can extend this from real data.
OBJECT_TYPE_MAP = {
    "lägenhet": "bostadsrätt",
    "bostadsrätt": "bostadsrätt",
    "villa": "villa",
    "villa/radhus": "villa",
    "hus": "villa",
    "gård": "villa",
    "radhus": "radhus",
    "kedjehus": "radhus",
    "parhus": "radhus",
    "fritidshus": "fritidshus",
    # Intentionally unmapped (not a dwelling type): "tomt/mark", "övrigt".
}

# Booli's kommun-wide searches. areaId targets the kommun (e.g. Tyresö).
SOLD_SEARCH_URL_TPL = "https://www.booli.se/sok/slutpriser?areaIds={area_id}"
FORSALE_URL_TPL = "https://www.booli.se/sok/till-salu?areaIds={area_id}"

# Booli serves listing photos from its CDN; a card carries only image refs (as
# Apollo refs like "Image:53983751"), while the Image entity in the Apollo state
# holds each photo's primaryLabel ("floorplan", "interior", "exterior", …) and
# Swedish alt text. The public URL is built from the id — 1440px wide, height
# auto (the "0" in 1440x0). _listing_photos turns a card's primaryImage + images
# refs into a list of {url, primaryLabel, alt} dicts. The kommun list view only
# exposes images({"limit":5}); the listing detail page carries the full `images`
# gallery, so enrich_details fetches one detail page per listing.
IMAGE_URL_TPL = "https://bcdn.se/images/cache/{id}_1440x0.webp"

# This BRF-scoped scrape (getHousingCoopSold) is constant for every row: the
# förening and kommun are the same throughout, and the name isn't in the
# payload. An area/kommun-wide scrape would instead derive these per row.
DEFAULT_KOMMUN = "Tyresö"
DEFAULT_BRF_NAME = "HSB Bostadsrättsförening Sjötungan i Tyresö"


# Personal data is dropped from records before anything is persisted.
# Booli's GraphQL response includes the agent's name, profile-page URL
# (often email-derived), face-photo URL, and personal ID. None are
# needed for price analysis and we have no lawful basis under GDPR to
# retain them. Only the agency (a legal entity) is kept.
PERSONAL_AGENT_FIELDS = ("agent", "agentId")


def strip_personal(records: list) -> list:
    return [{k: v for k, v in r.items() if k not in PERSONAL_AGENT_FIELDS}
            for r in records]


def parse_float_swedish(value) -> Optional[float]:
    """Parse '82,5 m²' or '3 rum' → float."""
    if value is None:
        return None
    s = str(value).replace(",", ".").replace("\xa0", " ")
    m = re.search(r"[\d.]+", s)
    return float(m.group()) if m else None


def make_street_number_extractor(street_name: str):
    """Build an extractor that pulls the integer street number after a street
    name. 'ä'/'a' are interchangeable in the match (Booli sometimes serves
    either form)."""
    flexible = re.escape(street_name).replace("ä", "[äa]").replace("Ä", "[ÄA]")
    pattern = re.compile(rf"{flexible}\s+(\d+)", re.IGNORECASE)

    def _extract(address: str) -> Optional[int]:
        m = pattern.search(address or "")
        return int(m.group(1)) if m else None

    return _extract


def fetch_brf_sold(brf_id: int, timeout_ms: int = 20000) -> dict:
    """Load the BRF page in headless Chromium and capture the
    getHousingCoopSold GraphQL response. Returns the parsed JSON
    payload (with top-level 'data' key)."""
    # Imported lazily so --from-raw works without Playwright installed.
    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth

    captured: dict = {"payload": None}

    def on_response(resp):
        if GRAPHQL_OP not in resp.url:
            return
        ctype = resp.headers.get("content-type", "")
        if "json" not in ctype:
            return
        try:
            captured["payload"] = resp.json()
        except Exception as e:
            print(f"  failed to parse {GRAPHQL_OP} response: {e}")

    with Stealth().use_sync(sync_playwright()) as pw:
        browser = pw.chromium.launch(headless=True, proxy=_proxy_opts())
        ctx = browser.new_context(
            locale="sv-SE",
            timezone_id="Europe/Stockholm",
            viewport={"width": 1366, "height": 850},
        )
        page = ctx.new_page()
        page.on("response", on_response)

        url = BRF_URL_TPL.format(id=brf_id)
        print(f"GET {url} …")
        page.goto(url, wait_until="domcontentloaded", timeout=45000)

        # Trigger the slutpriser section in case it's behind a hash anchor.
        try:
            page.evaluate("location.hash = '#sold'")
        except Exception:
            pass

        # Poll for the captured payload.
        waited = 0
        step = 250
        while waited < timeout_ms and captured["payload"] is None:
            page.wait_for_timeout(step)
            waited += step

        browser.close()

    if not captured["payload"]:
        raise RuntimeError(
            f"Did not capture {GRAPHQL_OP} response within {timeout_ms}ms")
    return captured["payload"]


def map_object_type(raw, unmapped: Optional[set] = None) -> Optional[str]:
    """Map a Booli objectType to the deals.property_type enum (None if unknown)."""
    if not raw:
        return None
    mapped = OBJECT_TYPE_MAP.get(str(raw).strip().lower())
    if mapped is None and unmapped is not None:
        unmapped.add(raw)
    return mapped


# The two ORTHOGONAL axes Booli carries on every card, kept lossless as their own
# columns (the conflated property_type/object_type enum above is derived from the
# pair and retained for back-compat). See docs — object form (what it physically
# is) and tenure (how it's owned) vary independently: a radhus can be sold as
# bostadsrätt (in a BRF) or äganderätt (freehold), so neither derives from the other.
def normalize_object_form(raw) -> Optional[str]:
    """Booli `objectType` -> lowercase physical dwelling form, PRESERVING Booli's
    distinctions (radhus/parhus/kedjehus stay separate — collapse at read time, not
    here). Non-dwelling types (tomt/mark, övrigt) and empties return None."""
    if not raw:
        return None
    form = str(raw).strip().lower()
    if form in ("tomt/mark", "tomt", "mark", "övrigt", ""):
        return None
    return form


def normalize_tenure(raw) -> Optional[str]:
    """Booli `tenureForm` -> lowercase property-right (tenure): 'bostadsrätt'
    (a BRF unit) or 'äganderätt' (freehold). Unknown non-empty values (e.g.
    'hyresrätt', 'tomträtt') are kept verbatim so they stay visible; None when absent."""
    if not raw:
        return None
    t = str(raw).strip().lower()
    if t in ("bostadsrätt", "bostadsratt"):
        return "bostadsrätt"
    if t in ("äganderätt", "aganderatt", "äganderatt"):
        return "äganderätt"
    return t


def _card_brf(card: dict):
    """Best-effort (booli_brf_id, brf_name) from a card — shapes vary by query,
    so probe a few likely paths. Returns (None, None) when absent."""
    coop = card.get("housingCooperative") or card.get("housingCoop") or {}
    if isinstance(coop, dict) and (coop.get("id") or coop.get("name")):
        cid = coop.get("id")
        return (str(cid) if cid is not None else None, coop.get("name"))
    cid = card.get("housingCoopId")
    return (str(cid) if cid is not None else None, None)


def clean_area_name(raw, kommun=None) -> Optional[str]:
    """Booli's descriptiveAreaName is the sub-kommun område/stadsdel
    (e.g. 'Trollbäcken', 'Centrala Tyresö'). Drop self-referential labels that are
    just the kommun itself ('Tyresö', 'Tyresö kommun') so the column carries only
    genuine sub-kommun areas. Returns None when absent."""
    name = (raw or "").strip()
    if not name:
        return None
    k = (kommun or "").strip()
    if k and name.casefold() in (k.casefold(), f"{k} kommun".casefold()):
        return None
    return name


def card_to_row(card: dict, extract_street_number, unmapped: Optional[set] = None) -> dict:
    """Map one Booli SoldProperty record to our CSV schema. Per-card kommun/BRF
    are filled when the card carries them (kommun-wide query) and left None
    otherwise (BRF-scoped query stamps them afterward)."""
    address = ((card.get("location") or {}).get("address") or {}).get("streetAddress", "") or ""
    region = ((card.get("location") or {}).get("region") or {})
    rel_url = card.get("url") or ""
    apt = card.get("apartmentNumber") or {}
    brf_id, brf_name = _card_brf(card)
    return {
        "sold_date": card.get("soldDate"),
        "address": address,
        "street_number": extract_street_number(address),
        "rooms": (card.get("rooms") or {}).get("raw"),
        "living_area_m2": parse_float_swedish((card.get("livingArea") or {}).get("formatted")),
        "final_price_kr": (card.get("soldPrice") or {}).get("raw"),
        "asking_price_kr": None,
        "kr_per_m2": (card.get("soldSqmPrice") or {}).get("raw"),
        "monthly_fee_kr": None,
        "price_change_pct": None,
        "broker_agency": (card.get("agency") or {}).get("name"),
        "listing_id": card.get("booliId"),
        "booli_url": f"https://www.booli.se{rel_url}" if rel_url else None,
        "apartment_number": apt.get("formatted") if isinstance(apt, dict) else None,
        "object_type": map_object_type(card.get("objectType"), unmapped),
        "object_form": normalize_object_form(card.get("objectType")),
        "tenure": normalize_tenure(card.get("tenureForm")),
        "lat": card.get("latitude"),
        "lng": card.get("longitude"),
        "kommun": region.get("municipalityName") if isinstance(region, dict) else None,
        "area_name": clean_area_name(card.get("descriptiveAreaName")),
        "booli_brf_id": brf_id,
        "brf_name": brf_name,
    }


def write_csv(rows: list, out_csv: Path) -> None:
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    rows.sort(key=lambda r: r["sold_date"] or "", reverse=True)
    with open(out_csv, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    located = sum(1 for r in rows if r.get("lat") is not None)
    print(f"CSV written to {out_csv} ({len(rows)} rows, {located} with coordinates)")


def rows_from_payload(payload: dict, street_name: str, brf_id, brf_name: str,
                      kommun: str) -> list:
    sold = strip_personal((payload.get("data") or {}).get("sold") or [])
    extract_street_number = make_street_number_extractor(street_name)
    rows = [card_to_row(c, extract_street_number) for c in sold]
    for r in rows:
        r["kommun"] = kommun
        r["area_name"] = clean_area_name(r["area_name"], kommun)
        r["booli_brf_id"] = str(brf_id)
        r["brf_name"] = brf_name
    return rows


# ---------------------------------------------------------------------------
# Kommun-wide area scrape (all Tyresö sales, not one BRF). Self-discovering:
# we don't know the operation name or card shape up front, so we intercept all
# GraphQL responses, detect sold-property cards heuristically, paginate by
# scrolling, and archive a raw sample so the first run teaches us the schema.
# ---------------------------------------------------------------------------

_FIRST_NUMBER = re.compile(r"\d+")


def general_street_number(address: str) -> Optional[int]:
    """Street number = first integer in the address ('Storängsvägen 12 B' -> 12)."""
    m = _FIRST_NUMBER.search(address or "")
    return int(m.group()) if m else None


def parse_int_swedish(value) -> Optional[int]:
    """Parse '57 200 kr/m²' or '5 495 000 kr' -> int (digits of the first run)."""
    if value is None:
        return None
    m = re.search(r"[\d \xa0 ]+", str(value))
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group())
    return int(digits) if digits else None


def _parse_circle(spec: str) -> tuple:
    """Parse a '--circle lat,lon,radius_km' spec -> (lat, lon, radius_km) floats.
    Booli has no radius search, so this only drives a client-side distance filter
    over scraped cards. Raises ValueError on a malformed spec."""
    parts = [p.strip() for p in str(spec).split(",")]
    if len(parts) != 3:
        raise ValueError(f"--circle must be 'lat,lon,radius_km', got {spec!r}")
    lat, lon, radius_km = (float(parts[0]), float(parts[1]), float(parts[2]))
    if radius_km <= 0:
        raise ValueError(f"--circle radius must be > 0, got {radius_km}")
    return lat, lon, radius_km


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance in kilometres between two WGS84 points."""
    r = 6371.0088  # mean earth radius (km)
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def _filter_rows_to_circle(rows: list, center_lat: float, center_lon: float,
                           radius_km: float) -> list:
    """Keep rows within radius_km of the centre. Rows with no lat/lng are dropped —
    a listing whose coordinate is unknown can't be proven inside the circle. Prints
    kept / outside / no-coordinate counts so a surprising drop rate is visible."""
    kept, outside, no_coord = [], 0, 0
    for r in rows:
        lat, lng = r.get("lat"), r.get("lng")
        if lat is None or lng is None:
            no_coord += 1
            continue
        if _haversine_km(center_lat, center_lon, lat, lng) <= radius_km:
            kept.append(r)
        else:
            outside += 1
    print(f"Circle filter ({radius_km} km @ {center_lat},{center_lon}): "
          f"{len(kept)} kept, {outside} outside, {no_coord} dropped (no coordinates)")
    return kept


def _serp_display_points(card: dict):
    """Pull (rooms, living_area_m2, kr_per_m2, monthly_fee_kr) out of a SERP
    card's displayAttributes, which carries them only as display text. The
    monthly fee (avgift) shows up as a 'kr/mån' data point on bostadsrätt
    listings; villas carry none, so it stays None."""
    # Pages Router cards keyed the block by queryContext; App Router
    # ListableProperty / detail Listing objects use a plain `displayAttributes`.
    da = (card.get('displayAttributes({"queryContext":"SERP_LIST_LISTING"})')
          or card.get("displayAttributes") or {})
    rooms = area = sqm = fee = None
    for p in da.get("dataPoints", []):
        text = (p.get("value") or {}).get("plainText") or ""
        tl = text.lower()
        # "kr/mån" must be tested before "kr/m" — the per-m² substring also
        # matches the monthly-fee text, so the fee has to be claimed first.
        if "kr/mån" in tl:
            fee = parse_int_swedish(text)
        elif "kr/m" in tl:
            sqm = parse_int_swedish(text)
        elif "tomt" in tl:
            continue  # plot area — not captured
        elif "rum" in tl:
            rooms = parse_float_swedish(text)
        elif "m²" in tl or "m2" in tl:
            area = parse_float_swedish(text)
    return rooms, area, sqm, fee


_IMAGE_REF_RE = re.compile(r"Image:(\d+)")


def _listing_photos(card: dict, state: Optional[dict] = None) -> list:
    """Build the listing's photos from a card's image refs. Booli carries only
    refs (Apollo refs like "Image:53983751") on the card; the CDN URL is derived
    from each id and the photo's category (primaryLabel, e.g. "floorplan",
    "interior", "exterior") + Swedish alt text are read from the Image entity in
    the Apollo `state`. Reads the detail-page `images` (the full gallery) when
    present, else the list-view `images({"limit":5})`, plus primaryImage. Returns
    a list of {url, primaryLabel, alt} dicts, primary first, deduped by id; []
    when the card has no images. primaryLabel/alt fall back to None when `state`
    is missing or the entity is absent."""
    state = state or {}
    refs = []
    primary = card.get("primaryImage")
    if isinstance(primary, dict):
        refs.append(primary.get("__ref"))
    # Detail pages expose the full `images`; SERP cards only images({"limit":5}).
    imgs = card.get("images") or card.get('images({"limit":5})') or []
    for img in imgs:
        if isinstance(img, dict):
            refs.append(img.get("__ref"))
    photos, seen = [], set()
    for ref in refs:
        m = _IMAGE_REF_RE.search(ref or "")
        if not m or m.group(1) in seen:
            continue
        iid = m.group(1)
        seen.add(iid)
        ent = state.get(f"Image:{iid}") or {}
        photos.append({
            "url": IMAGE_URL_TPL.format(id=iid),
            "primaryLabel": ent.get("primaryLabel"),
            "alt": ent.get("alt"),
        })
    return photos


def serp_card_to_row(card: dict, unmapped: Optional[set] = None) -> dict:
    """Map one kommun-wide search (SERP) SoldProperty into the CSV schema. This
    list view omits agency, apartment number, monthly fee (detail-page only)
    and carries no BRF id — but it does include asking price, price change %,
    kr/m² and coordinates."""
    address = card.get("streetAddress") or ""
    rel_url = card.get("url") or ""
    region = ((card.get("location") or {}).get("region") or {})
    rooms, area, sqm, _ = _serp_display_points(card)
    return {
        "sold_date": card.get("soldDate"),
        "address": address,
        "street_number": general_street_number(address),
        "rooms": rooms,
        "living_area_m2": area,
        "final_price_kr": (card.get("soldPrice") or {}).get("raw"),
        "asking_price_kr": parse_int_swedish((card.get("listPrice") or {}).get("formatted")),
        "kr_per_m2": sqm,
        "monthly_fee_kr": None,
        "price_change_pct": (card.get("soldPricePercentageDiff") or {}).get("raw"),
        "broker_agency": None,
        "listing_id": card.get("booliId"),
        "booli_url": f"https://www.booli.se{rel_url}" if rel_url else None,
        "apartment_number": None,
        "object_type": map_object_type(card.get("objectType"), unmapped),
        "object_form": normalize_object_form(card.get("objectType")),
        "tenure": normalize_tenure(card.get("tenureForm")),
        "lat": card.get("latitude"),
        "lng": card.get("longitude"),
        "kommun": region.get("municipalityName") if isinstance(region, dict) else None,
        "area_name": clean_area_name(
            card.get("descriptiveAreaName"),
            region.get("municipalityName") if isinstance(region, dict) else None),
        "booli_brf_id": None,
        "brf_name": None,
    }


def _next_data(page) -> dict:
    """Read the page's server-rendered __NEXT_DATA__ JSON blob."""
    return page.evaluate(
        "() => JSON.parse(document.getElementById('__NEXT_DATA__').textContent)")


# Booli moved to the Next.js App Router (seen 2026-10): pages no longer carry
# __NEXT_DATA__. Server-rendered GraphQL results now arrive two other ways:
#  - SERP pages: Apollo's streaming-SSR transport, inline scripts of the form
#    `(window[Symbol.for("ApolloSSRDataTransport")] ??= []).push({rehydrate: …})`,
#    a JS (not JSON) literal — it contains bare `undefined`.
#  - Detail pages: the React Server Components flight stream,
#    `self.__next_f.push([1, "<chunk>"])`; the concatenated chunks embed the full
#    `Listing` object as JSON.
_APOLLO_SSR_PUSH = '(window[Symbol.for("ApolloSSRDataTransport")] ??= []).push('
_JS_UNDEFINED_RE = re.compile(r'(?<=[:\[,])undefined(?=[,\]}])')
_FLIGHT_PUSH = "self.__next_f.push("


def _apollo_ssr_rehydrate(html: str) -> dict:
    """Merge the `rehydrate` maps of every Apollo SSR transport push in the page
    into one {hookId: {data, …}} dict. {} when the page has no transport."""
    dec = json.JSONDecoder()
    out: dict = {}
    i = 0
    while (i := html.find(_APOLLO_SSR_PUSH, i)) >= 0:
        i += len(_APOLLO_SSR_PUSH)
        end = html.find("</script>", i)
        body = _JS_UNDEFINED_RE.sub("null", html[i:end if end >= 0 else None])
        try:
            obj, _ = dec.raw_decode(body)
        except ValueError:
            continue
        out.update(obj.get("rehydrate") or {})
    return out


def _flight_text(html: str) -> str:
    """Concatenate the page's RSC flight-stream string chunks."""
    dec = json.JSONDecoder()
    chunks = []
    i = 0
    while (i := html.find(_FLIGHT_PUSH, i)) >= 0:
        i += len(_FLIGHT_PUSH)
        try:
            arr, n = dec.raw_decode(html[i:])
        except ValueError:
            continue
        i += n
        if isinstance(arr, list) and len(arr) > 1 and isinstance(arr[1], str):
            chunks.append(arr[1])
    return "".join(chunks)


def _flight_objects(flight: str, typename: str) -> list:
    """Every JSON object of the given __typename embedded in a flight stream."""
    dec = json.JSONDecoder()
    key = '{"__typename":"%s"' % typename
    out = []
    i = 0
    while (i := flight.find(key, i)) >= 0:
        try:
            obj, n = dec.raw_decode(flight[i:])
            out.append(obj)
            i += n
        except ValueError:
            i += 1
    return out


def _apollo_sold_cards(next_data: dict) -> list:
    state = (next_data.get("props", {}).get("pageProps", {})
             .get("__APOLLO_STATE__", {}))
    return [v for k, v in state.items() if k.startswith("SoldProperty:")]


def _total_sold_pages(next_data: dict) -> int:
    """Total result pages from the SearchSoldResult node, which lives nested
    under ROOT_QUERY.searchSold(...) in the Apollo state."""
    state = (next_data.get("props", {}).get("pageProps", {})
             .get("__APOLLO_STATE__", {}))
    best = 1

    def walk(node):
        nonlocal best
        if isinstance(node, dict):
            if node.get("__typename") == "SearchSoldResult" and "pages" in node:
                best = max(best, int(node["pages"]))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(state)
    return best


def _page_url(url: str, page_no: int) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}page={page_no}"


def fetch_area_sold(url: str, raw_path: Optional[Path] = None,
                    max_pages: Optional[int] = None) -> list:
    """Page through a kommun-wide sold search, parsing the server-rendered
    __NEXT_DATA__ on each page (results are SSR'd, not fetched via GraphQL).
    Returns unique SERP cards deduped by booliId."""
    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth

    cards: dict = {}

    def collect(nd):
        for c in _apollo_sold_cards(nd):
            cid = c.get("booliId") or c.get("id")
            if cid is not None:
                cards[str(cid)] = c

    with Stealth().use_sync(sync_playwright()) as pw:
        browser = pw.chromium.launch(headless=True, proxy=_proxy_opts())
        ctx = browser.new_context(locale="sv-SE", timezone_id="Europe/Stockholm",
                                  viewport={"width": 1366, "height": 850})
        page = ctx.new_page()

        print(f"GET {url} …")
        for attempt in range(3):  # page 1 drives total-page count; retry transients
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                break
            except Exception as e:
                if attempt == 2:
                    browser.close()
                    raise
                print(f"  page 1 retry {attempt + 1}/2 ({e})")
                page.wait_for_timeout(2000)
        nd = _next_data(page)
        total = _total_sold_pages(nd)
        if max_pages:
            total = min(total, max_pages)
        if raw_path is not None:
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(json.dumps(nd, ensure_ascii=False, indent=2),
                                encoding="utf-8")
            print(f"Page-1 __NEXT_DATA__ archived to {raw_path}")

        collect(nd)
        print(f"  page 1/{total}: {len(cards)} cards")
        failed = []
        for n in range(2, total + 1):
            for attempt in range(2):  # one retry on a transient page failure
                try:
                    page.goto(_page_url(url, n), wait_until="domcontentloaded",
                              timeout=45000)
                    collect(_next_data(page))
                    break
                except Exception as e:
                    if attempt:
                        failed.append(n)
                        print(f"  page {n}: skipped ({e})")
                    else:
                        page.wait_for_timeout(1500)
            if n % 10 == 0 or n == total:
                print(f"  page {n}/{total}: {len(cards)} cards")
        browser.close()

    if failed:
        print(f"WARNING: {len(failed)} page(s) failed and were skipped: {failed}")
    return list(cards.values())


DEFAULT_AREAS_CSV = PROCESSED / "booli_areas.csv"


def read_area_ids(parent_id, areas_csv: Path = DEFAULT_AREAS_CSV) -> list:
    """Read the område driver table (written by resolve_areas.py) and return the
    [(area_id, name)] whose parent_area_id is the given kommun areaId."""
    out = []
    with open(areas_csv, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if str(row.get("parent_area_id")) == str(parent_id):
                out.append((row["area_id"], row.get("name") or row["area_id"]))
    return out


def _card_kommun(card: dict) -> Optional[str]:
    if card.get("__typename") == "ListableProperty":
        return ((card.get("tracking") or {}).get("properties") or {}).get("municipality")
    region = ((card.get("location") or {}).get("region") or {})
    return region.get("municipalityName") if isinstance(region, dict) else None


def fetch_areas_sold(area_ids: list, kommun: Optional[str] = None,
                     max_pages: Optional[int] = None) -> list:
    """Scrape sold listings for each område areaId in turn and merge them, deduped
    by booliId across områden — Booli's område tier overlaps (a coarse område can
    return cards a finer one also returns), so a card seen under two områden is
    written once. Returns the merged unique cards.

    This is the completeness lever for scaling past one kommun: Booli's kommun-wide
    sold SERP is bounded by a result-page window, so a large kommun silently
    truncates; iterating its områden keeps each query under the cap.

    When `kommun` is given, cards whose municipality differs are dropped: a few
    resolved area ids turn out not to scope cleanly and Booli then returns national
    results, so this guards against pulling in other kommuns' sales."""
    merged: dict = {}
    spilled = 0
    failed: list = []  # områden whose whole fetch threw — retried once at the end
    pending = list(enumerate(area_ids, 1))
    while pending:
        i, (aid, name) = pending.pop(0)
        url = SOLD_SEARCH_URL_TPL.format(area_id=aid)
        print(f"[{i}/{len(area_ids)}] område {name} (areaId {aid})")
        before = len(merged)
        try:
            cards = fetch_area_sold(url, raw_path=None, max_pages=max_pages)
        except Exception as e:
            # A whole-område failure (e.g. page-1 navigation timeout) must not abort
            # the run and discard every prior område's cards — log, defer, continue.
            if (aid, name) in failed:
                print(f"  område {name} failed again, giving up ({e})")
                continue
            print(f"  område {name} failed, will retry at end ({e})")
            failed.append((aid, name))
            pending.append((i, (aid, name)))
            continue
        for c in cards:
            ck = _card_kommun(c)
            if kommun and ck and ck != kommun:
                spilled += 1
                continue
            cid = c.get("booliId") or c.get("id")
            if cid is not None:
                merged[str(cid)] = c
        print(f"  +{len(merged) - before} new ({len(merged)} unique total)")
    if spilled:
        print(f"Dropped {spilled} card(s) from other kommuns (område id didn't "
              f"scope to {kommun})")
    return list(merged.values())


def rows_from_area_cards(cards: list, kommun_default: str) -> list:
    unmapped: set = set()
    rows = [serp_card_to_row(c, unmapped) for c in cards]
    for r in rows:
        if not r.get("kommun"):
            r["kommun"] = kommun_default
    typed = sum(1 for r in rows if r["object_type"])
    located = sum(1 for r in rows if r["lat"] is not None)
    print(f"Mapped {len(rows)} cards: {typed} typed, {located} with coordinates")
    if unmapped:
        print(f"  UNMAPPED objectType values (extend OBJECT_TYPE_MAP): {sorted(unmapped)}")
    return rows


# ---------------------------------------------------------------------------
# Current for-sale listings (advertisements). Same SSR/__NEXT_DATA__ mechanism
# as the sold search, but the till-salu page yields Listing cards (asking price,
# kr/m², agency, published date, tenure form, amenities, Booli estimate).
# ---------------------------------------------------------------------------

def _amenity_keys(card: dict) -> list:
    keys = []
    for a in card.get("amenities") or []:
        if not isinstance(a, dict):
            continue
        if a.get("key"):  # App Router: inline Amenity object
            keys.append(a["key"])
            continue
        m = re.search(r'"key":"([^"]+)"', a.get("__ref", ""))
        if m:
            keys.append(m.group(1))
    return keys


def listing_card_to_row(card: dict, unmapped: Optional[set] = None) -> dict:
    """Map one for-sale Listing to the advertisements CSV schema. Accepts both
    the Pages Router SERP card and the App Router detail-page Listing (same
    field names; agency/avgift sit under slightly different keys)."""
    address = card.get("streetAddress") or ""
    rel_url = card.get("url") or ""
    region = ((card.get("location") or {}).get("region") or {})
    rooms, area, _, monthly_fee = _serp_display_points(card)
    if monthly_fee is None:
        monthly_fee = (card.get("rent") or {}).get("raw")
    agency = (card.get('agency({"queryContext":"SERP_LIST_LISTING"})')
              or card.get("agency") or {})
    diff = card.get("listPricePercentageDiff")
    estimate = (card.get("estimate") or {}).get("price")
    showing = card.get("nextShowing")
    attrs = {
        "tenure_form": card.get("tenureForm"),
        "is_new_construction": card.get("isNewConstruction"),
        "bidding_open": bool(card.get("biddingOpen")) if card.get("biddingOpen") is not None else None,
        "upcoming_sale": card.get("upcomingSale"),
        "days_active": card.get("daysActive"),
        "list_price_change_pct": diff.get("raw") if isinstance(diff, dict) else None,
        "estimate_kr": estimate.get("raw") if isinstance(estimate, dict) else estimate,
        "next_showing": showing.get("fullDateAndTime") if isinstance(showing, dict) else showing,
        "amenities": _amenity_keys(card) or None,
    }
    attrs = {k: v for k, v in attrs.items() if v not in (None, [], "")}
    return {
        "listing_id": card.get("booliId"),
        "listed_date": (card.get("published") or "")[:10] or None,
        "asking_price_kr": (card.get("listPrice") or {}).get("raw"),
        "property_type": map_object_type(card.get("objectType"), unmapped),
        "object_form": normalize_object_form(card.get("objectType")),
        "tenure": normalize_tenure(card.get("tenureForm")),
        "address": address,
        "street_number": general_street_number(address),
        "apartment_number": (card.get("apartmentNumber") or {}).get("value"),
        "rooms": rooms,
        "living_area_m2": area,
        "kr_per_m2": parse_int_swedish((card.get("listSqmPrice") or {}).get("formatted")),
        "monthly_fee_kr": monthly_fee,
        "broker_agency": agency.get("name") if isinstance(agency, dict) else None,
        "kommun": region.get("municipalityName") if isinstance(region, dict) else None,
        "area_name": clean_area_name(
            card.get("descriptiveAreaName"),
            region.get("municipalityName") if isinstance(region, dict) else None),
        "lat": card.get("latitude"),
        "lng": card.get("longitude"),
        "source_url": f"https://www.booli.se{rel_url}" if rel_url else None,
        "attributes_json": json.dumps(attrs, ensure_ascii=False) if attrs else "",
    }


_DISPLAY_DATE_DAYS_RE = re.compile(r"(\d+)\s+dag(?:ar)?\s+på Booli", re.I)


def _display_date_days(text) -> Optional[int]:
    """Days on Booli from a SERP displayDate: 'Inkommet idag' -> 0,
    'Inkommet igår' -> 1, '5 dagar på Booli' -> 5; None otherwise (e.g. a
    project's 'Under försäljning')."""
    t = (text or "").lower()
    if "idag" in t:
        return 0
    if "igår" in t:
        return 1
    m = _DISPLAY_DATE_DAYS_RE.search(t)
    return int(m.group(1)) if m else None


def listable_property_to_row(card: dict, unmapped: Optional[set] = None,
                             today: Optional[date] = None) -> dict:
    """Map one App Router SERP card (`ListableProperty`, 2026-10+) to the
    advertisements CSV schema. This card is leaner than the old Listing card:
    no published timestamp (listed_date is derived from the 'N dagar på Booli'
    display text), no tenure, no Booli estimate, no price-change %, and price /
    kr/m² only as display text. `enrich_details` fills those from the detail page."""
    today = today or date.today()
    tracking = ((card.get("tracking") or {}).get("properties") or {})
    kommun = tracking.get("municipality")
    address = card.get("title") or ""
    rel_url = card.get("url") or ""
    rooms, area, _, monthly_fee = _serp_display_points(card)
    display_price = card.get("displayPrice") or ""
    # A range ('2 895 000 – 5 995 000 kr') is a project, not one asking price.
    price = None if "–" in display_price else parse_int_swedish(display_price)
    days = _display_date_days(card.get("displayDate"))
    # subtitle: '<objectType> · <område> · <kommun>'
    parts = [p.strip() for p in (card.get("subtitle") or "").split("·")]
    area_raw = parts[1] if len(parts) >= 3 else None
    position = card.get("position") or {}
    presenter = card.get("presenter") or {}
    showing = card.get("secondaryStatus") or {}
    attrs = {
        "is_new_construction": (card.get("primaryStatus") or {}).get("key") == "newConstruction",
        "upcoming_sale": tracking.get("upcoming_sale"),
        "days_active": days,
        "next_showing": showing.get("label") if showing.get("key") == "showing" else None,
        "amenities": _amenity_keys(card) or None,
    }
    attrs = {k: v for k, v in attrs.items() if v not in (None, [], "")}
    return {
        "listing_id": card.get("listingId"),
        "listed_date": (today - timedelta(days=days)).isoformat() if days is not None else None,
        "asking_price_kr": price,
        "property_type": map_object_type(card.get("objectType"), unmapped),
        "object_form": normalize_object_form(card.get("objectType")),
        "tenure": None,
        "address": address,
        "street_number": general_street_number(address),
        "apartment_number": None,
        "rooms": rooms,
        "living_area_m2": area,
        # Booli shows kr/m² rounded to the nearest 100; match that.
        "kr_per_m2": int(round(price / area, -2)) if price and area else None,
        "monthly_fee_kr": monthly_fee,
        "broker_agency": presenter.get("name"),
        "kommun": kommun,
        "area_name": clean_area_name(area_raw, kommun),
        "lat": position.get("latitude"),
        "lng": position.get("longitude"),
        "source_url": f"https://www.booli.se{rel_url}" if rel_url else None,
        "attributes_json": json.dumps(attrs, ensure_ascii=False) if attrs else "",
    }


def _forsale_results(rehydrate: dict) -> list:
    """The SearchForSaleV2Result blocks of an App Router SERP: `forSale` (the
    paged list) and `newConstruction` (a fixed widget mixing nyproduktion
    listings with whole projects)."""
    out = []
    for entry in rehydrate.values():
        data = (entry or {}).get("data") or {}
        for key in ("forSale", "newConstruction"):
            node = data.get(key)
            if isinstance(node, dict) and node.get("__typename") == "SearchForSaleV2Result":
                out.append((key, node))
    return out


def _forsale_cards(rehydrate: dict) -> list:
    """Individual listings on a SERP page. Projects (originId 'project:…') have
    no listingId and are skipped — their units are listed separately."""
    return [it for _, node in _forsale_results(rehydrate)
            for it in node.get("items") or []
            if isinstance(it, dict) and it.get("listingId")]


def _total_forsale_pages(rehydrate: dict) -> int:
    """Pages in the `forSale` list = ceil(totalCount / page size)."""
    for key, node in _forsale_results(rehydrate):
        if key == "forSale":
            per_page = len(node.get("items") or [])
            total = int(node.get("totalCount") or 0)
            return max(1, -(-total // per_page)) if per_page else 1
    return 1


def fetch_area_forsale(url: str, raw_path: Optional[Path] = None,
                       max_pages: Optional[int] = None,
                       mis_scoped_pages: Optional[int] = None) -> list:
    """Page through a kommun-wide for-sale search, parsing the Apollo SSR
    transport on each page. Returns unique ListableProperty cards deduped by
    listingId."""
    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth

    cards: dict = {}

    def collect(rehydrate):
        for c in _forsale_cards(rehydrate):
            cards[str(c["listingId"])] = c

    # BOOLI_HEADED=1 runs a visible browser so a Turnstile checkbox can be solved
    # (auto-click or by hand); cf_clearance from that solve rides the whole run.
    headless = os.environ.get("BOOLI_HEADED") != "1"
    with Stealth().use_sync(sync_playwright()) as pw:
        browser = pw.chromium.launch(headless=headless, proxy=_proxy_opts())
        ctx = browser.new_context(locale="sv-SE", timezone_id="Europe/Stockholm",
                                  viewport={"width": 1366, "height": 850})
        page = ctx.new_page()
        print(f"GET {url} …")
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        if _on_cf_challenge(page):
            wait_s = int(os.environ.get("BOOLI_CF_WAIT_S", "180"))
            if not _clear_cf(page, wait_s=wait_s):
                browser.close()
                raise RuntimeError("Cloudflare challenge not cleared — "
                                   "rerun with BOOLI_HEADED=1 or a fresh IP.")
        rehydrate = _apollo_ssr_rehydrate(page.content())
        if not _forsale_results(rehydrate):
            browser.close()
            raise RuntimeError("No for-sale results in the page's Apollo SSR "
                               "transport — Booli's page structure changed again?")
        total = _total_forsale_pages(rehydrate)
        if mis_scoped_pages and total > mis_scoped_pages:
            # homesight: an område id Booli does not scope returns national results (2 520 pages for
            # Centrala Trollbäcken, 2026-10-12); page 1 only, never page through it
            print(f"  {total} pages: not scoped to the område (national results?); page 1 only")
            total = 1
        if max_pages:
            total = min(total, max_pages)
        if raw_path is not None:
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(json.dumps(rehydrate, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        collect(rehydrate)
        print(f"  page 1/{total}: {len(cards)} listings")
        for n in range(2, total + 1):
            try:
                page.goto(_page_url(url, n), wait_until="domcontentloaded", timeout=45000)
                if _on_cf_challenge(page):
                    _clear_cf(page, wait_s=180)
                collect(_apollo_ssr_rehydrate(page.content()))
            except Exception as e:
                print(f"  page {n}: skipped ({e})")
            if n % 5 == 0 or n == total:
                print(f"  page {n}/{total}: {len(cards)} listings")
        browser.close()

    return list(cards.values())


# homesight: an område whose search reports more pages than this is not scoped by Booli (the largest real
# one, Vasastan, has ~620 listings = 18 pages)
AREA_MIS_SCOPED_PAGES = 60


def fetch_areas_forsale(area_ids: list, kommun: Optional[str] = None,
                        max_pages: Optional[int] = None) -> list:
    """For-sale counterpart of `fetch_areas_sold`: scrape current advertisements
    område-by-område and merge, deduped by booliId across områden. A kommun-wide
    for-sale SERP is bounded by the same result-page window, so iterating områden
    keeps each query under the cap for a large kommun. When `kommun` is given,
    cards whose municipality differs are dropped (an område id that doesn't scope
    cleanly makes Booli return national results)."""
    merged: dict = {}
    spilled = 0
    for i, (aid, name) in enumerate(area_ids, 1):
        url = FORSALE_URL_TPL.format(area_id=aid)
        print(f"[{i}/{len(area_ids)}] område {name} (areaId {aid})")
        before = len(merged)
        for c in fetch_area_forsale(url, raw_path=None, max_pages=max_pages,
                                    mis_scoped_pages=AREA_MIS_SCOPED_PAGES):
            ck = _card_kommun(c)
            if kommun and ck and ck != kommun:
                spilled += 1
                continue
            cid = c.get("listingId") or c.get("booliId") or c.get("id")
            if cid is not None:
                merged[str(cid)] = c
        print(f"  +{len(merged) - before} new ({len(merged)} unique total)")
    if spilled:
        print(f"Dropped {spilled} ad(s) from other kommuns (område id didn't "
              f"scope to {kommun})")
    return list(merged.values())


def rows_from_listing_cards(cards: list, kommun_default: str) -> list:
    unmapped: set = set()
    rows = [listable_property_to_row(c, unmapped)
            if c.get("__typename") == "ListableProperty"
            else listing_card_to_row(c, unmapped) for c in cards]
    for r in rows:
        if not r.get("kommun"):
            r["kommun"] = kommun_default
    located = sum(1 for r in rows if r["lat"] is not None)
    print(f"Mapped {len(rows)} listings: {located} with coordinates")
    if unmapped:
        print(f"  UNMAPPED objectType values: {sorted(unmapped)}")
    return rows


def write_ads_csv(rows: list, out_csv: Path) -> None:
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    rows.sort(key=lambda r: r["listed_date"] or "", reverse=True)
    with open(out_csv, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ADVERT_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"CSV written to {out_csv} ({len(rows)} listings)")


# ---------------------------------------------------------------------------
# BRF enrichment. The kommun-wide list view has no förening, but each listing's
# detail page does. A BRF maps to a building, so we fetch one detail page per
# unique bostadsrätt address (not per sale), read its HousingCoop id, resolve
# names from the BRF pages, and write booli_brf_id/brf_name back onto the rows.
# Resumable: every lookup is cached to disk, so re-runs only fetch what's new.
# ---------------------------------------------------------------------------

def _load_json(path: Path):
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _save_json(path: Path, obj) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def _housing_coop_ids(next_data: dict) -> list:
    state = (next_data.get("props", {}).get("pageProps", {})
             .get("__APOLLO_STATE__", {}))
    return [v.get("id") for k, v in state.items()
            if k.startswith("HousingCoop:") and isinstance(v, dict) and v.get("id")]


def _coop_name(next_data: dict, coop_id) -> Optional[str]:
    state = (next_data.get("props", {}).get("pageProps", {})
             .get("__APOLLO_STATE__", {}))
    entry = state.get(f"HousingCoop:{coop_id}")
    if isinstance(entry, dict) and entry.get("name"):
        return entry["name"]
    for k, v in state.items():  # fallback: any named HousingCoop entry
        if k.startswith("HousingCoop:") and isinstance(v, dict) and v.get("name"):
            return v["name"]
    return None


def _representative_urls(rows: list) -> dict:
    """One listing URL per unique bostadsrätt address, preferring /bostad/ pages
    (richer __NEXT_DATA__) over /annons/."""
    addr_url: dict = {}
    for r in rows:
        if r.get("object_type") != "bostadsrätt":
            continue
        addr, url = r["address"], r.get("booli_url") or ""
        if addr not in addr_url or ("/bostad/" in url and "/bostad/" not in addr_url[addr]):
            addr_url[addr] = url
    return addr_url


def enrich_brf(input_csv: Path, output_csv: Path, cache_path: Path,
               max_buildings: Optional[int] = None) -> None:
    with open(input_csv, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    addr_url = _representative_urls(rows)

    cache = _load_json(cache_path) or {"addr_to_coop": {}, "coop_names": {}}
    a2c, names = cache["addr_to_coop"], cache["coop_names"]
    todo = [a for a in addr_url if a not in a2c]
    if max_buildings:
        todo = todo[:max_buildings]
    print(f"{len(addr_url)} bostadsrätt buildings; {len(a2c)} cached; "
          f"fetching {len(todo)} detail pages…")

    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth

    def goto_next_data(page, url):
        page.goto(url, wait_until="domcontentloaded", timeout=40000)
        return _next_data(page)

    with Stealth().use_sync(sync_playwright()) as pw:
        browser = pw.chromium.launch(headless=True, proxy=_proxy_opts())
        ctx = browser.new_context(locale="sv-SE", timezone_id="Europe/Stockholm",
                                  viewport={"width": 1366, "height": 850})
        page = ctx.new_page()

        for i, addr in enumerate(todo, 1):
            url = addr_url[addr]
            full = url if url.startswith("http") else f"https://www.booli.se{url}"
            coop_id = None
            for attempt in range(2):
                try:
                    nd = goto_next_data(page, full)
                    ids = _housing_coop_ids(nd)
                    coop_id = ids[0] if ids else None
                    if coop_id and coop_id not in names:
                        nm = _coop_name(nd, coop_id)
                        if nm:
                            names[coop_id] = nm
                    break
                except Exception:
                    page.wait_for_timeout(1500)
            a2c[addr] = coop_id
            if i % 25 == 0:
                _save_json(cache_path, cache)
                print(f"  buildings {i}/{len(todo)} ({len(names)} names so far)")
        _save_json(cache_path, cache)

        missing = sorted({c for c in a2c.values() if c and c not in names})
        print(f"resolving {len(missing)} BRF names…")
        for i, coop_id in enumerate(missing, 1):
            try:
                nd = goto_next_data(page, f"https://www.booli.se/bostadsrattsforening/{coop_id}")
                nm = _coop_name(nd, coop_id)
                if nm:
                    names[coop_id] = nm
            except Exception:
                pass
            if i % 25 == 0:
                _save_json(cache_path, cache)
                print(f"  names {i}/{len(missing)}")
        browser.close()
    _save_json(cache_path, cache)

    linked, brfset, named_misses = 0, set(), 0
    for r in rows:
        if r.get("object_type") == "bostadsrätt":
            coop_id = a2c.get(r["address"])
            if coop_id:
                r["booli_brf_id"] = coop_id
                r["brf_name"] = names.get(coop_id) or ""
                if not names.get(coop_id):
                    named_misses += 1
                linked += 1
                brfset.add(coop_id)
    write_csv(rows, Path(output_csv))
    print(f"Linked {linked} bostadsrätt rows to {len(brfset)} distinct BRFs "
          f"({len(brfset) - len({c for c in brfset if names.get(c)})} without a resolved name)")


# ---------------------------------------------------------------------------
# Listing detail enrichment. The kommun list view is thin: it exposes only
# images({"limit":5}) and no construction year. The full photo gallery (the
# `images` field) and the building's constructionYear both live on each
# listing's detail page. We fetch one detail page per advertisement and write
# photo_urls + construction_year back onto the row. Resumable: every listing's
# fields are cached to disk, so re-runs only fetch new listings.
# ---------------------------------------------------------------------------

def _detail_fields(html: str, listing_id: str) -> Optional[dict]:
    """Photos, construction year and the full detail-derived ad row for the
    listing on an App Router detail page, read from the RSC flight stream's
    `Listing` object. None when the page carries no matching Listing (not
    served / structure changed) — the caller must not cache that."""
    listings = [o for o in _flight_objects(_flight_text(html), "Listing")
                if str(o.get("booliId") or o.get("id")) == str(listing_id)]
    if not listings:
        return None
    best = max(listings, key=lambda o: len(o.get("images") or []))
    # Images are inline objects here (not Apollo refs), so read them directly.
    photos, seen = [], set()
    for img in best.get("images") or []:
        iid = str((img or {}).get("id") or "")
        if iid and iid not in seen:
            seen.add(iid)
            photos.append({"url": IMAGE_URL_TPL.format(id=iid),
                           "primaryLabel": img.get("primaryLabel"),
                           "alt": img.get("alt")})
    row = listing_card_to_row(best)
    return {
        "photos": photos,
        "construction_year": best.get("constructionYear"),
        "listing": {k: v for k, v in row.items() if v not in (None, "")},
    }


# Ad columns the detail page knows better than the App Router SERP card (which
# lacks published/tenure/estimate and shows price & kr/m² only as text).
_DETAIL_OVERRIDE_COLUMNS = (
    "listed_date", "asking_price_kr", "tenure", "apartment_number", "kr_per_m2",
    "monthly_fee_kr", "broker_agency", "area_name",
)


def _photos_lack_metadata(entry: dict) -> bool:
    """True for a legacy cache entry whose photos are bare URL strings (scraped
    before photos carried primaryLabel/alt) — re-fetching upgrades them in place."""
    return any(isinstance(p, str) for p in (entry.get("photos") or []))


def enrich_details(input_csv: Path, output_csv: Path, cache_path: Path,
                   max_listings: Optional[int] = None,
                   refetch_photos: bool = False) -> None:
    """Fill photo_urls + construction_year on every advertisement by fetching its
    detail page (the only place the full gallery and construction year live).
    Cached by listing_id, so re-runs only fetch new listings. With refetch_photos,
    also re-fetch already-cached listings whose photos predate primaryLabel/alt, to
    backfill the per-image category metadata without discarding the rest of the cache."""
    with open(input_csv, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    # listing_id -> {photos: [...], construction_year, listing: {ad columns}}.
    # Entries cached before the App Router switch have no `listing`; a row
    # without tenure came from the leaner App Router SERP and needs one.
    cache = _load_json(cache_path) or {}
    todo = [r for r in rows
            if r.get("listing_id") and r.get("source_url")
            and (r["listing_id"] not in cache
                 or (refetch_photos and _photos_lack_metadata(cache[r["listing_id"]]))
                 or ("listing" not in cache[r["listing_id"]] and not r.get("tenure")))]
    if max_listings:
        todo = todo[:max_listings]
    stale = sum(1 for r in todo if r["listing_id"] in cache)
    print(f"{len(rows)} listings; {len(cache)} cached; fetching {len(todo)} detail pages"
          f"{f' ({stale} re-fetched to backfill photo labels)' if stale else ''}…")

    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth

    with Stealth().use_sync(sync_playwright()) as pw:
        browser = pw.chromium.launch(headless=True, proxy=_proxy_opts())
        ctx = browser.new_context(locale="sv-SE", timezone_id="Europe/Stockholm",
                                  viewport={"width": 1366, "height": 850})
        page = ctx.new_page()
        challenged = 0
        for i, r in enumerate(todo, 1):
            lid, url = r["listing_id"], r["source_url"]
            full = url if url.startswith("http") else f"https://www.booli.se{url}"
            fields = None
            blocked = False
            for attempt in range(2):
                try:
                    page.goto(full, wait_until="domcontentloaded", timeout=40000)
                    blocked = _on_cf_challenge(page)
                    if not blocked:
                        fields = _detail_fields(page.content(), lid)
                        break
                except Exception:
                    pass
                page.wait_for_timeout(1500)
            if blocked:
                challenged += 1  # transient — leave uncached so a re-run retries
            else:
                # A real page with no Listing = ad removed since the SERP scrape;
                # cache that so it isn't re-fetched every run.
                cache[lid] = fields or {"photos": [], "construction_year": None,
                                        "listing": None}
            page.wait_for_timeout(random.randint(*ENRICH_PACE_MS))
            if i % 25 == 0:
                _save_json(cache_path, cache)
                print(f"  listings {i}/{len(todo)}")
        browser.close()
    _save_json(cache_path, cache)
    if challenged:
        print(f"  ⚠ {challenged} detail page(s) hit a Cloudflare challenge — "
              f"left uncached; re-run to retry them.")

    withphotos = total = withyear = 0
    for r in rows:
        f = cache.get(r.get("listing_id")) or {}
        detail = f.get("listing") or {}
        for col in _DETAIL_OVERRIDE_COLUMNS:
            if col in detail:
                r[col] = detail[col]
        if detail.get("attributes_json"):
            attrs = json.loads(r.get("attributes_json") or "{}")
            attrs.update(json.loads(detail["attributes_json"]))
            r["attributes_json"] = json.dumps(attrs, ensure_ascii=False)
        # Normalize: cache entries written before photos carried metadata hold a
        # bare URL string per photo; wrap those as {url, primaryLabel, alt} so the
        # exported column is uniformly a list of objects (labels stay None until
        # the listing is re-fetched).
        photos = [{"url": p, "primaryLabel": None, "alt": None} if isinstance(p, str) else p
                  for p in (f.get("photos") or [])]
        year = f.get("construction_year")
        r["photo_urls"] = json.dumps(photos, ensure_ascii=False) if photos else ""
        r["construction_year"] = year if year else ""
        total += len(photos)
        if photos:
            withphotos += 1
        if year:
            withyear += 1
    write_ads_csv(rows, Path(output_csv))
    print(f"Detail: {withphotos}/{len(rows)} with photos ({total} images), "
          f"{withyear}/{len(rows)} with construction year")


# ---------------------------------------------------------------------------
# Sold listing detail enrichment. The sold-listing search pages do not carry
# monthly fee (avgift). It is available on each listing's own detail page
# (/annons/{id} or /bostad/{id}), which preserves the original listing data.
# We scan every displayAttributes entry in the page's Apollo state regardless
# of queryContext, so the extraction is robust to Booli changing the key.
# Only bostadsrätt listings are fetched (villas and other types have no avgift).
# Resumable: results are cached by listing_id.
# ---------------------------------------------------------------------------

def _find_fee_in_state(state: dict) -> Optional[int]:
    """Scan all Apollo entities for a displayAttributes block containing a
    'kr/mån' data point and return the parsed integer fee, or None."""
    for v in state.values():
        if not isinstance(v, dict):
            continue
        for field_key, field_val in v.items():
            if not field_key.startswith("displayAttributes(") or not isinstance(field_val, dict):
                continue
            for p in field_val.get("dataPoints", []):
                text = (p.get("value") or {}).get("plainText") or ""
                if "kr/mån" in text.lower():
                    return parse_int_swedish(text)
    return None


def _sold_detail_fields(next_data: dict) -> dict:
    state = (next_data.get("props", {}).get("pageProps", {})
             .get("__APOLLO_STATE__", {}))
    return {"monthly_fee_kr": _find_fee_in_state(state)}


def _on_cf_challenge(page) -> bool:
    """True when the page is a Cloudflare 'verify you are human' interstitial
    rather than the real listing. A real Booli page is a Next.js app: it carries
    __NEXT_DATA__ (Pages Router, pre-2026-10) or the RSC flight stream
    `self.__next_f` (App Router); the challenge page has neither."""
    try:
        return not page.evaluate(
            "() => !!(document.getElementById('__NEXT_DATA__') || self.__next_f)")
    except Exception:
        return True


def _clear_cf(page, wait_s: int = 150) -> bool:
    """Clear a Cloudflare Turnstile (managed-challenge) interstitial. Headless
    browsers fail it instantly, so the enrichers run HEADED (BOOLI_HEADED=1) and
    this auto-clicks the checkbox; if the auto-click doesn't pass, it waits for a
    human to tick the box in the visible window. Returns True once the real page
    (with __NEXT_DATA__) is reachable. cf_clearance then rides the rest of the run."""
    if not _on_cf_challenge(page):
        return True
    # Headless can't solve a checkbox (no one to click) — only auto-click is worth
    # a try, so cap the wait short instead of blocking the whole run for wait_s.
    if os.environ.get("BOOLI_HEADED") != "1":
        wait_s = min(wait_s, 20)
    # Hard block ("Sorry, you have been blocked") has no checkbox — bail fast
    # rather than wait out the timeout. It means this IP is flagged for automation
    # and needs a fresh IP, not a solve.
    try:
        body = (page.inner_text("body") or "").lower()
        if "you have been blocked" in body or "unable to access" in body:
            print("  ✗ Cloudflare HARD BLOCK (no checkbox) — this IP is flagged "
                  "for automation; needs a fresh IP.", flush=True)
            return False
    except Exception:
        pass
    print(f"  ⚠ Cloudflare checkbox — solve it in the browser window "
          f"(auto-clicking; waiting up to {wait_s}s)…", flush=True)
    end = time.time() + wait_s
    while time.time() < end:
        for sel in ('iframe[src*="challenges.cloudflare.com"]',
                    'iframe[title*="Cloudflare"]', 'iframe[title*="human"]'):
            try:
                page.frame_locator(sel).locator(
                    "input[type=checkbox], label, button").first.click(timeout=2500)
                break
            except Exception:
                continue
        page.wait_for_timeout(2500)
        if not _on_cf_challenge(page):
            print("  ✓ Cloudflare cleared.", flush=True)
            return True
    print("  ✗ Cloudflare not cleared within timeout.", flush=True)
    return False


# Fetch outcomes. ok/no_fee are DEFINITIVE (never re-fetched); challenged/error
# are transient and re-fetched on a later run. Conflating no_fee with challenged
# (both used to be just monthly_fee_kr=None) is what made us chase genuinely
# feeless objects as if they were throttled — so every fetch now records which.
_RETRYABLE_STATUS = {"challenged", "error"}


def _classify_sold_detail(page) -> tuple:
    """Classify a loaded listing page into (monthly_fee_kr, status):
      ok         — a 'kr/mån' data point was found → the fee
      no_fee     — page rendered real listing attributes (m²/rooms/…) but no
                   avgift is shown → genuinely missing for this object; permanent
      challenged — Cloudflare interstitial or a stripped page with NO listing
                   attributes → no real data was served; retry on a clean IP
    """
    if _on_cf_challenge(page):
        return None, "challenged"
    nd = _next_data(page)
    state = (nd.get("props", {}).get("pageProps", {}).get("__APOLLO_STATE__", {})) or {}
    fee = _find_fee_in_state(state)
    if fee is not None:
        return fee, "ok"
    # No fee — did the page actually render a real listing? Two valid shapes:
    #  - a `SoldProperty` apollo object (current sold-page structure; often has no
    #    displayAttributes block at all), or
    #  - a `displayAttributes(...)` block with dataPoints (older structure).
    # Either means real content was served, so the absence of a fee is genuine
    # (no_fee, permanent) — NOT a stripped/challenged response. A truly
    # stripped/challenged page has neither. (Before this, SoldProperty no-fee pages
    # were mislabeled `challenged` and retried forever — see logs/tools/
    # investigate_challenged.sh.)
    rendered = any(k.startswith("SoldProperty") for k in state) or any(
        fk.startswith("displayAttributes(") and isinstance(fv, dict) and fv.get("dataPoints")
        for v in state.values() if isinstance(v, dict)
        for fk, fv in v.items()
    )
    return None, ("no_fee" if rendered else "challenged")


def _sold_needs_fetch(lid: str, cache: dict) -> bool:
    """Re-fetch only what's worth re-fetching: uncached, transient failures, or
    legacy entries (monthly_fee_kr=None with no status — unknown why). A recorded
    fee or a definitive 'no_fee' is left alone."""
    e = cache.get(lid)
    if not e:
        return True
    if e.get("monthly_fee_kr") is not None:
        return False
    return e.get("status") != "no_fee"


def enrich_sold_details(input_csv: Path, output_csv: Path, cache_path: Path,
                        max_listings: Optional[int] = None,
                        years: Optional[int] = 5) -> None:
    """Fill monthly_fee_kr on bostadsrätt sold listings by fetching each listing's
    detail page (the only place the original avgift is preserved). Each fetch is
    cached by listing_id WITH a status (ok|no_fee|challenged|error) so re-runs skip
    definitive results and only retry transient (challenged/error) ones.

    By default only the most recent `years` (=5) of sales are fetched: recent avgift
    is the analytically valuable slice, the deep tail is slow and skews to no_fee, and
    a full-history backfill is a multi-thousand-fetch / many-hour grind. Pass years=0
    (--sold-years 0) for the full history; the cache makes a later widen incremental."""
    with open(input_csv, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    cache = _load_json(cache_path) or {}
    cutoff = None
    if years:
        from datetime import date, timedelta
        cutoff = (date.today() - timedelta(days=365 * years)).isoformat()
    todo = [r for r in rows
            if r.get("listing_id") and r.get("booli_url")
            and r.get("object_type") == "bostadsrätt"
            and (cutoff is None or (r.get("sold_date") or "") >= cutoff)
            and _sold_needs_fetch(r["listing_id"], cache)]
    if max_listings:
        todo = todo[:max_listings]
    eligible = sum(1 for r in rows if r.get("object_type") == "bostadsrätt")
    scope = f"sales since {cutoff} (last {years}y)" if cutoff else "full history"
    print(f"{len(rows)} sold listings ({eligible} bostadsrätt); "
          f"{len(cache)} cached; scope: {scope}; fetching {len(todo)} detail pages…")

    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth

    # Headless trips Cloudflare's managed challenge instantly. Set BOOLI_HEADED=1
    # to run a visible browser whose Turnstile checkbox can be solved (auto-click
    # or by hand) — needed when the IP is on a challenge (vs a clean IP that just
    # passes). cf_clearance from the warmup solve then rides the rest of the run.
    headless = os.environ.get("BOOLI_HEADED") != "1"
    with Stealth().use_sync(sync_playwright()) as pw:
        browser = pw.chromium.launch(headless=headless, proxy=_proxy_opts())
        ctx = browser.new_context(locale="sv-SE", timezone_id="Europe/Stockholm",
                                  viewport={"width": 1366, "height": 850})
        page = ctx.new_page()
        # Warm up: load one listing and clear any Cloudflare challenge ONCE before
        # the loop, so the cleared session covers the subsequent fetches.
        if todo:
            warm = todo[0]["booli_url"]
            warm = warm if warm.startswith("http") else f"https://www.booli.se{warm}"
            try:
                page.goto(warm, wait_until="domcontentloaded", timeout=40000)
                _clear_cf(page, wait_s=180)
            except Exception:
                pass
        consecutive_blocked = 0
        for i, r in enumerate(todo, 1):
            lid, url = r["listing_id"], r["booli_url"]
            fee, status = None, "error"
            t0 = time.time()
            for attempt in range(2):
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=40000)
                    if _on_cf_challenge(page):      # challenge → try to clear, then classify
                        _clear_cf(page, wait_s=180)
                    fee, status = _classify_sold_detail(page)
                    if status != "challenged":      # ok or no_fee = definitive answer
                        break
                except Exception:
                    status = "error"
                    page.wait_for_timeout(1500)
            cache[lid] = {"monthly_fee_kr": fee, "status": status}
            _append_sold_event({
                "ts_epoch": time.time(), "index": i, "listing_id": lid,
                "status": status, "fee": fee, "attempts": attempt + 1,
                "latency_ms": int((time.time() - t0) * 1000),
                "kommun": r.get("kommun"), "sold_date": r.get("sold_date")})
            # Abort on a streak of blocked fetches: the IP has tripped a block and
            # every further hit is futile (keeps the flag warm). Resume on a fresh IP.
            if status in _RETRYABLE_STATUS:
                consecutive_blocked += 1
                if consecutive_blocked >= SOLD_ENRICH_ABORT_STREAK:
                    _save_json(cache_path, cache)
                    print(f"  ✗ ABORT: {consecutive_blocked} consecutive challenged/error "
                          f"fetches at {i}/{len(todo)} — IP looks blocked; resume on a fresh IP "
                          f"(cached results are kept, challenged rows retried next run).")
                    break
            else:
                consecutive_blocked = 0
            # Pace requests to stay under Booli's rate-throttle. Bursting the
            # per-listing detail fetches (sub-second each) trips a footprint-wide
            # block after ~1-1.5k pages; a jittered ~2s gap keeps a long backfill
            # under the limit so it runs to completion. Tune via ENRICH_PACE_MS.
            page.wait_for_timeout(random.randint(*ENRICH_PACE_MS))
            if i % 25 == 0:
                _save_json(cache_path, cache)
                print(f"  listings {i}/{len(todo)}")
        browser.close()
    _save_json(cache_path, cache)

    with_fee = 0
    comp = {}   # status composition over eligible bostadsrätt (legacy None = 'unknown')
    for r in rows:
        e = cache.get(r.get("listing_id")) or {}
        fee = e.get("monthly_fee_kr")
        r["monthly_fee_kr"] = fee if fee is not None else ""
        if fee:
            with_fee += 1
        if r.get("object_type") == "bostadsrätt" and r.get("listing_id") in cache:
            st = e.get("status") or ("ok" if fee is not None else "unknown")
            comp[st] = comp.get(st, 0) + 1
    write_csv(rows, Path(output_csv))
    print(f"Detail: {with_fee}/{len(rows)} rows now have monthly_fee_kr")
    print(f"  status (eligible bostadsrätt): {comp}  "
          f"[no_fee=genuinely missing; challenged/error=retryable]")


# ---------------------------------------------------------------------------
# Per-BRF detail. Each förening page exposes org number, structural facts
# (units, area, build year, ownership, äkta) and economy KPIs (total loan, …)
# in its __NEXT_DATA__. We fetch one page per distinct BRF and write a row each.
# ---------------------------------------------------------------------------

def _dp_map(section) -> dict:
    """dataPoints -> {key: plainText} for a HousingCoop summary/economy section."""
    if not isinstance(section, dict):
        return {}
    return {p.get("key"): (p.get("value") or {}).get("plainText", "")
            for p in section.get("dataPoints", []) if p.get("key")}


def _yes_no(value):
    if not value:
        return None
    return str(value).strip().lower() in ("ja", "yes", "true")


def _brf_residences(state: dict) -> list:
    """Every apartment (Residence entry) in the BRF, flattened to CSV rows."""
    out = []
    for k, v in state.items():
        if not k.startswith("Residence:") or not isinstance(v, dict):
            continue
        la = v.get("livingArea") or {}
        fl = v.get("floor") or {}
        apt = v.get("apartmentNumber") or {}
        rooms = v.get("rooms") or {}
        addr = ((v.get("location") or {}).get("address") or {}).get("streetAddress", "") or ""
        url = v.get("url") or ""
        out.append({
            "residence_booli_id": str(v.get("booliId") or v.get("id") or ""),
            "address": addr,
            "street_number": general_street_number(addr),
            "apartment_number": apt.get("formatted") if isinstance(apt, dict) else None,
            "rooms": rooms.get("raw") if isinstance(rooms, dict) else None,
            "living_area_m2": (la.get("raw") if la.get("raw") is not None
                               else parse_float_swedish(la.get("formatted"))),
            "floor": parse_int_swedish(fl.get("formatted")) if isinstance(fl, dict) else None,
            "cadastral": v.get("cadastral") or "",
            "lat": v.get("latitude"),
            "lng": v.get("longitude"),
            "url": f"https://www.booli.se{url}" if url else "",
        })
    return out


def extract_brf_detail(next_data: dict, coop_id) -> Optional[dict]:
    state = (next_data.get("props", {}).get("pageProps", {})
             .get("__APOLLO_STATE__", {}))
    coop = state.get(f"HousingCoop:{coop_id}")
    if not isinstance(coop, dict):
        coop = next((v for k, v in state.items()
                     if k.startswith("HousingCoop:") and isinstance(v, dict)), None)
    if not coop:
        return None

    summ = _dp_map(coop.get("summary"))
    eco = _dp_map(coop.get("economy"))
    economy = {k: v for k, v in eco.items() if v}  # keep only populated KPIs
    disclaimer = ((coop.get("summary") or {}).get("disclaimer") or {}).get("markdown", "") or ""
    year = re.search(r"årsredovisning(?:en)?\s+för\s+(\d{4})", disclaimer)
    residences = _brf_residences(state)
    addresses = sorted({r["address"] for r in residences if r.get("address")})

    return {
        "booli_brf_id": str(coop.get("id") or coop_id),
        "name": coop.get("name") or "",
        "org_number": coop.get("orgNumber") or "",
        "construction_year": parse_int_swedish(summ.get("constructionYear")),
        "num_units": parse_int_swedish(summ.get("numberOfHousingCoopUnits")),
        "num_rental_units": parse_int_swedish(summ.get("numberOfRentalUnits")),
        "total_living_area_m2": parse_int_swedish(summ.get("totalLivingArea")),
        "plot_ownership": summ.get("plotOwnership") or "",
        "is_genuine": _yes_no(summ.get("housingCoopType")),
        "total_loan_kr": parse_int_swedish(eco.get("totalLoan")),
        "annual_report_year": int(year.group(1)) if year else None,
        "has_valid_annual_report": coop.get("hasValidAnnualReport"),
        "economy_json": json.dumps(economy, ensure_ascii=False) if economy else "",
        "description": ((coop.get("description") or {}).get("plainText") or "").strip(),
        "num_addresses": len(addresses),
        "addresses_json": json.dumps(addresses, ensure_ascii=False) if addresses else "",
        "_residences": residences,  # not a details column; written separately
    }


def _distinct_brf_ids(input_csv: Path) -> list:
    with open(input_csv, newline="", encoding="utf-8-sig") as f:
        ids = {r["booli_brf_id"] for r in csv.DictReader(f) if r.get("booli_brf_id")}
    return sorted(ids)


def load_brf_details(input_csv: Path, output_csv: Path, cache_path: Path,
                     residences_csv: Path) -> None:
    ids = _distinct_brf_ids(input_csv)
    cache = _load_json(cache_path) or {}
    # Re-fetch entries cached before residences/addresses were captured.
    todo = [i for i in ids if "_residences" not in (cache.get(i) or {})]
    print(f"{len(ids)} distinct BRFs; {len(ids) - len(todo)} cached; fetching {len(todo)}…")

    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth

    with Stealth().use_sync(sync_playwright()) as pw:
        browser = pw.chromium.launch(headless=True, proxy=_proxy_opts())
        ctx = browser.new_context(locale="sv-SE", timezone_id="Europe/Stockholm",
                                  viewport={"width": 1366, "height": 900})
        page = ctx.new_page()
        for i, coop_id in enumerate(todo, 1):
            detail = None
            for attempt in range(2):
                try:
                    page.goto(f"https://www.booli.se/bostadsrattsforening/{coop_id}",
                              wait_until="domcontentloaded", timeout=40000)
                    detail = extract_brf_detail(_next_data(page), coop_id)
                    break
                except Exception:
                    page.wait_for_timeout(1500)
            cache[coop_id] = detail or {"booli_brf_id": coop_id}
            if i % 20 == 0:
                _save_json(cache_path, cache)
                print(f"  {i}/{len(todo)}")
        browser.close()
    _save_json(cache_path, cache)

    rows = []
    for i in ids:
        d = cache.get(i)
        if not d:
            continue
        addrs = sorted({r["address"] for r in d.get("_residences", []) if r.get("address")})
        rows.append({**d, "num_addresses": len(addrs),
                     "addresses_json": json.dumps(addrs, ensure_ascii=False) if addrs else ""})
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(output_csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=BRF_DETAILS_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    with_org = sum(1 for r in rows if r.get("org_number"))
    with_year = sum(1 for r in rows if r.get("construction_year"))
    print(f"Wrote {len(rows)} BRFs to {output_csv} "
          f"({with_org} with org number, {with_year} with construction year)")

    # Flatten the per-BRF residence inventories into one CSV.
    residences_csv.parent.mkdir(parents=True, exist_ok=True)
    n_res = 0
    with open(residences_csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=BRF_RESIDENCE_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for bid in ids:
            for res in (cache.get(bid) or {}).get("_residences", []):
                w.writerow({"booli_brf_id": bid, **res})
                n_res += 1
    print(f"Wrote {n_res} residences to {residences_csv}")


def main():
    # Line-buffer stdout so progress prints flush at every newline even when the
    # output is redirected to a file or pipe (block-buffered by default). Long
    # område/enrichment runs are otherwise a silent black box for many minutes.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--area-id",
                    help="Booli areaId for a kommun-wide sold scrape (all sales, "
                         "all property types). Mutually exclusive with --brf-id.")
    ap.add_argument("--area-url",
                    help="Explicit kommun sold-search URL (overrides the one built "
                         "from --area-id, if Booli's URL shape differs).")
    ap.add_argument("--area-parent", type=int, default=None,
                    help="Kommun areaId: scrape ALL its sold listings område-by-"
                         "område, iterating the children in booli_areas.csv (built "
                         "by resolve_areas.py) and deduping cards across områden. "
                         "Keeps each query under Booli's result-page cap for large "
                         "kommuns. Writes to --output-csv.")
    ap.add_argument("--areas-csv", type=Path, default=DEFAULT_AREAS_CSV,
                    help=f"Område driver table for --area-parent "
                         f"(default: {DEFAULT_AREAS_CSV})")
    ap.add_argument("--forsale-id",
                    help="Booli areaId for a kommun-wide for-sale scrape (current "
                         "advertisements). Writes to --ads-csv.")
    ap.add_argument("--forsale-url",
                    help="Explicit kommun for-sale (till-salu) URL.")
    ap.add_argument("--ads-csv", type=Path, default=DEFAULT_ADS_CSV,
                    help=f"Advertisements output (default: {DEFAULT_ADS_CSV})")
    ap.add_argument("--max-pages", type=int, default=None,
                    help="Cap the number of result pages (area mode) — handy for "
                         "a quick test run before the full ~380-page scrape.")
    ap.add_argument("--circle", default=None,
                    help="Limit results to a circle: 'lat,lon,radius_km' (e.g. "
                         "'59.2694036,18.1098588,3'). Booli has no radius search, so "
                         "this scrapes områden as usual, then post-filters cards by "
                         "haversine distance from the centre (rows without coordinates "
                         "are dropped). For-sale also switches to område-by-område so a "
                         "large kommun doesn't truncate. Applies to --area-parent (sold) "
                         "and --forsale-id (ads).")
    ap.add_argument("--enrich-brf", type=Path, default=None,
                    help="Enrich an existing kommun CSV: fetch one detail page per "
                         "unique bostadsrätt building to fill booli_brf_id/brf_name. "
                         "Writes to --output-csv. Resumable via a disk cache.")
    ap.add_argument("--max-buildings", type=int, default=None,
                    help="Cap buildings fetched in --enrich-brf (for a test run).")
    ap.add_argument("--enrich-details", type=Path, default=None,
                    help="Enrich an existing advertisements CSV: fetch each listing's "
                         "detail page for the full photo gallery + construction year and "
                         "write photo_urls/construction_year back. Writes to --ads-csv. "
                         "Resumable via a disk cache.")
    ap.add_argument("--max-listings", type=int, default=None,
                    help="Cap listings fetched in --enrich-details (for a test run).")
    ap.add_argument("--refetch-photos", action="store_true",
                    help="With --enrich-details, also re-fetch already-cached listings "
                         "whose photos predate the primaryLabel/alt metadata, to backfill "
                         "per-image categories. Leaves the rest of the cache intact.")
    ap.add_argument("--enrich-sold-details", type=Path, default=None,
                    help="Enrich an existing sold listings CSV: fetch each bostadsrätt "
                         "listing's detail page to extract monthly_fee_kr (avgift at the "
                         "time of the deal). Writes to --output-csv. Resumable via a "
                         "disk cache.")
    ap.add_argument("--max-sold-listings", type=int, default=None,
                    help="Cap listings fetched in --enrich-sold-details (for a test run).")
    ap.add_argument("--sold-years", type=int, default=5,
                    help="Avgift backfill (--enrich-sold-details) only fetches sales "
                         "from the last N years (default 5 — the analytically valuable, "
                         "fast slice). Use 0 for the full history; the disk cache makes "
                         "widening the window later incremental (only new years fetched).")
    ap.add_argument("--load-brf-details", action="store_true",
                    help="Fetch per-BRF detail (org number, facts, economy) for every "
                         "distinct booli_brf_id in --input-csv; write one row per BRF.")
    ap.add_argument("--input-csv", type=Path, default=DEFAULT_CSV,
                    help=f"Source CSV for --load-brf-details ids (default: {DEFAULT_CSV})")
    ap.add_argument("--brf-details-csv", type=Path, default=DEFAULT_BRF_DETAILS,
                    help=f"Output for --load-brf-details (default: {DEFAULT_BRF_DETAILS})")
    ap.add_argument("--brf-residences-csv", type=Path, default=DEFAULT_BRF_RESIDENCES,
                    help=f"Residence inventory output (default: {DEFAULT_BRF_RESIDENCES})")
    ap.add_argument("--brf-id", type=int, default=267023,
                    help="Booli housingCoopId (default: 267023 = HSB BRF Sjötungan)")
    ap.add_argument("--street-name", default="Myggdalsvägen",
                    help="Street name used to parse the building number (BRF mode only)")
    ap.add_argument("--brf-name", default=DEFAULT_BRF_NAME,
                    help=f"Förening name stamped on every row (default: {DEFAULT_BRF_NAME!r})")
    ap.add_argument("--kommun", default=DEFAULT_KOMMUN,
                    help=f"Municipality stamped on every row (default: {DEFAULT_KOMMUN!r})")
    ap.add_argument("--output-csv", type=Path, default=DEFAULT_CSV,
                    help=f"Per-listing CSV output (default: {DEFAULT_CSV})")
    ap.add_argument("--output-raw", type=Path, default=DEFAULT_RAW,
                    help=f"Raw GraphQL payload archive (default: {DEFAULT_RAW})")
    ap.add_argument("--from-raw", type=Path, default=None,
                    help="Rebuild the CSV from a saved raw JSON payload instead "
                         "of fetching live (no Playwright/network).")
    args = ap.parse_args()
    circle = _parse_circle(args.circle) if args.circle else None  # (lat, lon, radius_km)

    # Per-BRF detail load (org number, facts, economy) for distinct BRFs.
    if args.load_brf_details:
        load_brf_details(args.input_csv, args.brf_details_csv, BRF_DETAILS_CACHE,
                         args.brf_residences_csv)
        return

    # Enrich an existing kommun CSV with förening id/name from detail pages.
    if args.enrich_brf:
        enrich_brf(args.enrich_brf, args.output_csv, BRF_CACHE,
                   max_buildings=args.max_buildings)
        return

    # Enrich an existing advertisements CSV with the full photo gallery.
    if args.enrich_details:
        enrich_details(args.enrich_details, args.ads_csv, DETAILS_CACHE,
                       max_listings=args.max_listings,
                       refetch_photos=args.refetch_photos)
        return

    # Enrich an existing sold listings CSV with avgift from listing detail pages.
    if args.enrich_sold_details:
        enrich_sold_details(args.enrich_sold_details, args.output_csv,
                            SOLD_DETAILS_CACHE,
                            max_listings=args.max_sold_listings,
                            years=args.sold_years)
        return

    # Kommun-wide for-sale scrape: current advertisements.
    if args.forsale_id or args.forsale_url:
        if circle and args.forsale_id:
            # Circle mode: iterate the kommun's områden (so a large kommun's SERP
            # can't truncate) then distance-filter the merged ads.
            area_ids = read_area_ids(args.forsale_id, args.areas_csv)
            if not area_ids:
                print(f"No områden for parent {args.forsale_id} in {args.areas_csv} — "
                      f"run resolve_areas.py --kommun-id {args.forsale_id} first.")
                return
            print(f"Circle mode: scraping ads across {len(area_ids)} områden "
                  f"under kommun {args.forsale_id}")
            cards = fetch_areas_forsale(area_ids, kommun=args.kommun,
                                        max_pages=args.max_pages)
        else:
            url = args.forsale_url or FORSALE_URL_TPL.format(area_id=args.forsale_id)
            cards = fetch_area_forsale(url, raw_path=args.output_raw,
                                       max_pages=args.max_pages)
        print(f"Captured {len(cards)} for-sale listings")
        rows = rows_from_listing_cards(cards, args.kommun)
        if circle:
            rows = _filter_rows_to_circle(rows, *circle)
        write_ads_csv(rows, args.ads_csv)
        return

    # Område-by-område scrape: iterate a kommun's children (booli_areas.csv) and
    # merge, deduping across områden. The completeness path for large kommuns.
    if args.area_parent:
        area_ids = read_area_ids(args.area_parent, args.areas_csv)
        if not area_ids:
            print(f"No områden for parent {args.area_parent} in {args.areas_csv} — "
                  f"run resolve_areas.py --kommun-id {args.area_parent} first.")
            return
        print(f"Scraping {len(area_ids)} områden under kommun {args.area_parent}")
        cards = fetch_areas_sold(area_ids, kommun=args.kommun, max_pages=args.max_pages)
        print(f"Captured {len(cards)} unique sold cards across {len(area_ids)} områden")
        rows = rows_from_area_cards(cards, args.kommun)
        if circle:
            rows = _filter_rows_to_circle(rows, *circle)
        write_csv(rows, args.output_csv)
        return

    # Kommun-wide area scrape: all sales across Tyresö, not one BRF.
    if args.area_id or args.area_url:
        url = args.area_url or SOLD_SEARCH_URL_TPL.format(area_id=args.area_id)
        cards = fetch_area_sold(url, raw_path=args.output_raw, max_pages=args.max_pages)
        print(f"Captured {len(cards)} unique sold cards")
        rows = rows_from_area_cards(cards, args.kommun)
        write_csv(rows, args.output_csv)
        return

    if args.from_raw:
        payload = json.loads(Path(args.from_raw).read_text(encoding="utf-8"))
        print(f"Loaded raw payload from {args.from_raw}")
    else:
        payload = fetch_brf_sold(args.brf_id)
        sold = (payload.get("data") or {}).get("sold") or []
        print(f"Captured {len(sold)} sold listings for BRF {args.brf_id}")
        payload["data"]["sold"] = strip_personal(sold)
        args.output_raw.parent.mkdir(parents=True, exist_ok=True)
        args.output_raw.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        print(f"Raw JSON written to {args.output_raw}")

    rows = rows_from_payload(payload, args.street_name, args.brf_id, args.brf_name,
                             args.kommun)
    write_csv(rows, args.output_csv)


if __name__ == "__main__":
    main()
