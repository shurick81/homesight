#!/usr/bin/env python3
"""Blue/green deployment of the read model to Cosmos DB, as the data repo does it with Postgres schemas.

Two databases, homesight_blue and homesight_green, each holding every container in
tools/readmodel/containers.py. The Function reads the one named by its COSMOS_DATABASE setting, which
infra/main.bicepparam sets (`activeDatabase`). The live database is never written to:

    status      which database is live
    provision   rebuild the INACTIVE database from the repo: drop it, create it with every container
                (partition key + indexing policy), load all documents, then verify it (document
                counts, and every /advertisements sort and a filtered and a radius query run against it)
    flip        make the inactive database live: set activeDatabase in infra/main.bicepparam and
                deploy infra/main.bicep (restarts the Function; the old database stays as rollback)

    .venv/bin/python tools/cloud/provision.py status
    .venv/bin/python tools/cloud/provision.py provision
    .venv/bin/python tools/cloud/provision.py flip          # run again to roll back

Needs the Azure CLI signed in: databases and containers are created through Azure Resource Manager
(Cosmos key auth is off), documents are written with the signed-in user's Cosmos data role.
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from tools.readmodel.containers import CONTAINERS, api_ads  # noqa: E402

RESOURCE_GROUP = "homesight"
FUNCTION_APP = "homesight-api"
PARAMS_FILE = REPO / "infra" / "main.bicepparam"
COLORS = ("homesight_blue", "homesight_green")


def az(*args, parse=True):
    out = subprocess.run(["az", *args, "-o", "json" if parse else "none", "--only-show-errors"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"az {' '.join(args[:4])} failed:\n{out.stderr}")
    return json.loads(out.stdout) if parse and out.stdout.strip() else None


def cosmos_account():
    accounts = az("cosmosdb", "list", "-g", RESOURCE_GROUP, "--query", "[].{name:name, endpoint:documentEndpoint}")
    if len(accounts) != 1:
        sys.exit(f"expected one Cosmos account in {RESOURCE_GROUP}, found {len(accounts)}")
    return accounts[0]


def live_database():
    settings = az("functionapp", "config", "appsettings", "list", "-g", RESOURCE_GROUP, "-n", FUNCTION_APP)
    return next((s["value"] for s in settings if s["name"] == "COSMOS_DATABASE"), None)


def inactive(live):
    return COLORS[1] if live == COLORS[0] else COLORS[0]


def _create(container, doc, attempts=30):
    """create_item, retried on 429 for as long as it takes: serverless caps a partition at 5 000 RU/s, which a
    bulk load of large documents reaches; the SDK's own retries give up after about 30 s."""
    from azure.cosmos.exceptions import CosmosHttpResponseError

    for attempt in range(attempts):
        try:
            return container.create_item(doc)
        except CosmosHttpResponseError as e:
            if e.status_code != 429 or attempt == attempts - 1:
                raise
            time.sleep(min(2 ** attempt, 20))


def provision(target):
    acct = cosmos_account()
    base = ["-g", RESOURCE_GROUP, "-a", acct["name"]]
    existing = [d["name"] for d in az("cosmosdb", "sql", "database", "list", *base)]
    if target in existing:
        print(f"dropping {target}")
        az("cosmosdb", "sql", "database", "delete", *base, "-n", target, "--yes", parse=False)
    print(f"creating {target}")
    az("cosmosdb", "sql", "database", "create", *base, "-n", target, parse=False)
    for name, spec in CONTAINERS.items():
        args = ["cosmosdb", "sql", "container", "create", *base, "-d", target, "-n", name,
                "-p", spec["partition_key"]]
        if spec["indexing_policy"]:
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
                json.dump(spec["indexing_policy"], f)
            args += ["--idx", f"@{f.name}"]
        az(*args, parse=False)
        print(f"  container {name} (partition {spec['partition_key']})")

    from azure.cosmos import CosmosClient
    from azure.identity import DefaultAzureCredential

    db = CosmosClient(acct["endpoint"], credential=DefaultAzureCredential()).get_database_client(target)
    expected = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for name, spec in CONTAINERS.items():
            docs = spec["build"]()
            pk = spec["partition_key"].lstrip("/")
            keys = [(d[pk], d["id"]) for d in docs]   # an id is unique within its partition
            assert len(keys) == len(set(keys)), f"duplicate ids in {name}"
            container = db.get_container_client(name)
            list(pool.map(lambda d: _create(container, d), docs))
            expected[name] = len(docs)
            print(f"  loaded {len(docs)} into {name}")
    verify(db, expected)


def verify(db, expected):
    """Counts per container, then the API's own query code against this database: every sort (each
    needs its composite index, so a missing index fails here, not in production), a filter and a radius."""
    for name, n in expected.items():
        got = list(db.get_container_client(name).query_items("SELECT VALUE COUNT(1) FROM c",
                                                             enable_cross_partition_query=True))[0]
        if got != n:
            sys.exit(f"verify: {name} holds {got} documents, expected {n}")
    ads = db.get_container_client("advertisements")
    total_ru, worst = 0.0, (0.0, "")
    for sort in api_ads.SORTS:
        rows, ru = api_ads.query(ads, api_ads.parse({"sort": sort}))
        if len(rows) != min(10, expected["advertisements"]):
            sys.exit(f"verify: sort={sort} returned {len(rows)} rows")
        total_ru += ru
        worst = max(worst, (ru, sort))
    sample = api_ads.query(ads, api_ads.parse({"sort": "listed_date"}))[0][0]
    checks = {
        "brf_org_number + price_asc": {"brf_org_number": sample["brf_org_number"], "sort": "price_asc"},
        "kommun + min_rooms": {"kommun": sample["kommun"], "min_rooms": "2"},
        "radius 1 km": {"near_lat": str(sample["location"]["lat"]), "near_lng": str(sample["location"]["lng"]),
                        "radius_m": "1000"},
    }
    for label, qs in checks.items():
        rows, ru = api_ads.query(ads, api_ads.parse(qs))
        if not rows:
            sys.exit(f"verify: {label} returned no rows")
        print(f"  {label}: {len(rows)} rows, {ru:.1f} RU")
    print(f"verified: counts {expected}; {len(api_ads.SORTS)} sorts, "
          f"avg {total_ru / len(api_ads.SORTS):.1f} RU, max {worst[0]:.1f} RU ({worst[1]})")


def flip(target):
    text = PARAMS_FILE.read_text(encoding="utf-8")
    new = re.sub(r"param activeDatabase = '[^']*'", f"param activeDatabase = '{target}'", text)
    PARAMS_FILE.write_text(new, encoding="utf-8")
    print(f"infra/main.bicepparam: activeDatabase = {target}; deploying infra/main.bicep")
    az("deployment", "group", "create", "-g", RESOURCE_GROUP, "-n", "main",
       "-f", str(REPO / "infra" / "main.bicep"), "-p", str(PARAMS_FILE), parse=False)
    print(f"live: {live_database()}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["status", "provision", "flip"])
    ap.add_argument("--target", choices=COLORS, help="override the database to provision or make live")
    args = ap.parse_args()
    live = live_database()
    target = args.target or inactive(live)
    if args.command == "status":
        print(f"live: {live}; inactive: {inactive(live)}")
    elif args.command == "provision":
        if target == live:
            sys.exit(f"{target} is live; provision the inactive database and flip instead")
        provision(target)
        print(f"{target} is ready; make it live with: tools/cloud/provision.py flip")
    else:
        if target == live:
            sys.exit(f"{target} is already live")
        flip(target)


if __name__ == "__main__":
    main()
