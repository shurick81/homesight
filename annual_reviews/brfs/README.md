# BRF data model

How a BRF (bostadsrättsförening) is stored, what each number means, and how the numbers must agree with
each other, both inside one year and from one year to the next.

The aim is a **cash view** of the association rather than the accountant's view: what money came in, what
went out, and what happened to the bank balance and the loans. Non-cash bookkeeping items (avskrivningar,
årets resultat, bokslutsdispositioner) are deliberately not stored.

## Two layers: documents and values

```
raw/annual_reviews/brfs/<orgnr>/<year>/          original documents issued by the BRF, untouched (local only:
                                                 git tracks metadata.yaml with source and sha256)
    metadata.yaml                                   file, document, fiscal_year, source.url, sha256
    <report>.pdf                                    årsredovisning / stämmohandlingar for fiscal <year>
raw/protocols/brfs/<orgnr>/<meeting year>/       stämma protocols (meeting_year instead of fiscal_year)
raw/maintenance_plans/brfs/<orgnr>/<plan>/       underhållsplaner (same metadata.yaml convention)

annual_reviews/brfs/                             values extracted from the documents
    event_categories.csv                            shared category trees (id,name,parent),
    state_categories.csv                            identical for every BRF so that BRFs
    general_categories.csv                          can be compared
    <orgnr>/financial_events.csv                    flows during the year (SEK)
    <orgnr>/financial_states.csv                    balances at year end (SEK)
    <orgnr>/general_states.csv                      text facts (board, auditors, stämma)
    <orgnr>/property_states.csv                     counts and areas of the property (apartments, m²)
    <orgnr>/checked_by_eye.csv                      values confirmed on the page image where OCR cannot read them
    <orgnr>/restatements.csv                        opening balances a year's own report restates (cited), used by the chain checks
    <orgnr>/pipe_works.csv                          every mention of work on the stammar (done or planned), cited
    index.csv                                       one row per BRF: size, coverage, stambyte summary (generated)
    <orgnr>/profile.yaml                            name, stable facts, major works, stambyte assessment, data notes
```

`profile.yaml` is the one file that holds **derived judgement** (when the next stambyte is likely, by the
rubric below), kept apart from the extracted values. Facts copied from a report name their `source` (file
and page); the `stambyte` assessment is dated with `as_of`; `verified_through` is the last fiscal year that
passes the verify-brf-data checks; `data_notes` records caveats about the extracted values (merged lines,
restatements, unreadable fonts). The verification scripts do not read it; `build_index.py` does.

`index.csv` is generated from the profiles and the extracted tables by `tools/brf/build_index.py`; do not
edit it by hand.

A BRF is identified by its **organisationsnummer** (`712400-1467` is Sjötungan). `<year>` is always the
**fiscal year the document covers**, not the year it was published (the 2024 report sits in the
stämmohandlingar of 2025). A broken fiscal year is filed under the year it **starts** (Siken's 2024-05-01 –
2025-04-30 report is `2024`), as allabrf.se and hittabrf.se label it; the exact dates are in general_states
category 0.

Every extracted value carries its provenance: `file,page,x,y,width,height` point at the exact spot in the
PDF (1-based page index in the file; box in **PDF points** from the top-left, so a viewer rendering at
scale 1.5 multiplies by 1.5). A value without a source is not allowed, so any number in an analysis can be
clicked back to the document it came from.

**One document per year.** A value for year *Y* is always taken from the report *for* year *Y*, never from
the comparison column of the next report: reports restate earlier years (e.g. when the layout of the
income statement changes), and the chain of identities below only holds for the figures as originally
reported.

## The three tables

All three share the same shape: `year, category_id, <amount|value>, file, page, x, y, width, height`.
`(year, category_id)` is unique per BRF.

### financial_events: what flowed during the year

Integer SEK. **Sign = direction of money for the association**: income and cash in are positive, costs
and cash out are negative (even where a note prints the cost as a positive number).

Top-level categories (no `parent`) are the cash-flow lines. Together they explain the whole year:

