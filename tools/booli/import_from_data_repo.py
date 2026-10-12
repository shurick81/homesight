#!/usr/bin/env python3
"""Copy the Booli data for the BRFs in annual_reviews/brfs/index.csv out of the `data` repo's cache.

Reads the canonical CSVs in <data repo>/data/processed/booli/ (they are produced by that repo's Booli
scrapers) and writes, for every BRF in index.csv that Booli knows by its orgnr:

    market/brfs/booli_brfs.csv          Booli's facts about the BRF, one row per BRF
    market/brfs/<orgnr>/sales.csv       completed sales (Booli links the sale to the BRF)
    market/brfs/<orgnr>/listings.csv    apartments for sale at the time of the snapshot
    market/brfs/<orgnr>/apartments.csv  the BRF's apartment register on Booli
    market/booli/*.csv.gz               the whole snapshot (every kommun the data repo covers), gzipped: the
                                        input of the advertisements read model (tools/readmodel/advertisements.py)
    market/sources.yaml                 data repo commit and snapshot date of each input

market/address_links.csv (kept by hand, each row with its evidence) links addresses to a BRF where Booli has
no apartment register for it. Those links attach listings, and sales that Booli did not link to any BRF.

Listings carry no BRF link on Booli, so they are attached the way the data repo's import does it
(link_advertisement_brf): the Booli residence id in the URL if it is in a register, else the full
address, else the address without its entrance letter when that maps to one BRF only. `linked_by` says
which. Re-running replaces the files, so a listing that is no longer for sale disappears.

    python3 tools/booli/import_from_data_repo.py --data-repo ../data
"""
import argparse
import csv
import gzip
import json
import re
import subprocess
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
INDEX_CSV = REPO / "annual_reviews" / "brfs" / "index.csv"
OUT = REPO / "market"

BRF_COLS = ["orgnr", "booli_brf_id", "name", "construction_year", "num_units", "num_rental_units",
            "total_living_area_m2", "plot_ownership", "is_genuine", "total_loan_kr", "annual_report_year",
            "has_valid_annual_report", "economy_json", "description", "addresses_json", "booli_url"]
SALE_COLS = ["sold_date", "linked_by", "address", "apartment_number", "rooms", "living_area_m2", "final_price_kr",
             "asking_price_kr", "kr_per_m2", "monthly_fee_kr", "price_change_pct", "object_type", "object_form",
             "tenure", "broker_agency", "area_name", "lat", "lng", "listing_id", "booli_url"]
LISTING_COLS = ["listing_id", "listed_date", "asking_price_kr", "property_type", "object_form", "tenure",
                "address", "street_number", "apartment_number", "rooms", "living_area_m2", "kr_per_m2", "monthly_fee_kr",
                "construction_year", "broker_agency", "kommun", "area_name", "lat", "lng", "linked_by", "source_url",
                "attributes_json", "photo_urls"]
APARTMENT_COLS = ["residence_booli_id", "address", "apartment_number", "rooms", "living_area_m2", "floor",
                  "cadastral", "lat", "lng", "url"]

INPUTS = {
    "brf_details": "brf_details_booli.csv",
    "sales": "sales_booli.csv",
    "advertisements": "advertisements_booli.csv",
    "residences": "brf_residences_booli.csv",
    "allabrf_links": "brf_address_links_allabrf.csv",
    "areas": "booli_areas.csv",
}


