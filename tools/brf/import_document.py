#!/usr/bin/env python3
"""Copy an original BRF document into raw/ and write its metadata.yaml.

Usage:
    import_document.py <orgnr> annual_review <fiscal year> FILE.pdf [--url URL] [--name NAME.pdf]
    import_document.py <orgnr> protocol <meeting year> FILE.pdf [--url URL] [--name NAME.pdf]

The file is copied unchanged (keeping its name unless --name is given) to
raw/annual_reviews/brfs/<orgnr>/<year>/ or raw/protocols/brfs/<orgnr>/<year>/.
An existing folder with a different file is left alone and reported.
"""
import argparse
import hashlib
import os
import shutil
import sys

import yaml

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), *[".."] * 2))
KINDS = {
    "annual_review": ("annual_reviews", "årsredovisning", "fiscal_year"),
    "protocol": ("protocols", "protokoll ordinarie föreningsstämma", "meeting_year"),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("orgnr")
    ap.add_argument("kind", choices=KINDS)
    ap.add_argument("year", type=int)
    ap.add_argument("pdf")
    ap.add_argument("--url", default="")
    ap.add_argument("--name", help="file name to store it under (default: the source file's name)")
    args = ap.parse_args()

    sub, document, year_key = KINDS[args.kind]
    folder = os.path.join(REPO, "raw", sub, "brfs", args.orgnr, str(args.year))
    name = args.name or os.path.basename(args.pdf)
    dst = os.path.join(folder, name)
    sha = hashlib.sha256(open(args.pdf, "rb").read()).hexdigest()

    meta_path = os.path.join(folder, "metadata.yaml")
    if os.path.exists(meta_path):
        old = yaml.safe_load(open(meta_path, encoding="utf-8")) or {}
        if old.get("sha256") == sha:
            print(f"already imported: {os.path.relpath(dst, REPO)}")
            return 0
        if old.get("file"):
            print(f"! {os.path.relpath(folder, REPO)} already holds {old['file']}; not overwritten", file=sys.stderr)
            return 1

    os.makedirs(folder, exist_ok=True)
    if os.path.abspath(args.pdf) != os.path.abspath(dst):
        shutil.copy2(args.pdf, dst)
    meta = {"file": name, "document": document, year_key: args.year, "source": {"url": args.url}, "sha256": sha}
    with open(meta_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(meta, f, allow_unicode=True, sort_keys=False)
    print(f"imported {os.path.relpath(dst, REPO)}")
    if args.kind == "annual_review":
        import build_coverage                      # keep annual_reviews/brfs/coverage.md in step
        out, n, held, ok, total = build_coverage.build()
        print(f"{os.path.relpath(out, REPO)} updated: {held}/{total} reports held, {ok}/{total} verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
