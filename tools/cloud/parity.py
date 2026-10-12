#!/usr/bin/env python3
"""Compare the read model, as built from the repo, with the data repo's live API, field by field.

    advertisements  every listing. The data API pages by `limit` (at most 10) with no offset, so each listing
                    is fetched through a narrow filter (its kommun and exact price, area, build year, ...).
    kommuns, areas, cities
                    every row, and the row order.
    brfs            every /brfs row (fetched by brf_name_search, narrowed by kommun/omrade), and the first page
                    of every sort, unfiltered and in one kommun
    sales_stats     /sales/stats/avg-price-per-sqm for every year/rooms combination we hold, plus a few that
                    should be 404.
    all             (default) all of the above

Fields the data API does not serve (homesight's verified layer) are skipped. A field that is NULL in every one
of our rows but set in theirs is reported once as not migrated (the school block). Prints the differences
grouped by field, with examples.

    .venv/bin/python tools/cloud/parity.py                          # everything, about 3 minutes
    .venv/bin/python tools/cloud/parity.py kommuns                  # one endpoint
    .venv/bin/python tools/cloud/parity.py --kommun Tyresö          # listings of one kommun
    .venv/bin/python tools/cloud/parity.py --data-repo-linking      # link listings to BRFs as the data repo
                                                                    # does: what is left is computation
    .venv/bin/python tools/cloud/parity.py --cache /tmp/parity      # keep the data API's rows, reuse them

Known differences (see api/openapi.json): about 150 more listings linked to a BRF here, and through them the
BRF scope (units_in_scope_br, pct_for_sale_br, turnover_*, *_loan_per_m2_br) of their area, kommun and
city; street_name without a trailing space; ±1 on a trend or forecast that falls on a rounding tie.

Aurora behind the data API sleeps when idle; the first requests are retried until it answers.
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from tools.readmodel import advertisements, geo  # noqa: E402
from tools.readmodel.containers import api_ads  # noqa: E402

DATA_API = "https://wqyh106uo0.execute-api.eu-north-1.amazonaws.com"
LIMIT = 10


def fetch(params):
    url = f"{DATA_API}/advertisements?" + urllib.parse.urlencode(dict(params, limit=LIMIT))
    for attempt in range(8):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return json.load(r)["advertisements"]
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504):
                raise
        except (urllib.error.URLError, TimeoutError):
            pass
        time.sleep(2 + 3 * attempt)
    raise RuntimeError(f"no answer from {url}")


# Filters that select by exact value: API parameter(s) -> document field. Tried in order for each listing until
# one selects at most LIMIT listings of the kommun (counted on our documents).
EXACT = [
    (("min_price", "max_price"), "asking_price_kr"),
    (("min_area_m2", "max_area_m2"), "living_area_m2"),
    (("min_construction_year", "max_construction_year"), "construction_year"),
    (("max_monthly_fee",), "monthly_fee_kr"),
]
SELECTORS = [("asking_price_kr",), ("asking_price_kr", "living_area_m2"), ("living_area_m2",),
             ("living_area_m2", "area_name"), ("living_area_m2", "construction_year"),
             ("living_area_m2", "area_name", "construction_year"), ("area_name", "construction_year"),
             ("living_area_m2", "object_form", "area_name")]


def _params(a, fields):
    params = {"kommun": a["kommun"]}
    for f in fields:
        if f == "area_name":
            params["omrade"] = a["area_name"]
        elif f == "object_form":
            params["object_form"] = a["object_form"]
        else:
            names = next(n for n, field in EXACT if field == f)
            for n in names:
                params[n] = a[f]
    return params


def _matches(a, b, fields):
    return all(b[f] == a[f] if f in ("area_name", "object_form") else b[f] is not None and float(b[f]) == float(a[f])
               for f in fields)


def groups(ads):
    """Filter sets that each select at most LIMIT listings (by our documents), and the listing ids they hold.
    A max_monthly_fee filter is not exact, so it is never used alone."""
    by_kommun = defaultdict(list)
    for a in ads:
        by_kommun[a["kommun"]].append(a)
    out, seen, unreachable = {}, set(), []
    for a in ads:
        if a["listing_id"] in seen:
            continue
        for fields in SELECTORS:
            if any(a[f] is None for f in fields):
                continue
            hit = [b for b in by_kommun[a["kommun"]] if _matches(a, b, fields)]
            if len(hit) <= LIMIT:
                key = json.dumps(_params(a, fields), sort_keys=True, ensure_ascii=False)
                out[key] = [b["listing_id"] for b in hit]
                seen.update(out[key])
                break
        else:
            unreachable.append(a["listing_id"])
    return [(json.loads(k), ids) for k, ids in out.items()], unreachable


def same(ours, theirs):
    if ours == theirs:
        return True
    if isinstance(ours, (int, float)) and isinstance(theirs, (int, float, str)) and not isinstance(ours, bool):
        try:
            return abs(float(ours) - float(theirs)) < 1e-6
        except ValueError:
            return False
    return False


def compare(ours, theirs, show, keyed_by=None):
    """ours, theirs: {key: row}. Returns (diffs Counter, examples, not migrated fields, relinked kinds)."""
    diffs, examples, links = Counter(), defaultdict(list), defaultdict(list)
    unmigrated = {k for k in {k for r in ours.values() for k in r}
                  if all(r.get(k) is None for r in ours.values())
                  and any(t.get(k) is not None for t in theirs.values())}
    for key, a in ours.items():
        t = theirs.get(key)
        if t is None:
            continue
        # a listing linked to another BRF (or only on one side) differs in every brf_* field: count it once
        relinked = "brf_org_number" in a and (a["brf_org_number"] != t.get("brf_org_number")
                                              or a["brf_name"] != t.get("brf_name"))
        if relinked:
            kind = ("linked only here" if t.get("brf_name") is None else
                    "linked only in the data repo" if a["brf_name"] is None else "linked to another BRF")
            links[kind].append(key)
        for k, v in a.items():
            if k not in t or k in unmigrated or (relinked and k.startswith("brf_")):
                continue
            if not same(v, t[k]):
                diffs[k] += 1
                if len(examples[k]) < show:
                    examples[k].append((key, v, t[k]))
    return diffs, examples, sorted(unmigrated), links


def report(name, ours, theirs, show):
    diffs, examples, unmigrated, links = compare(ours, theirs, show)
    missing = [k for k in ours if k not in theirs]
    only_ours = sorted({k for a in ours.values() for k in a} - {k for t in theirs.values() for k in t})
    print(f"== {name}: compared {len(ours) - len(missing)}; not in the data API: {len(missing)} {missing[:5]}")
    if only_ours:
        print(f"fields the data API does not serve: {', '.join(only_ours)}")
    if unmigrated:
        print(f"not migrated (NULL here): {', '.join(unmigrated)}")
    for kind, ids in links.items():
        print(f"BRF link: {len(ids)} {kind} (their brf_* fields not compared), e.g. {ids[:5]}")
    if not diffs:
        print("no differences")
    for k, n in diffs.most_common():
        print(f"{k}: {n} differ")
        for key, v, tv in examples[k]:
            print(f"    {key}: ours {json.dumps(v, ensure_ascii=False)[:100]}  "
                  f"theirs {json.dumps(tv, ensure_ascii=False)[:100]}")


def cached(cache, name, fetch_all):
    path = cache / f"{name}.json" if cache else None
    if path and path.exists():
        return json.loads(path.read_text())
    rows = fetch_all()
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rows, ensure_ascii=False))
    return rows


def parity_advertisements(args):
    ads = [a for a in geo.advertisements() if not args.kommun or a["kommun"] == args.kommun]
    ours = {a["listing_id"]: {k: v for k, v in a.items() if k not in api_ads.HIDDEN_FIELDS} for a in ads}
    todo, unreachable = groups(ads)
    print(f"{len(ads)} listings; {len(unreachable)} not selectable through the data API's filters")

    def fetch_all():
        theirs, done = {}, 0
        with ThreadPoolExecutor(args.workers) as pool:
            for rows in pool.map(lambda g: fetch(g[0]), todo):
                for r in rows:
                    theirs[str(r["listing_id"])] = r
                done += 1
                if done % 500 == 0:
                    print(f"  {done}/{len(todo)} requests", file=sys.stderr)
        return theirs

    theirs = cached(args.cache, "advertisements" + (f"_{args.kommun}" if args.kommun else ""), fetch_all)
    report("advertisements", {k: v for k, v in ours.items() if k not in unreachable}, theirs, args.show)


def parity_level(name, args):
    keys = geo.LEVELS[name]
    ours_rows = [{k: v for k, v in d.items() if k not in geo.HIDDEN_FIELDS} for d in getattr(geo, name)()]
    theirs_rows = cached(args.cache, name, lambda: get(name)[name])
    key = lambda r: " / ".join(str(r[k]) for k in keys)  # noqa: E731
    report(name, {key(r): r for r in ours_rows}, {key(r): r for r in theirs_rows}, args.show)
    if [key(r) for r in ours_rows] != [key(r) for r in theirs_rows]:
        print("row order differs")


def parity_brfs(args):
    from tools.readmodel import brfs as readmodel_brfs
    from tools.readmodel.containers import api_brfs

    docs = readmodel_brfs.build()
    # rows only homesight has (verified BRFs with nothing for sale) are not compared
    listed = [d for d in docs if d["active_listing_count"] > 0]
    ours = {d["brf_org_number"]: api_brfs.clean(d) for d in listed}
    print(f"{len(listed)} BRF rows with listings; {len(docs) - len(listed)} homesight-only (no listing)")

    def selector(d):
        name = d["brf_name"].lower()
        for keys in ((), ("kommun",), ("kommun", "area_name")):
            hit = [o for o in listed if name in o["brf_name"].lower() and all(o[k] == d[k] for k in keys)]
            if len(hit) <= 10:
                q = {"brf_name_search": d["brf_name"]}
                q.update({("omrade" if k == "area_name" else k): d[k] for k in keys})
                return json.dumps(q, sort_keys=True, ensure_ascii=False)
        return None

    def fetch_all():
        queries = {q for q in map(selector, listed) if q}
        rows = {}
        with ThreadPoolExecutor(args.workers) as pool:
            for page in pool.map(lambda q: get("brfs?" + urllib.parse.urlencode(dict(json.loads(q), limit=10)))["brfs"],
                                 queries):
                rows.update({r["brf_org_number"]: r for r in page})
        sorts = {}
        for sort in api_brfs.SORTS:
            for scope in ({}, {"kommun": "Tyresö"}):
                key = json.dumps(dict(scope, sort=sort), ensure_ascii=False)
                sorts[key] = [r["brf_org_number"]
                              for r in get("brfs?" + urllib.parse.urlencode(dict(scope, sort=sort, limit=10)))["brfs"]]
        return {"rows": rows, "sorts": sorts}

    theirs = cached(args.cache, "brfs", fetch_all)
    report("brfs", ours, theirs["rows"], args.show)

    def first_page(sort, kommun=None):
        field, direction = api_brfs.SORTS[sort]
        rows = [d for d in listed if kommun is None or d["kommun"] == kommun]
        have = sorted((d for d in rows if d[field] is not None), key=lambda d: d["brf_org_number"])
        have.sort(key=lambda d: d[field], reverse=direction == "DESC")
        rest = sorted((d for d in rows if d[field] is None), key=lambda d: d["brf_org_number"])
        return [d["brf_org_number"] for d in (have + rest)[:10]]

    wrong = []
    for key, orgs in theirs["sorts"].items():
        q = json.loads(key)
        if first_page(q["sort"], q.get("kommun")) != orgs:
            wrong.append(key)
    print(f"sort order: {len(theirs['sorts']) - len(wrong)}/{len(theirs['sorts'])} first pages identical"
          + (f"; differ: {wrong}" if wrong else ""))


def parity_sales_stats(args):
    from tools.readmodel import sales_stats

    ours = {d["id"]: {k: v for k, v in d.items() if k != "id"} for d in sales_stats.build()}
    probes = list(ours) + ["1990_all", "2025_19.5", "all_17.5"]

    def fetch_all():
        def one(doc_id):
            year, rooms = doc_id.split("_")
            qs = urllib.parse.urlencode({k: v for k, v in (("year", year), ("rooms", rooms)) if v != "all"})
            try:
                return doc_id, get(f"sales/stats/avg-price-per-sqm?{qs}")
            except urllib.error.HTTPError as e:
                return doc_id, {"status": e.code}
        with ThreadPoolExecutor(args.workers) as pool:
            return dict(pool.map(one, probes))

    theirs = cached(args.cache, "sales_stats", fetch_all)
    absent = [k for k in probes if k not in ours]
    wrong_404 = [k for k in absent if theirs.get(k, {}).get("status") != 404]
    print(f"404 where we hold no document: {len(absent) - len(wrong_404)}/{len(absent)} {wrong_404}")
    report("sales_stats", ours, {k: v for k, v in theirs.items() if "status" not in v}, args.show)


def get(path):
    url = f"{DATA_API}/{path}"
    for attempt in range(8):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504):
                raise
        except (urllib.error.URLError, TimeoutError):
            pass
        time.sleep(2 + 3 * attempt)
    raise RuntimeError(f"no answer from {url}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("endpoint", nargs="?", default="all",
                    choices=["all", "advertisements", *geo.LEVELS, "brfs", "sales_stats"])
    ap.add_argument("--kommun", help="advertisements: only this kommun")
    ap.add_argument("--show", type=int, default=3, help="examples per field")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--data-repo-linking", action="store_true",
                    help="link listings to BRFs exactly as the data repo does")
    ap.add_argument("--cache", type=Path, help="directory for the data API's rows: written after fetching, "
                                                "read instead of fetching when present")
    args = ap.parse_args()
    if args.data_repo_linking:
        advertisements.LINKING = "data-repo"
    for name in (["advertisements", *geo.LEVELS, "brfs", "sales_stats"] if args.endpoint == "all"
                 else [args.endpoint]):
        if name == "advertisements":
            parity_advertisements(args)
        elif name == "brfs":
            parity_brfs(args)
        elif name == "sales_stats":
            parity_sales_stats(args)
        else:
            parity_level(name, args)


if __name__ == "__main__":
    main()
