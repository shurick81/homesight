# Market data (Booli)

What the market says about each BRF: completed sales, apartments for sale now, and the apartment register,
from [Booli](https://www.booli.se). Kept apart from `annual_reviews/`: those are the BRF's own verified
accounts; these are listing data as Booli publishes them, unverified.

```
booli/*.csv.gz               the whole snapshot, every kommun the data repo covers (Stockholm, Nacka, Solna,
                             Tyresö): listings, sales, BRF details, apartment registers, allabrf links, areas;
                             gzipped copies of the data repo's files, read by tools/readmodel/advertisements.py
sources.yaml                 where and when each input was captured (data repo commit, snapshot dates)
brfs/booli_brfs.csv          Booli's facts per BRF: units, area, plot ownership, total loan, addresses
brfs/<orgnr>/sales.csv       completed sales linked to the BRF on Booli, oldest first
brfs/<orgnr>/listings.csv    apartments for sale at the snapshot date
brfs/<orgnr>/apartments.csv  the BRF's apartment register on Booli (address, lgh nr, rooms, m², floor)
```

Every row keeps its Booli URL (`booli_url`, `source_url`, `url`) as provenance. Amounts are SEK, areas m².

**Source.** For now the files are copied from the `data` repo's Booli cache by
`tools/booli/import_from_data_repo.py`, for the BRFs in `annual_reviews/brfs/index.csv` (matched by orgnr).
Re-running replaces the files, so a listing that is no longer for sale disappears; `sources.yaml` records
the snapshot of each input. The scrapers themselves have not moved here yet.

**Linking to the BRF.**
- Sales: Booli links each sale to its BRF; that link is used as is. A few sales have loose addresses on
  Booli (no house number, a `MP` suffix) but are real apartments of the BRF.
- Listings: Booli does not link a listing to its BRF, so it is attached as the data repo's import does it.
  `linked_by` says how: `residence_id` (the URL names an apartment in the register), `address` (the full
  address is in the register), or `address_without_letter` (the address without its entrance letter maps
  to this BRF only).

**Caveats.** `kr_per_m2` is Booli's figure. `monthly_fee_kr` is the avgift at the time of the sale or
listing and can be missing (Booli has no fee on some recent sales). A listing with `upcoming_sale` in its
attributes is announced but not yet open for bids, so its `listed_date` can be old.
