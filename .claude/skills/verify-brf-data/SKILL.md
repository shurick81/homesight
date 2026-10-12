---
name: verify-brf-data
description: Verify extracted BRF data in annual_reviews/brfs/<orgnr>/ against the source PDFs and against itself - every value at its cited place, the cash and debt chains between year-ends, parent = sum of children, property areas - and resolve what fails. Use after extracting a BRF annual report, before comparing or analysing BRFs, or when asked whether a BRF's numbers are correct, clean or add up.
---

# Verify BRF data

Step 2 of 3 (extract-brf-annual-review → **verify-brf-data** → compare-brfs). Data is only used for analysis
once it passes these checks. The identities are explained in `annual_reviews/brfs/README.md`.

What is verified per BRF is `verified_through` in `annual_reviews/brfs/<orgnr>/profile.yaml` (summarised in
`index.csv`). Reference examples: Sjötungan `712400-1467` 2015–2025 (2016–2025 chained, 2015 as opening balance)
and Bergfoten `712400-0089` 2024–2025 (2023 as opening balance).

## Run

```
python3 tools/brf/verify_sources.py annual_reviews/brfs/<orgnr> --years 2016-2025 --fix --quiet   # fill/repair boxes
python3 tools/brf/verify_sources.py annual_reviews/brfs/<orgnr> --years 2016-2025 --quiet         # must exit 0
python3 tools/brf/validate.py      annual_reviews/brfs/<orgnr> --years 2016-2025                  # must end with OK
```

Include the opening-balance year in `verify_sources.py --years` (its states are checked against its own report)
and start `validate.py` one year later (its cash check needs the year before). Add `--crosscheck` to
`verify_sources.py` for the comparison-column check below. The first run over a scanned report OCRs every
page (minutes); later runs use the cache in `.cache/pdfwords/`.

## What is checked

| level | check | tool | must |
|---|---|---|---|
| value | the amount is printed inside its cited box (OK), or lines in one column add up to it (SUM) | verify_sources | pass |
| value | it is read from the fiscal year's own report (not WRONG-DOC) and its own column (not COLUMN) | verify_sources | pass |
| value | names in general_states are on the cited page | verify_sources | pass |
| year | sum of top-level events ("Årets kassaflöde") and year-end liquidity ("Likvida medel vid årets slut") are printed in the report, ±2 kr | verify_sources (PRINTED) | should; investigate NOT PRINTED |
| year | parent = Σ children for every category tree | validate (INFO) | explain any remainder > a few kr |
| year | property total yta = Σ areas | validate (AREA) | pass |
| years | **CASH**: Σ top-level events(Y) = L(Y) − L(Y−1), L = states 2+3+4+5 | validate | pass (±25 kr) |
| years | **DEBT**: financing(Y) = D(Y) − D(Y−1), D = states 0+1 | validate (INFO) | explain any gap |
| years | property counts/areas that change (PROP) | validate (INFO) | explain: real change or new basis |
| years | the next year's report repeats the value in its comparison column | verify_sources --crosscheck | informational |

CASH plus PRINTED is the same arithmetic as a cash waterfall: the top-level events of a year add up to the
year's cash flow, which equals the change between the opening and closing liquidity.

## Resolving failures

Always look at the page before changing anything: `python3 tools/brf/pdf_text.py FILE --pages N`, and for scans
render it with `pdftoppm -f N -l N -r 90 -png FILE out` and read the image.

| status | usual cause | fix |
|---|---|---|
| MOVED | box approximate or empty | `--fix` sets it; check it picked the right occurrence for small numbers |
| PAGE n | wrong page cited | `--fix` sets page and box when there is one match |
| COLUMN | value read from the previous-year column | take the left (current-year) value |
| WRONG-DOC | value taken from a later report's comparison column | re-cite (and if restated, re-read) from the year's own report |
| MISSING | wrong value, or OCR misread | read the image. Wrong → correct it. Right but unreadable → `checked_by_eye.csv` (also when OCR then finds the same number on another page, PAGE: keep the page you read, do not let `--fix` move it) |
| TEXT? | name misspelt, wrong page, or handwriting | same as MISSING |
| CASH error | missing or double-counted top-level line, wrong sign, comparison column, liquidity account counted twice (2 and 3/4/5), or wrong opening balance | compare each top-level event with the report's Kassaflödesanalys line by line |
| CASH error = a restated opening | the year's own report restates last year's closing cash (its KF "Likvida medel vid årets början" differs, e.g. a reclassification between kassa and fordringar) | find the moved amount in the comparison column; record the restated opening in `restatements.csv` (columns as financial_states, plus `note`; cited like any value). `validate.py` then uses it and prints RESTATED (INFO) |
| DEBT gap | financing that is not loans (insatser, upplåtelseavgifter) — not an error | mention it; it is real money from members |
| NOT PRINTED | the report has no "Årets kassaflöde" line, or a top-level event is off | if CASH passes and the report has no such line, note it |
| remainder in parent = Σ children | a note line left unmapped, or an aggregate line that cannot be split | map it, or explain it (e.g. "Fastighetsskötsel och förvaltning" stays in 2) |

**checked_by_eye.csv** (per BRF, `table,year,category_id,note`): only for values that are right on the page image
but that OCR cannot read (handwritten signatures, smudged scans). The note says what was seen where. Such rows
report as EYE. Never use it for a value that is not visible on the page.

Each check also handles OCR quirks so you do not have to: a second OCR pass at 400 dpi for missed numbers,
look-alike digits (B→8, O→0), dropped or merged thousands separators ("3614 918", "63 914918"), units glued to
numbers ("385m²") and small numbers written as words ("nio").

## Report

State, per year range: sources (counts of OK/SUM/EYE, exit code), CASH and DEBT per year with differences, any
parent remainders with their explanation, PROP changes with their explanation, and every correction made to
the data (old → new value, why). Do not call data verified if any check was skipped.

When every check passes, set `verified_through` in the BRF's `profile.yaml` to the last verified fiscal year,
note any caveat about the values under `data_notes`, and run `python3 tools/brf/build_index.py`.
