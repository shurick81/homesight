---
name: extract-brf-annual-review
description: Import and extract structured data (income statement, cash flow, balance sheet, note breakdowns, property counts/areas, board/auditors) from a Swedish bostadsrättsförening (BRF) annual report PDF into the CSVs in annual_reviews/brfs/<orgnr>/. Use when asked to import, extract, parse or structure a BRF årsredovisning / stämmohandlingar. After extracting, verify with the verify-brf-data skill.
---

# Extract BRF annual review data

Turns one BRF annual report (årsredovisning, often bundled inside stämmohandlingar) into rows in
four CSVs. Every row cites where the value came from (file, page, bounding box) so it can be checked.

This is step 1 of 3:

1. **extract-brf-annual-review** (this skill): import the PDF, extract the values.
2. **verify-brf-data**: check every value against its PDF and the years against each other. An extraction is
   not finished until that skill's checks pass.
3. **compare-brfs**: compare BRFs per m² or as shares.

The data model and the identities between the numbers are described in `annual_reviews/brfs/README.md`.

Reference examples (extracted and verified, use them when in doubt): Sjötungan `712400-1467` 2015–2025
(scanned and text reports, two layouts) and Bergfoten `712400-0089` 2024–2025 (HSB template with
liquidity on the avräkningskonto).

## Layout

```
raw/annual_reviews/brfs/<orgnr>/<year>/metadata.yaml   # file, document, fiscal_year, source.url, sha256
raw/annual_reviews/brfs/<orgnr>/<year>/<report>.pdf    # the original file, never modified; local only, not in git
raw/protocols/brfs/<orgnr>/<meeting year>/            # stämma protocols, same metadata (meeting_year)
annual_reviews/brfs/{event,state,general,property}_categories.csv   # shared category lists (id,name,parent)
annual_reviews/brfs/<orgnr>/financial_events.csv   # year,category_id,amount,file,page,x,y,width,height
annual_reviews/brfs/<orgnr>/financial_states.csv   # year,category_id,amount,file,page,x,y,width,height
annual_reviews/brfs/<orgnr>/general_states.csv     # year,category_id,value,file,page,x,y,width,height
annual_reviews/brfs/<orgnr>/property_states.csv    # year,category_id,amount,file,page,x,y,width,height
annual_reviews/brfs/<orgnr>/checked_by_eye.csv     # table,year,category_id,note (see verify-brf-data)
```

`<year>` is the fiscal year the report covers (the 2024 report is in the 2025 stämmohandlingar, so it goes in
`2024/`). Keep the original file name: the CSVs cite it. If `metadata.yaml` has no `file:`, use the only PDF in
the folder.

**Importing a document:** use `tools/brf/import_document.py`, which copies the PDF unchanged into its year folder
and writes `metadata.yaml`:

```
python3 tools/brf/import_document.py <orgnr> annual_review <fiscal year> FILE.pdf --url URL [--name NAME.pdf]
python3 tools/brf/import_document.py <orgnr> protocol <meeting year> FILE.pdf --url URL
```

```yaml
file: stamma2025.pdf
document: årsredovisning          # or: protokoll ordinarie föreningsstämma
fiscal_year: 2024                 # protocols: meeting_year: 2025
source:
  url: https://…/stamma2025.pdf   # where it was published (empty if unknown)
sha256: …                         # of the file, so later copies can be compared
```

Check the fiscal year inside the document, not from the file name (`arsredovisning2013.pdf` can be the 2012
report). A new BRF also gets a row in `annual_reviews/brfs/tracked.csv` (orgnr, name, municipality). Every import
rewrites `annual_reviews/brfs/coverage.md` (the BRF × year table of held / extracted / verified reports), and so
does `tools/brf/build_index.py` after extraction and verification.

Import the report for the year **before** the first year you extract too: its closing balances are
the opening balances that the cash check needs (only its financial_states are needed).

When asked for more years of a BRF, or before reporting a report as missing, run `python3 tools/brf/source_gap.py <orgnr>`:
it lists every document its known source pages offer that is not imported yet. Import everything it lists (check org number
and fiscal year in the PDF), not only the years you were asked about, and record any listing page you find in the BRF's
`profile.yaml` under `sources:`.

## Scripts

