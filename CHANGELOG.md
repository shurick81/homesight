# Changelog

All notable changes to the homesight API (https://homesight-api.azurewebsites.net) are recorded here. The
format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The deployed version is `API_VERSION` in [api/function_app.py](api/function_app.py), also the `info.version`
of the spec; `GET /health` returns it. A new endpoint or field is a minor version; a fix or a change in what
data is served is a patch. Loading newly extracted annual reports (a new BRF or year, no change to the spec)
is therefore a **patch**: bump `API_VERSION`, add a release entry here with the BRFs and years under **Data**,
deploy the code (README), then `provision` + `flip`. Release once per request, not per BRF or per report: finish the whole
request (every BRF and year asked for), let the user verify the complete result, then commit, deploy and flip.

## [Unreleased]

## [0.11.4] - 2026-10-15

### Data
- Gösen 2011–2016, 2022 and 2025, Pluto FY 2017/18–2019/20, Siken FY 2018/19, 2019/20 and 2025/26, Laken 2019 and Solhöjden
  2017 (July 2017–December 2018), 2019 and 2020 extracted and verified. Ten Tyresö BRFs now have: Sjötungan, Gäddan,
  Björkbacken 15 years; Gösen 14; Bergfoten 12; Pluto and Siken 8; Solhöjden 5; Laken 3; Almandinen 2.
- Gösen 2014 and Solhöjden 2019–2020 have one-off items (a settlement, a tenant compensation, a land sale) that no category
  holds; those years are `check_failed` with a note.

### Changed
- `tools/brf/validate.py` checks a year whose previous year is not stored when its opening is restated, and skips the debt
  identity without a previous year.

## [0.11.3] - 2026-10-14

### Data
- Bergfoten 2012, 2013, 2015, 2017 and 2018 extracted and verified (from HSB's archive folder): 12 fiscal years (2014 and 2016
  not found).

## [0.11.2] - 2026-10-14

### Data
- Björkbacken 2010–2012 and 2014–2019 extracted and verified from the reports its own site lists (2020 completed): 15 fiscal
  years 2010–2025 (the books changed from May–April to calendar year in 2013 and no report covers May–December 2013). Reports
  without a cash flow statement now show `cash_not_stored` in `warnings`.

## [0.11.1] - 2026-10-14

### Data
- Gäddan 2011–2015 (from HSB's archive) and Sjötungan 2011–2015 verified: both now have 15 verified fiscal years
  (2011–2025). Sjötungan 2011–2012 were rebuilt from the year's own report; 2012 is `check_failed` (the report's own
  cash flow is 906 kr off its balance sheet).

## [0.11.0] - 2026-10-14

### Added
- A quality label on every fiscal year of the annual-report data: `quality` (`verified`,
  `verified_with_caveats`, `check_failed`, `unverified`) and `warnings` (`{code, message}`) in
  `GET /brfs/{orgnr}/financials`, saying which checks could not run or failed: `cash_check_failed`,
  `cash_not_stored` (no cash flow statement), `no_opening_balance` (previous report not held), `checked_by_eye`,
  `scanned` (values read by OCR), `note` (a caveat written in the BRF's profile), `unverified`. Each of `values`
  also says whether it was `checked_by_eye`. Listings and `/brfs` rows carry the latest year's label as
  `brf_report_quality` and `brf_report_warnings`.

### Fixed
- Sjötungan 2003–2015 were served as `verified: true`; only 2016–2025 are verified (profile `verified_from`).
  They are now `unverified`, and 2004–2011 also `cash_check_failed`.

## [0.10.1] - 2026-10-14

### Data
- 20 annual reports from allabrf.se extracted and verified (`tools/brf/allabrf.py`): Gösen 2021, 2023;
  Laken 2022; Soluret 2021, 2023; Trädgården 278 FY2021; Peppargränd 1 2024; Rödingen 2024; Almandinen 2024;
  and the new BRFs Solhöjden 2023–2024, Pigghajen 2021, 2024, Tyresö Trädgårdar 2022, Steget FY2021–FY2022,
  Kvarteret Skogsbacken 2021–2022 and Sander 2023–2024 (a new build).

## [0.10.0] - 2026-10-13

### Added
- `id` and `created_at` on every listing, as in the Real Estate Data API (missed in the migration): `id` is
  the row's position in the import, `created_at` when the row was loaded. Rows with an equal sort value are
  now ordered by `id` descending, as there, instead of by `listing_id`, so tied rows come in the same order.
- The spec documents every nearest-school field (`school_1..5_*`) and `avg_school_socio_index*`, which the
  0.9.0 spec left out.

### Changed
- `tools/cloud/parity.py` also reports fields the data API serves that are missing here.

## [0.9.0] - 2026-10-13

### Added
- Schools, from Skolverket's *Index på skolenhetsnivå* 2023–2026 (`schools/`): on every listing the five
  nearest school units (`school_1..5_*`: name, type, geodesic distance, index per edition, ineligible share)
  and the averages over the nearest 1, 3 and 5 with a 2029 projection (`avg_school_socio_index*`); on every
  område, kommun and city the mean, median, per-edition series, trends and projection, copied down to
  listings and `/brfs` rows. The `min_school_index*` filters and `school_*` sorts now work.

### Known differences from the data API
- A projection or trend on an exact rounding tie can differ by 0.1 (about 2% of listing projections). Where
  two school units share one address, the fifth-nearest can be the other one (12 listings).

## [0.8.0] - 2026-10-12

### Changed
- **Breaking:** `GET /brfs` follows the Real Estate Data API's contract: one row per BRF with an apartment for
  sale (about 3 100), filters (`brf_name_search`, `kommun`, `omrade`, `min_brf_units`, ...), 32 sorts and at
  most 10 rows per request, instead of the list of BRFs with extracted reports. Those BRFs are now found with
  `has_report=true` and are listed even when nothing is for sale.
- **Breaking:** `GET /brfs/{orgnr}` returns the BRF's `/brfs` row, with the former summary under `report`
  (null for a BRF without extracted reports). Every BRF in `/brfs` can be read this way.

### Added
- `has_report` filter on `/brfs` (homesight only), and the verified layer (`brf_report_*`, `brf_land_tenure`,
  `brf_stambyte_*`) on `/brfs` rows.

### Known differences from the data API
- About 55 more BRFs (the extra listing links). For a BRF whose listings span several områden, `area_name`
  and the benchmarks come from the område with most of its listings (the data repo takes an arbitrary
  listing). The first page of every sort is otherwise identical.

## [0.7.0] - 2026-10-12

### Added
- `GET /sales/stats/avg-price-per-sqm` (`?year=`, `?rooms=`): average sold kr/m², with the data repo's
  contract. Every year/rooms combination is precomputed, so a request is one point read; identical to the
  data API for all 424 combinations.
- `GET /llms.txt`: a short Markdown guide for LLM agents (coverage, how to query, caveats).

### Fixed
- `GET /sales/stats/avg-price-per-sqm` echoes `rooms` as a number with a decimal (`2.0`), as the data API.
- The first 0.7.0 deploy added a catch-all route for `GET /` that shadowed every single-segment endpoint
  (`/health`, `/advertisements`, `/kommuns`, `/brfs`, `/openapi.json`, ...): they answered 404 for a few
  minutes until it was removed. `GET /` discovery is therefore not available.

## [0.6.0] - 2026-10-12

### Added
- `GET /cities`, `GET /kommuns` (with `price_stats`), `GET /areas` (`?kommun=`, `?name_search=`), with the
  data repo's contract: listing counts, asking and sold kr/m² per type of home, avgift and loan per m²,
  housing-cooperative stock and turnover, per-year sold history with trends and forecasts, and each
  parent's figures copied down.
- On every listing in `/advertisements`: the område, kommun and city benchmarks (`area_*`, `kommun_*`,
  `city_*`: same-type avg/median kr/m², sold price, avgift and turnover summaries).

### Changed
- Trends and forecasts are computed with Postgres' own regression (Youngs-Cramer) and exact averages, so a
  value that falls on a rounding tie matches the data repo more often; a few still differ by 1.

### Known differences from the data API
- The school block (`school_*`, `*_school_socio_index*`) is not migrated: NULL.
- About 150 more listings are linked to their BRF here, which also changes the housing-cooperative stock
  fields (`units_in_scope_br`, `pct_for_sale_br`, `turnover_br_*`, `*_loan_per_m2_br`) of their område,
  kommun and city.

## [0.5.1] - 2026-10-12

### Changed
- `/advertisements` serves every listing of the Booli snapshot: Stockholm, Nacka, Solna and Tyresö
  (11 139 listings), instead of only the listings of the BRFs with extracted reports.
- A listing is also linked to its BRF by the Booli apartment id in its URL and by its address with the
  entrance letter (about 150 more links than the data repo; never a different BRF).
- `street_name` has no trailing space or stray digit.

## [0.5.0] - 2026-10-12

### Added
- `GET /brfs/{orgnr}/financials` (`?include=values`): every verified fiscal year of a BRF: income, costs,
  interest, loans, cash, per-m² figures and ratios, and with `values` where each number is printed in the
  report.
- On listings of BRFs with extracted reports: `brf_report_*` (latest verified year), `brf_land_tenure` and
  the stambyte assessment.

## [0.4.0] - 2026-10-11

### Added
- `land_tenure` on BRF summaries (`äganderätt`, `tomträtt`, `arrende`), from the annual report.

## [0.3.0] - 2026-10-11

### Added
- `GET /advertisements`, with the data repo's contract (parameters, 49 sorts, filters, radius search,
  errors, rows) for the listings of the BRFs with extracted reports.

### Changed
- Read model: one Cosmos DB container per resource, each document a finished response row; blue/green
  databases (`homesight_blue`, `homesight_green`), switched by `tools/cloud/provision.py`.

## [0.2.0] - 2026-10-11

### Changed
- Every endpoint is public: no function key.

## [0.1.0] - 2026-10-11

### Added
- First deploy on Azure Functions (Flex Consumption) and Cosmos DB (serverless): `GET /health`,
  `GET /openapi.json`, `GET /brfs`, `GET /brfs/{orgnr}` (BRF summaries from the extracted annual reports).
  Paths without the `/api` prefix.

## Data

- 2026-10-15 (0.11.4): 15-year request for ten Tyresö BRFs completed as far as reports are found (see the 0.11.4 entry).
- 2026-10-14 (0.11.3): Bergfoten 2012, 2013, 2015, 2017, 2018 extracted and verified (12 years).
- 2026-10-14 (0.11.2): Björkbacken 2010–2012 and 2014–2020 extracted and verified (15 years); `tools/brf/source_gap.py` lists
  documents on known source pages that are not imported yet; Gösen 2017–2018 and Solhöjden 2017–2020 imported, not extracted.
- 2026-10-14 (0.11.1): Gäddan and Sjötungan 2011–2015 extracted and verified (15 years each); Björkbacken, Gösen,
  Solhöjden, Laken, Siken, Pluto, Bergfoten and Almandinen cannot reach 15 years from public sources found so far.
- 2026-10-14 (0.10.1): 20 reports from allabrf.se extracted and verified, six of them for BRFs new to the data
  (Solhöjden, Pigghajen, Tyresö Trädgårdar, Steget, Kvarteret Skogsbacken, Sander); Steget and Skogsbacken print
  no cash flow statement, so their cash is not stored.
- 2026-10-14: HSB Brf Laken 2020 (and 2019 closing balances) extracted and verified; no later report is public.
- 2026-10-13: Brf Fyrhöjden (Kärrtorp, Stockholm; tomträtt) 2021–2025 extracted and verified (2019–2020 held for
  closing balances); `tools/brf/pdfwords.py` also ignores the Visma e-signing stamp.
- 2026-10-13: Centrala Tyresö: Tyresö View 1, Diamanten, Björkbacken, Delfinen, Skogslunden and Safiren
  (2020–2025), Tornsvalan (2020–2022, 2024), Almandinen (2023) and Peppargränd 1 (2021) extracted and verified;
  older reports of Gösen, Laken, Soluret, Siklöjan, Strömstaren, Trädgården 278 and Kringlan 2 (2019–2025)
  downloaded, not yet extracted. `tools/brf/pdfwords.py` OCRs scans that carry a GetAccept stamp and honours
  `ocr: force` in a document's `metadata.yaml`.
- 2026-10-12: Passadvinden 2025 (from Booli's BRF page) and 2020 (svenskbrf.se, scanned) extracted and verified,
  each unchained; its 2020 sewer renovation under the house recorded. Every tracked BRF now has verified data
  (53 of 70 reports of the last five years held, all verified).
- 2026-10-12: Silvertärnan 2025 (published on the BRF's site 2026-05-08) extracted and verified, chained from
  2024; every held report of the last five years remains verified (52 of 70).
- 2026-10-12: Siken 2020–2023, Karlsön 2021–2023, Laxtrappan 2021–2022 and Silvertärnan 2021–2022
  extracted and verified; every held report of the last five years is now verified (see
  [annual_reviews/brfs/coverage.md](annual_reviews/brfs/coverage.md)).
- 2026-10-11 – 2026-10-12: annual reports of Rödingen, Stortorpet 1, Odlingslotten and Karlsön added;
  every BRF's land tenure extracted.
