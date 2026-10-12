#!/usr/bin/env python3
"""Refresh the Booli snapshot in market/booli/ from booli.se (live scraping, tools/booli/scrape.py).

    ads      the listings for sale now, in every kommun of KOMMUNS: område by område (the områden in
             booli_areas.csv) and the kommun-wide search, unioned on listing_id (each misses listings
             the other finds). A listing still for sale keeps what the previous snapshot knew about it
             (tenure, listing date, photos, construction year, apartment number), which the new search
             cards lack; only listings not seen before get their detail page fetched (--details, capped
             by --max-details, cached so a re-run continues where it stopped). A listing no longer for
             sale drops out. If a kommun comes back with under MIN_KEEP of its previous count (a throttled
             or broken run), its previous listings are kept and the run says so. Every search is cached in
             .cache/booli/serp/ for --max-age days: on a Cloudflare challenge the run stops without writing,
             and the next run continues from the cache.
    pack     gzip the working CSVs back into market/booli/ and record the refresh in market/sources.yaml

The working copies live in .cache/booli/work/ (unpacked from market/booli/ when missing). Nothing is written
to market/ before `pack`. After packing: tools/cloud/parity.py (optional) and tools/cloud/provision.py.

    .venv/bin/python tools/booli/refresh.py ads                       # search pages only (~600 pages)
    .venv/bin/python tools/booli/refresh.py ads --kommun Tyresö       # one kommun
    .venv/bin/python tools/booli/refresh.py ads --details --max-details 2000
    .venv/bin/python tools/booli/refresh.py pack

Booli is behind Cloudflare and throttles per IP; see the data repo's src/extract/booli/throttling.md. A run of
search pages plus a few thousand paced detail pages has stayed well within one home IP's daily budget.
"""
import argparse
import csv
import gzip
import json
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scrape  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
MARKET = REPO / "market" / "booli"
SOURCES = REPO / "market" / "sources.yaml"
WORK = scrape.WORK
SERP_CACHE = REPO / ".cache" / "booli" / "serp"   # one file per search, reused for --max-age days
FILES = ("advertisements_booli.csv", "sales_booli.csv", "brf_details_booli.csv", "brf_residences_booli.csv",
         "brf_address_links_allabrf.csv", "booli_areas.csv")
KOMMUNS = {"Stockholm": 1, "Nacka": 76, "Solna": 35, "Tyresö": 184}   # Booli kommun areaIds
MIN_KEEP = 0.7
KOMMUN_MAX_PAGES = 400   # the kommun-wide search (Stockholm ~235 pages); never more
# what a listing still for sale keeps from the previous snapshot when the new search card lacks it
CARRY_OVER = ("tenure", "listed_date", "apartment_number", "construction_year", "photo_urls", "broker_agency")


def read_rows(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def unpack():
    """market/booli/<file>.gz -> .cache/booli/work/<file>, for the files not unpacked yet."""
    WORK.mkdir(parents=True, exist_ok=True)
    for fn in FILES:
        if not (WORK / fn).exists():
            with gzip.open(MARKET / (fn + ".gz"), "rb") as src, open(WORK / fn, "wb") as dst:
                shutil.copyfileobj(src, dst)
            print(f"unpacked {fn}")


def serp(area_id, name, mis_scoped_pages, max_pages, max_age_days):
    """The for-sale cards of one search (an område, or a whole kommun), from SERP_CACHE when fetched within
    max_age_days, else live (and then cached), so an interrupted refresh resumes where it stopped."""
    path = SERP_CACHE / f"{area_id}.json"
    if path.exists():
        cached = json.loads(path.read_text(encoding="utf-8"))
        if date.fromisoformat(cached["fetched"]) >= date.today() - timedelta(days=max_age_days):
            print(f"  {name}: {len(cached['cards'])} listings from the cache ({cached['fetched']})")
            return cached["cards"]
    print(f"  {name} (areaId {area_id})")
    cards = scrape.fetch_area_forsale(scrape.FORSALE_URL_TPL.format(area_id=area_id), max_pages=max_pages,
                                      mis_scoped_pages=mis_scoped_pages)
    SERP_CACHE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fetched": date.today().isoformat(), "cards": cards}, ensure_ascii=False),
                    encoding="utf-8")
    return cards