In `tools/brf/` (Python 3, pdfplumber, PyYAML; tesseract OCR with Swedish for scanned pages). OCR is cached in
`.cache/pdfwords/` (`pdfwords.py`), so every script sees the same words for a page and only the first run over a
scanned report is slow:

- `pdf_text.py FILE [--pages 4-12] [--grep REGEX]`: text with a `pN|` page prefix on every line.
- `locate.py FILE PAGE "54 426 020" [...] [--near "Nettoomsättning"] [--union]`: bounding box(es) as
  `x,y,width,height`, in **PDF points** from the top-left corner (a viewer that renders at scale s multiplies by s).
  Use `--near` with the row label when the number appears more than once on the page. Use `--union` for one box
  around several numbers, when a value is a sum.
- `import_document.py`: see above.

## Procedure

1. **Read the report.** Run `pdf_text.py` on the whole PDF. Find these sections and their PDF page numbers:
   Förvaltningsberättelse (board, auditors, valberedning, fiscal year, stämma, the property: number of
   apartments/lokaler/garage and their areas), Resultaträkning (RR),
   Balansräkning (BR, usually 2 pages), Kassaflödesanalys (KF), Noter (notes on revenue, operating costs,
   other external costs, personnel, loans), and the auditor's report or signature page (Revisionsberättelse).
   The `page` column is the **1-based page index in the PDF file**, not the page number printed on the page.
2. **Check what already exists.** If the output CSVs exist, read them. Do not add a row whose `(year, category_id)`
   is already present. If the CSVs are missing, create them with the headers above.
3. **Extract only the fiscal year's own column.** Ignore the comparison column for the previous year.
   (Earlier years come from their own reports.)
4. **Map each value to a category** using the rules below. Get its box with `locate.py`.
5. **Append the rows** to the CSVs. Keep the existing rows and their order, and put new rows at the end, grouped as in the reference.
   If you cannot get a box (e.g. a sum), leave `x,y,width,height` empty and let `verify_sources.py --fix` fill it.
6. **Verify.** Follow the verify-brf-data skill: `verify_sources.py --fix`, then `verify_sources.py` and
   `validate.py` until both are clean. Fix the extraction, not the checks.
7. **Report back.** List the rows added, any judgement calls (lines you grouped or summed, categories
   you left out, the unassigned remainders and basis changes the checks reported), and anything you could not find.

## Value conventions

- **Amounts:** integer SEK with no spaces. If the report is in tkr, multiply by 1000 and say so in the report.
  `-` or a blank cell means the item is not present: skip the row. Write `0` only when the report prints 0.
- **Signs in financial_events:** income is positive. Costs are **negative**, even where a note prints them
  positive. Cash flow items keep the sign printed in the KF (+ means cash in, − means cash out).
- **Signs in financial_states:** use the balance as printed, normally positive.
- **Sums:** when one category combines several lines (for example all "Hyror …" lines), store the sum. Give it a
  `--union` box covering all the lines.
- **general_states values:** for several people, separate names with `;` and keep the order of the report.
  Use names only, without roles. Mark a mid-year change on the name, e.g. `Anna Berg (t.o.m. 2024-05-20)` or
  `Per Ek (fr.o.m. 2024-05-20)`. Write the fiscal year as `YYYY-MM-DD – YYYY-MM-DD`, with spaces around an en dash.
- **Boxes:** give the box of the value itself (the number or the names), not of its label. Leave `x,y,width,height`
  empty only when there is no single place to point at (for example, a name mentioned inside a sentence spread
  over several lines). `page` must still be filled in.

## Category mapping

### financial_events (from RR, KF and notes)

Top level, from the **Resultaträkning**:

| id | name | take |
|---|---|---|
| 0 | Nettoomsättning | RR "Nettoomsättning" (some reports call it "Rörelseintäkter" when there is no separate övriga line) |
| 4 | Övriga intäkter | RR "Övriga rörelseintäkter" |
| 1 | Driftkostnader | RR "Driftkostnader"/"Driftskostnader"/"Fastighetskostnader" (negative) |
| 16 | Planerat underhåll | only when the RR shows planned maintenance as **its own line**, outside Driftkostnader |
| 5 | Övriga externa kostnader | RR line |
| 6 | Personalkostnader | RR "Personalkostnader (och arvoden)" |
| 56 | Skatt på årets resultat | RR, only if non-zero |