| group | ids | source in the report |
|---|---|---|
| Income | 0 Nettoomsättning, 4 Övriga intäkter | Resultaträkning |
| Operating costs | 1 Driftkostnader, 16 Planerat underhåll (only if a separate RR line), 5 Övriga externa kostnader, 6 Personalkostnader, 56 Skatt | Resultaträkning |
| Interest | 7 Räntekostnader (paid), 15 Erhållen ränta | Kassaflödesanalys (else RR) |
| Working capital | 13 Kundfordringar, 14 Övriga fordringar, 11 Leverantörsskulder, 12 Övriga kortfristiga skulder, *or* the aggregates 9 / 10 | Kassaflödesanalys |
| Investments | 8 Investeringar | Kassaflödesanalys |
| Financing | 3 Kassaflöde från finansieringsverksamheten | Kassaflödesanalys |

Why working capital is needed: the RR lines are *accrual* amounts (a fee invoiced in December counts even if
paid in January). The working-capital changes turn them into the money that actually moved. RR +
working capital + interest + investments + financing = change in cash.

Child categories (with `parent`) break a top-level line down and are **not** added again to the cash sum:

```
0 Nettoomsättning                 = 17 Årsavgifter + 18 Hyror + 19 Övriga + 20 Avgifts- och hyresbortfall
1 Driftkostnader                  = 2 Fastighetsskötsel och lokalvård + 21..32
    2 Fastighetsskötsel o lokalvård = 50 Fastighetsskötsel + 51 Städning + 52 Hiss + 53 Besiktning
                                      + 54 Trädgård + 55 Snöröjning
5 Övriga externa kostnader        = 33..43
6 Personalkostnader               = 44..49
```

Children are only recorded when the notes print them, so a parent may have no children, or a small
"unassigned" remainder (a note line with no category). That remainder is reported, not hidden.

Mutually exclusive choices (pick one so nothing is counted twice):

- 9 *or* 13+14 (receivables): 9 only when the KF gives a single aggregated line.
- 10 *or* 11+12 (short-term liabilities): same rule.
- 7 as a net figure *or* 7 and 15 separately.

### financial_states: balances at 31 December

Integer SEK, as printed (normally positive).

| id | name | role |
|---|---|---|
| 2 | Kassa och bank | **liquidity** |
| 3 | Avräkningskonto HSB | **liquidity** (only if listed separately) |
| 4 | Placeringskonto HSB | **liquidity** (only if listed separately) |
| 5 | Kortfristiga placeringar | **liquidity** (only if listed separately) |
| 0 | Övriga skulder till kreditinstitut | **debt**: the short-term part of the loans |
| 1 | Skulder till kreditinstitut | **debt**: the long-term part of the loans |
| 6 | Eget kapital | context |
| 7 | Summa tillgångar | context |

Define for a year *Y*:

- `L(Y) = state 2 + 3 + 4 + 5` (liquid funds)
- `D(Y) = state 0 + 1` (loans from credit institutions)

### property_states: what the association owns

Integer counts and m² at year end, as printed in the förvaltningsberättelse: apartments (bostadsrätt /
hyresrätt), lokaler, garage and parking places, förråd, and their areas. When the report prints a total
area, `Total yta = Bostadsrättsyta + Hyresrättsyta + Lokalyta + Förrådsyta` (checked). These are the
denominators for per-apartment and per-m² measures (årsavgift per m², debt per m² …), so the basis
matters: reports sometimes change what they count (Sjötungan's 2021 report counts 104 lokaler / 2 186 m²,
other years 15 / 710 m²; its prose reports to 2019 give 55 548 m² "bostadsyta", the tables from 2020
give 50 388 + 567 m²). Changes between years are listed by `validate.py` so they are seen, not averaged.

### general_states: text facts

One text `value` per `(year, category)`: fiscal-year dates, ordförande, ledamöter, suppleanter, valberedning,
auditors, who signed the audit, members present and proxies at the stämma. Several people are separated by `;`;
mid-year changes are marked `Name (t.o.m. YYYY-MM-DD)` / `(fr.o.m. …)`. Categories 6–7 (attendance) use the
**year the stämma was held** and come from the stämma protocol, not the annual report.

Building year: `11 Byggnadsår` as printed (`1964 och 1966`, `1966-68`; HSB reports have it in the
"Nybyggnadsår byggnad" table) and `12 Värdeår` (the tax assessment's year, which many non-HSB reports give
instead). Stored for the report year it was read from. The value stays as printed (it is the evidence);
`index.csv` turns it into numbers for analysis: `built_from` and `built_to` are the first and last year of the
latest Byggnadsår, else Värdeår (`1966-68` gives 1966 and 1968), and `built_source` says which. The profile
holds the same as `built_from`, `built_to` and `built_basis` (`byggnadsår` or `värdeår`).