def copy_snapshot(src):
    """market/booli/<input>.csv.gz: byte-for-byte gzip of each input (mtime 0, so an unchanged input gives an
    unchanged file)."""
    out = OUT / "booli"
    out.mkdir(parents=True, exist_ok=True)
    for fn in INPUTS.values():
        data = (src / fn).read_bytes()
        with open(out / (fn + ".gz"), "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
            gz.write(data)


def read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def write_csv(path, cols, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def relaxed(addr_key):
    """'sikvägen 29a' -> 'sikvägen 29' (as _relaxed_addr_key in the data repo's import_booli.py)."""
    return re.sub(r"(\d+)[a-z]$", r"\1", addr_key)


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True).stdout.strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-repo", type=Path, default=REPO.parent / "data")
    args = ap.parse_args()
    src = args.data_repo / "data" / "processed" / "booli"

    copy_snapshot(src)
    orgnrs = {r["orgnr"]: r["name"] for r in read_csv(INDEX_CSV)}
    brfs = {}                                          # booli_brf_id -> details row
    for r in read_csv(src / INPUTS["brf_details"]):
        if r["org_number"] in orgnrs:
            brfs[r["booli_brf_id"]] = dict(r, orgnr=r["org_number"],
                                          booli_url=f"https://www.booli.se/bostadsrattsforening/{r['booli_brf_id']}")
    orgnr_of = {bid: b["orgnr"] for bid, b in brfs.items()}
    missing = sorted(set(orgnrs) - set(orgnr_of.values()))

    # apartment register; also the address -> BRF map for linking listings
    apartments = {o: [] for o in orgnr_of.values()}
    residence_brf, addr_brf = {}, {}
    for r in read_csv(src / INPUTS["residences"]):
        o = orgnr_of.get(r["booli_brf_id"])
        if o:
            apartments[o].append(r)
            residence_brf[r["residence_booli_id"]] = o
    # the address map spans every BRF, not only ours, so that an ambiguous relaxed key is detected
    by_relaxed = {}
    for r in read_csv(src / INPUTS["residences"]):
        key = r["address"].strip().lower()
        if key and r["booli_brf_id"]:
            addr_brf.setdefault(key, r["booli_brf_id"])
            by_relaxed.setdefault(relaxed(key), set()).add(r["booli_brf_id"])
    relaxed_brf = {k: next(iter(v)) for k, v in by_relaxed.items() if len(v) == 1}

    links = {r["address"].strip().lower(): r["orgnr"] for r in read_csv(OUT / "address_links.csv")
             if r["orgnr"] in orgnr_of.values()} if (OUT / "address_links.csv").exists() else {}

    sales = {o: [] for o in orgnr_of.values()}
    for r in read_csv(src / INPUTS["sales"]):
        o = orgnr_of.get(r["booli_brf_id"])
        if o:
            sales[o].append(dict(r, linked_by="booli_brf_id"))
        elif not r["booli_brf_id"] and r["address"].strip().lower() in links:
            sales[links[r["address"].strip().lower()]].append(dict(r, linked_by="address_link"))

    listings = {o: [] for o in orgnr_of.values()}
    for r in read_csv(src / INPUTS["advertisements"]):
        m = re.search(r"/bostad/(\d+)", r["source_url"])
        key = r["address"].strip().lower()
        if m and m.group(1) in residence_brf:
            o, how = residence_brf[m.group(1)], "residence_id"
        elif key in links:
            o, how = links[key], "address_link"
        elif orgnr_of.get(addr_brf.get(key)):
            o, how = orgnr_of[addr_brf[key]], "address"
        elif orgnr_of.get(addr_brf.get(relaxed(key))):
            o, how = orgnr_of[addr_brf[relaxed(key)]], "address_without_letter"
        elif orgnr_of.get(relaxed_brf.get(relaxed(key))):
            o, how = orgnr_of[relaxed_brf[relaxed(key)]], "address_without_letter"
        else:
            continue
        listings[o].append(dict(r, linked_by=how))

    write_csv(OUT / "brfs" / "booli_brfs.csv", BRF_COLS, sorted(brfs.values(), key=lambda b: b["orgnr"]))
    for o in sorted(orgnr_of.values()):
        d = OUT / "brfs" / o
        write_csv(d / "sales.csv", SALE_COLS, sorted(sales[o], key=lambda r: (r["sold_date"], r["listing_id"])))
        write_csv(d / "listings.csv", LISTING_COLS,
                  sorted(listings[o], key=lambda r: (r["listed_date"], r["listing_id"])))
        write_csv(d / "apartments.csv", APARTMENT_COLS,
                  sorted(apartments[o], key=lambda r: (r["address"], r["apartment_number"], r["residence_booli_id"])))
        print(f"{o} {orgnrs[o][:40]:40} sales {len(sales[o]):4}  listings {len(listings[o]):3}  "
              f"apartments {len(apartments[o]):4}")

    rel = "data/processed/booli/"
    lines = ["# Where market/ came from: the data repo's Booli cache, written by tools/booli/import_from_data_repo.py.",
             "# snapshot = date of the last commit that changed the input file (the scrape it holds).",
             f"imported: {date.today().isoformat()}",
             f"data_repo: {git(args.data_repo, 'remote', 'get-url', 'origin')}",
             f"data_repo_commit: {git(args.data_repo, 'rev-parse', '--short', 'HEAD')}",
             "inputs:"]
    for name, fn in INPUTS.items():
        dirty = bool(git(args.data_repo, "status", "--porcelain", "--", rel + fn))
        lines += [f"  {name}:", f"    file: {rel + fn}",
                  f"    snapshot: {git(args.data_repo, 'log', '-1', '--format=%cs', '--', rel + fn)}",
                  f"    commit: {git(args.data_repo, 'log', '-1', '--format=%h', '--', rel + fn)}"]
        if dirty:
            lines.append("    uncommitted_changes: true")
    if missing:
        lines.append(f"not_on_booli: [{', '.join(missing)}]")
    (OUT / "sources.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if missing:
        print("not found on Booli by orgnr:", ", ".join(missing))


if __name__ == "__main__":
    main()