Do **not** record depreciation (avskrivningar), rörelseresultat or årets resultat. These are not categories.

From the **Kassaflödesanalys**:

| id | name | take |
|---|---|---|
| 7 | Räntekostnader | KF interest paid ("Erlagd ränta"). If the KF nets interest in one line ("Erlagd ränta, ränteintäkter"), put the net value here and skip 15. If the KF has no interest lines, use RR "Räntekostnader och liknande resultatposter" |
| 15 | Erhållen ränta | KF interest received, when shown separately (else RR "Ränteintäkter" if 7 also came from the RR) |
| 8 | Investeringar | KF "Kassaflöde från investeringsverksamheten" |
| 3 | Kassaflöde från finansieringsverksamheten | KF total for financing activities |
| 13 | Kundfordringar | KF change in kundfordringar / avgifts- och hyresfordringar |
| 14 | Övriga fordringar | KF change in other receivables (övriga fordringar, förutbetalda kostnader, "fordringar") |
| 11 | Leverantörsskulder | KF change in leverantörsskulder |
| 12 | Övriga kortfristiga skulder | KF change in other short-term liabilities (all non-leverantör lines summed) |
| 9 | Rörelsekapital - ej insamlade intäkter | only if the KF gives **only** one aggregated receivables line ("Ökning/minskning av kortfristiga fordringar"). Then use 9 instead of 13/14 |
| 10 | Rörelsekapital - ej betalda kostnader | only if the KF gives **only** one aggregated short-term liabilities line. Then use 10 instead of 11/12 |

Children, from the **notes**. A child row goes in only when the note gives the line, and the parent's children
should add up to the parent.

- **Under 0 Nettoomsättning (revenue note):**
  - 17 Årsavgifter: all årsavgift lines (bostäder + lokaler, and "Årsavgifter el") summed.
  - 18 Hyror: all hyra lines summed (bostäder, lokaler, garage, p-platser, övrigt).
  - 19 Övriga intäkter (nettoomsättning): everything else in the note, such as överlåtelse- and
    pantsättningsavgifter, andrahandsuthyrning, debited electricity or fiber, påminnelseavgifter.
  - 20 Avgifts- och hyresbortfall: vacancies and rent losses (negative).
- **Under 1 Driftkostnader (operating costs note):**
  - 2 Fastighetsskötsel och lokalvård: the **sum** of 50–55 below. Record it as a separate row with a union box.
    - 50 Fastighetsskötsel
    - 51 Städning
    - 52 Hisstillsyn / hissavtal
    - 53 Tillsyn och besiktning (OVK, besiktningar, kontroller, obligatoriska besiktningar)
    - 54 Trädgårdsskötsel / markskötsel
    - 55 Snöröjning
  - 21 Reparationer
  - 31 Underhåll: maintenance shown **inside** the Driftkostnader note
  - 22 El
  - 23 Uppvärmning / fjärrvärme
  - 24 Vatten / VA
  - 25 Sophämtning / avfall
  - 26 Fastighetsförsäkring / försäkringspremie
  - 32 Försäkringsskador / självrisker
  - 27 Kabel-TV och bredband / IT
  - 28 Fastighetsskatt och fastighetsavgift (sum)
  - 29 Förvaltningsarvoden (ekonomisk + teknisk förvaltning, förvaltningsavtal)
  - 57 Tomträttsavgäld: ground rent for a tomträtt (site leasehold), when the note shows it
  - 30 Övriga driftkostnader: any remaining lines, summed.
- **Under 5 Övriga externa kostnader:**
  - 33 Bevakning
  - 34 Hyror och arrenden / leasing
  - 35 Förbrukningsinventarier och varuinköp
  - 36 Administration
  - 37 Extern revision: audit fees listed under övriga externa
  - 38 Konsult
  - 39 Medlemsavgifter
  - 40 Juristarvode
  - 41 Föreningsverksamhet
  - 43 Underhållsplan
  - 42 Övriga förvaltningskostnader: remaining lines.
- **Under 6 Personalkostnader:**
  - 44 Styrelsearvoden
  - 45 Revisionsarvode: only when listed under personnel
  - 46 Övriga arvoden
  - 47 Sociala avgifter
  - 49 Löner och övriga ersättningar
  - 48 Övriga personalkostnader