def refresh_ads(details, max_details, only=None, max_age_days=3):
    unpack()
    previous = read_rows(WORK / "advertisements_booli.csv")
    by_id = {r["listing_id"]: r for r in previous}
    out, report = [], []
    for kommun, area_id in KOMMUNS.items():
        old = [r for r in previous if r["kommun"] == kommun]
        if only and kommun not in only:
            out += old
            continue
        print(f"== {kommun} (areaId {area_id}): {len(old)} listings in the previous snapshot")
        cards = {}
        try:
            for aid, name in scrape.read_area_ids(area_id, WORK / "booli_areas.csv"):
                for c in serp(aid, name, scrape.AREA_MIS_SCOPED_PAGES, None, max_age_days):
                    if scrape._card_kommun(c) in (None, kommun):
                        cards[str(c.get("listingId"))] = c
            iterated = len(cards)
            for c in serp(area_id, f"{kommun} (kommun search)", None, KOMMUN_MAX_PAGES, max_age_days):
                if scrape._card_kommun(c) in (None, kommun):
                    cards.setdefault(str(c.get("listingId")), c)
        except RuntimeError as e:   # a Cloudflare challenge: the IP needs rest
            print(f"\nSTOPPED at {kommun}: {e}\nNothing written. The search pages fetched so far are cached in "
                  f"{SERP_CACHE} (reused for {max_age_days} days); re-run later, ideally after a night's rest.")
            return
        rows = [r for r in scrape.rows_from_listing_cards(list(cards.values()), kommun) if r["kommun"] == kommun]
        if len(rows) < MIN_KEEP * len(old):
            report.append(f"{kommun}: KEPT the previous {len(old)} listings: only {len(rows)} came back "
                          f"(under {MIN_KEEP:.0%}); re-run later")
            out += old
            continue
        carried = 0
        for r in rows:
            prev = by_id.get(str(r["listing_id"]))
            if prev:
                for col in CARRY_OVER:
                    if not r.get(col) and prev.get(col):
                        r[col] = prev[col]
                carried += 1
        report.append(f"{kommun}: {len(rows)} listings ({iterated} by område, {len(rows) - iterated} more from "
                      f"the kommun search); {carried} still for sale, {len(rows) - carried} new, "
                      f"{len(old) - carried} gone")
        out += rows
    scrape.write_ads_csv(out, WORK / "advertisements_booli.csv")
    if details:
        scrape.enrich_details(WORK / "advertisements_booli.csv", WORK / "advertisements_booli.csv",
                              scrape.DETAILS_CACHE, max_listings=max_details)
    print("\n".join(["", "== ads refreshed"] + report))


def pack(what):
    """Gzip the working CSVs back into market/booli/ (mtime 0: an unchanged file gives an unchanged .gz) and
    record the refresh in market/sources.yaml."""
    for fn in FILES:
        if not (WORK / fn).exists():
            continue
        data = (WORK / fn).read_bytes()
        old = gzip.decompress((MARKET / (fn + ".gz")).read_bytes()) if (MARKET / (fn + ".gz")).exists() else None
        if data == old:
            continue
        with open(MARKET / (fn + ".gz"), "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
            gz.write(data)
        print(f"packed {fn}")
    text = SOURCES.read_text(encoding="utf-8")
    marker = "refreshed_here:"
    line = f"  - {{date: {date.today().isoformat()}, what: {what}, by: tools/booli/refresh.py}}"
    text = text.replace(marker, f"{marker}\n{line}", 1) if marker in text else text.rstrip("\n") + f"\n{marker}\n{line}\n"
    SOURCES.write_text(text, encoding="utf-8")
    print(f"market/sources.yaml: {line.strip()}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["ads", "pack"])
    ap.add_argument("--details", action="store_true", help="ads: fetch the detail page of new listings")
    ap.add_argument("--max-details", type=int, default=None, help="ads: at most this many detail pages")
    ap.add_argument("--kommun", action="append", choices=list(KOMMUNS),
                    help="ads: only this kommun (repeatable); the others keep their listings")
    ap.add_argument("--max-age", type=int, default=3,
                    help="ads: reuse a search fetched within this many days (resume an interrupted refresh)")
    ap.add_argument("--what", default="advertisements", help="pack: what was refreshed (for sources.yaml)")
    args = ap.parse_args()
    if args.command == "ads":
        refresh_ads(args.details, args.max_details, args.kommun, args.max_age)
    else:
        pack(args.what)


if __name__ == "__main__":
    main()