Land: `13 Markinnehav` says whether the association owns the land under its buildings or leases it, stored as
printed, like the building year. Owning (`äganderätt`, also `friköpt`) means no ground rent; a `tomträtt`
(site leasehold from the kommun) means a yearly `tomträttsavgäld` (financial_events `57`, under
Driftkostnader) that the kommun renegotiates every 10–20 years, often with a large increase; `arrende` is a
private land lease. Take, in this order: an explicit statement ("Marken innehas med äganderätt", "Föreningen
äger fastigheten …", "Fastigheten innehas med tomträtt", "friköpt"); else, in the note on Byggnader och mark,
the land line with an acquisition value ("Ingående anskaffningsvärde mark"): only an owner carries land as an
asset; a leaseholder has no land line and pays tomträttsavgäld. Ignore the generic glossary pages that HSB
appends ("En del föreningar äger inte sin mark utan har ett tomträttsavtal …"): they are not about this
association. `index.csv` normalises the value to `land_tenure` (`äganderätt`, `tomträtt` or `arrende`).

## Stambyte: evidence and assessment

The stambyte (replacing drains, water pipes and usually bathrooms) is the largest single cost in a BRF's
life, so it is stored in two layers, like the numbers: what the documents say, and what follows from them.

### pipe_works.csv: what the documents say

One row per statement in a document about work on the pipes, cited like every other value.

`year_from, year_to, status, kind, scope, cost_kr, text, file, page, x, y, width, height`

- `status`, as stated in **that** document: `done`, `in_progress`, `decided` (stämma decision, contract or
  financing in place), `planned` (in the underhållsplan or the 5-year list, not decided).
- `kind`: `stambyte` (whole association), `partial_stambyte` (some buildings or parts),
  `reference_stambyte` (pilot stammar before a full one), `relining`, `stamspolning` (flushing, replaces
  nothing), `ground_pipes` (markliggande rör), `individual_bathrooms` (members renovating one by one).
- `scope`: `;`-separated, from `avlopp`, `tappvatten`, `våtrum`, `golvbrunnar`; empty when not stated.
- `cost_kr`: only when the document prints a cost for this work.
- `text`: the line as printed; the box points at it.

### profile.yaml `stambyte`: what follows

```yaml
stambyte:
  as_of: 2026-10-11
  last:  {years, kind, scope, confidence, basis}          # years: null when the pipes are original
  next:  {from, to, status, scope, confidence, basis}     # status: in_progress | decided | planned | estimated
  report: |                                                # a few sentences: what was done, what it cost, what is coming
```

Confidence rubric:

| | high | medium | low |
|---|---|---|---|
| last | a document gives the year of a full stambyte (or the building is too new to have had one) | year given but partial, or scope unclear; or "none" inferred from a works list that is silent | no document mentions it |
| next | `in_progress` or `decided` | `planned` in a document | `estimated`: last stambyte or build year + 40–50 years |

`build_index.py` copies `last.years`, `next.from–to`, `next.status` and `next.confidence` into `index.csv`.

### Stamspolning regularity (derived, in index.csv)

Flushing the drains (stamspolning) every 3–5 years keeps old pipes working; whether a BRF does it is a sign
of how it maintains the stammar. `build_index.py` derives three columns from the `stamspolning` rows of
`pipe_works.csv` (today's year as reference):

- `stamspolning_done`: the years done; `stamspolning_next`: planned years from this year on.
- `stamspolning_regular`:
  - `regular`: at least two occasions (done, or done plus planned) with no gap above 6 years, and the last
    one done within 6 years or another one planned;
  - `lapsed`: done at least twice, but not in the last 6 years and nothing planned;
  - `irregular`: done at least twice with a gap above 6 years, the last within 6 years;
  - `once`, `planned only`, `none recorded`.

`none recorded` means the imported reports are silent, not that it is never done: flushing is often booked
under repairs without a mention. It weighs more for a report that has a works list (HSB, Simpleko) than
for one that has none (the Fastum template).

## How the numbers connect

### Inside one year

1. **Parent = Σ children** for every category tree above (±1–2 kr rounding allowed).
2. **Summa tillgångar = Summa eget kapital och skulder** in the balance sheet (checked by hand at extraction;
   total liabilities are not stored).

### Across years: this is what makes the series verifiable

3. **Cash identity.** The top-level events of a year explain the change in liquid funds exactly:

   ```
   Σ top-level financial_events(Y)  =  L(Y) − L(Y−1)
   ```

   The opening balance of year *Y* is the closing balance of year *Y−1*, taken from *Y−1*'s own report. If
   a figure was mistyped, a working-capital line was missed, or a category was double-counted, the
   identity breaks for that year and only that year. A chain of years that all balance is therefore strong
   evidence that every year was extracted correctly.

4. **Debt identity.** Net borrowing in the cash-flow statement equals the change in loans:

   ```
   event 3 (financing)(Y)  =  D(Y) − D(Y−1)
   ```

   This holds when financing consists only of new loans and amortisation. A gap means the financing
   section contains something else, which is worth knowing about in its own right: for Sjötungan
   2012–2015 the gap is exactly the "Inbetalda insatser och upplåtelseavgifter" line (e.g. 1 975 000
   in 2015), money from members, not from the bank.

5. **Equity** cannot be chained from the stored numbers, because årets resultat depends on depreciation,
   which is not stored. `Eget kapital` is context only.

## Verification

Two scripts in `tools/brf/` turn the rules above into checks; the verify-brf-data skill
(`.claude/skills/verify-brf-data/`) describes how to run them and resolve failures:

| check | script | what fails it |
|---|---|---|
| every value is printed where it is cited | `verify_sources.py` | wrong number, wrong page, comparison column (COLUMN), another year's report (WRONG-DOC) |
| the year's cash flow and closing liquidity are printed in its report (±2 kr) | `verify_sources.py` | warning (NOT PRINTED) |
| next year's report repeats the value | `verify_sources.py --crosscheck` | informational: differences are restatements |
| parent = Σ children | `validate.py` | INFO with the unassigned remainder |
| cash identity (3) | `validate.py` | ERROR when off by more than 25 kr |
| debt identity (4) | `validate.py` | INFO with the non-loan financing amount |
| total area = its parts | `validate.py` | ERROR (AREA) |
| property counts/areas change between years | `validate.py` | INFO (PROP) |

Scanned reports are OCR'd (cached in `.cache/pdfwords/`, keyed by the PDF's sha256). Where OCR cannot read a value at all
(handwritten signatures), it is checked on the page image and listed with a note in
`<orgnr>/checked_by_eye.csv`.

## Verification status

### Sjötungan (712400-1467)

| years | sources | cash identity (3) | debt identity (4) |
|---|---|---|---|
| 2016–2025 (2015 as opening balance) | all values verified against the imported PDFs; 2 by eye | holds (±1 kr) | holds exactly |
| 2012–2014 | PDFs imported, not verified | holds (±20 kr) | gap = inbetalda insatser och upplåtelseavgifter |
| 2003–2011 | PDFs imported, not verified | broken (gaps up to ±18 MSEK) | broken in most years |

Only 2015–2025 is in scope for analysis. 2004–2011 would need re-extraction (working-capital and some financing lines are missing, and some states come
from a later report's comparison column).

### Bergfoten (712400-0089)

| years | sources | cash identity (3) | debt identity (4) |
|---|---|---|---|
| 2024–2025 (2023 as opening balance) | all values verified against the imported PDFs | holds (±1 kr) | holds exactly |

### Siken (712400-1426)

Fiscal year 1 May – 30 April.

| years | sources | cash identity (3) | debt identity (4) |
|---|---|---|---|
| 2024 (2024-05-01 – 2025-04-30) | all values verified against the imported PDF | holds exactly against the report's own opening balance (6 081 232 → 7 744 498; the opening matches the comparison column of BR + note 10); not chained, no 2023/24 report imported | holds against the BR comparison column (52 990 230 both years, financing 0) |

## Comparing BRFs

Because the category ids are shared, any analysis written for one BRF works for any other.
`tools/brf/compare.py` groups the categories into comparable buckets and prints kr per m² or shares; the
compare-brfs skill (`.claude/skills/compare-brfs/`) documents the buckets, the area basis and the pitfalls. Per-flat and
per-m² measures (e.g. årsavgift per m², debt per m²) use `property_states`.