If a material note line fits no category, put it in the parent's "övriga" child (30, 42 or 48) and mention it in
the report. Do not add new categories to the shared CSVs unless the user agrees.

Notes on applying the child rules:

- **Driftkostnader split over several notes.** If the RR line cites several notes (e.g. "Driftskostnader 4, 5, 6, 7":
  fastighetsskötsel, reparationer och underhåll, taxebundna kostnader, övriga driftskostnader), all their lines
  are children of 1.
- **Aggregate lines.** If the note prints one line covering several of 50–55 (or 50–55 together with
  förvaltning, e.g. "Fastighetsskötsel och förvaltning") ("Fastighetsskötsel, lokalvård och
  samfälligheter") plus separate ones ("Snö och halkbekämpning"), 2 is the sum of all of them; map only the
  separate lines to 50–55, so the aggregate line is the remainder of 2.
- **Notes with their own subtotal.** If a note is itself one group, such as a "Fastighetsskötsel" note with a Summa,
  use that printed Summa for 2. Map its lines to 50–55. A leftover "Övrigt" line inside that note stays unassigned:
  it shows up as the remainder of 2, which is fine.
- **Sums stay on one page.** A summed value's box has to be on one page, so only sum lines from the same page and
  note. Do not sum "Övrigt" lines from different notes into one row. Take one of them and leave the other unassigned.
- **Other external costs.** Ekonomisk förvaltning, IT-tjänster and bankkostnader listed under övriga externa
  kostnader go to 36 Administrationskostnader. (29 Förvaltningsarvoden is only for förvaltning inside Driftkostnader.)
  Styrelse- och årsmöten / stämmokostnader go to 41 Föreningsverksamhet.
- **Lines with no category.** RR lines like "Övriga rörelsekostnader" (e.g. realisationsförlust) have no category.
  Skip them and mention them in the report.
- **Rounding.** A remainder of ±1–2 kr between a parent and its children is rounding in the report. Leave it.

### financial_states (from BR; balances at fiscal year end)

| id | name | take |
|---|---|---|
| 0 | Övriga skulder till kreditinstitut | **short-term** part of loans: "Kortfristiga skulder → (Skulder till kreditinstitut / Kortfristig del av långfristig skuld)". Watch out: in many reports the BR labels the *long-term* line "Övriga skulder till kreditinstitut". Map by short/long term, not by label |
| 1 | Skulder till kreditinstitut | **long-term** part of loans (Långfristiga skulder → skulder till kreditinstitut) |
| 2 | Kassa och bank | KF "Likvida medel vid årets slut". If the KF does not show it, use BR "Kassa och bank". If 3/4/5 are recorded separately, use only BR "Kassa och bank" here so that nothing is counted twice. Many HSB associations keep their money on the avräkningskonto *inside* "Övriga kortfristiga fordringar" (Bergfoten: BR kassa 34 291, KF likvida medel 13 355 498): then 2 is the KF figure |
| 3 | Avräkningskonto HSB Stockholm | only if the BR lists it separately |
| 4 | Placeringskonto HSB Stockholm | only if the BR lists it separately |
| 5 | Kortfristiga placeringar | only if the BR lists it separately |
| 6 | Eget kapital | BR "Summa eget kapital" |
| 7 | Summa tillgångar | BR "Summa tillgångar" |

If the loan note (Skulder till kreditinstitut) splits short- and long-term differently from the BR, use the BR.

### property_states (from the Förvaltningsberättelse)

Counts and areas of what the association owns, at the end of the fiscal year, **as printed in that year's
report**. Newer HSB reports have a table "Antal | Benämning | Total yta m2"; older ones say it in prose
("24 bostadshus innehållande 604 lägenheter, nio hyresrätter, fem kommersiella lokaler", "Total bostadsyta:
55.548 kvm"). Use the formal förvaltningsberättelse when a bundle has two.

| id | name | take |
|---|---|---|
| 0 | Lägenheter bostadsrätt (antal) | apartments upplåtna med bostadsrätt (the property description, not "medlemslägenheter") |
| 1 | Lägenheter hyresrätt (antal) | rental apartments |
| 2 | Lokaler (antal) | lokaler / kommersiella lokaler, whatever the report counts |
| 3 | Garageplatser (antal) | garage places |
| 4 | Parkeringsplatser (antal) | p-platser / bilplatser (not MC or visitor places) |
| 5 | Förråd (antal) | only when the table lists förråd |
| 6 | Garage- och parkeringsplatser (antal) | only when the report gives one combined number; then skip 3/4 |
| 7 | Total yta (m²) | the table's "Totalt" area, only when printed |
| 8 | Bostadsrättsyta (m²) | area of the bostadsrätt apartments (child of 7) |
| 9 | Hyresrättsyta (m²) | area of the rental apartments (child of 7) |
| 10 | Lokalyta (m²) | area of the lokaler (child of 7) |
| 11 | Förrådsyta (m²) | area of förråd (child of 7) |
| 12 | Bostadsyta totalt (m²) | a single dwelling area without a bostadsrätt/hyresrätt split (older prose reports) |

Rows with 0 m² (garage, p-platser) get no area row. Numbers written as words ("nio") are fine: the verifier
reads them. When a report changes its basis (e.g. counts förråd as lokaler), store it as printed:
`validate.py` reports every change between years as `PROP`, and a changed basis should be mentioned in the report.

### general_states (from Förvaltningsberättelse and signatures)

| id | name | take |
|---|---|---|
| 0 | Räkenskapsår | fiscal year, `2024-01-01 – 2024-12-31` |
| 1 | Ordförande | chair |
| 2 | Vice ordförande | vice chair, if named |
| 3 | Ledamöter | all other ordinary board members (ledamöter). Excludes the chair, the vice chair, suppleanter and HSB-appointed members |
| 10 | Ledamot utsedd av HSB | board member appointed by HSB (or a similar external organisation), e.g. "Ledamot (HSB Stockholm)" |
| 8 | Suppleanter | deputy members |
| 4 | Valberedning | nomination committee |
| 5 | Revisorer | auditors as described in the förvaltningsberättelse (person names and/or audit firm, e.g. `Mattias Matti;Kungsbron Borevision`) |
| 9 | Revisorer signerat årsredovisningen | the people who sign the revisionsberättelse or the audit endorsement |
| 6 | Närvarande röstberättigade medlemmar | members present at the stämma. Normally taken from the stämma protocol. Take it from the report only if the report states it, and use the **year the stämma was held** |
| 7 | Fullmakter | proxies at that same stämma (same rule as 6) |
| 11 | Byggnadsår | building year(s) as printed (`1964 och 1966`, `1966-68`; HSB: the "Nybyggnadsår byggnad" table) |
| 12 | Värdeår | the tax assessment's year, when that is what the report gives instead of 11 |
| 13 | Markinnehav | whether the association owns or leases the land, **as printed**: an explicit statement ("Marken innehas med äganderätt", "Föreningen äger fastigheten …", "… med tomträtt", "friköpt"); else the land line in the Byggnader och mark note ("Ingående anskaffningsvärde mark": only an owner has it). Never from HSB's generic glossary pages. See `annual_reviews/brfs/README.md` |

These values belong to the fiscal year (`year` = report year), except 6/7 as noted.

## Pitfalls

- **Never take a value from the comparison column** of a later report, even when the own report is a scan.
  Reports restate earlier years (Sjötungan's 2023 report moved planerat underhåll into Driftkostnader and also
  restated 2022). `verify_sources.py` flags this as COLUMN or WRONG-DOC.
- Bundled stämmohandlingar contain several documents (kallelse, motioner, budget, årsredovisning,
  revisionsberättelse). Take values only from the årsredovisning pages and the revisionsberättelse.
  Ignore e-signing "Document history" / activity-log pages. They contain names and emails, but those are not report data.
- In zsh, `L="python3 locate.py FILE"; $L ...` does not word-split. Call the script directly, or use a shell
  function: `L() { python3 .../locate.py FILE "$@"; }`.
- Section totals can share a line with the previous year's numbers. Always confirm with `locate.py`'s `[line: …]`
  output that you chose the current-year column. It is usually the left of the two amount columns.
- Numbers use spaces as thousands separators and may use the Unicode minus sign (−). `locate.py`
  normalises both, so search for exactly what the text dump shows.
- For scanned pages (`[OCR]` in `pdf_text.py`), read the page image too (render it with
  `pdftoppm -f N -l N -r 110 -png FILE out`). Check every OCR'd number against a sum before trusting it.
