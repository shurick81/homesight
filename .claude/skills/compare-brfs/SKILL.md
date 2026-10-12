---
name: compare-brfs
description: Compare verified BRF data between associations or years - where the money goes (heating, interest, repairs, investments …), income, debt and liquidity - per m² or as a share of income, running costs or all outflows. Use when asked to compare BRFs, to put a BRF's costs or debt in perspective, or to compute per-m² or per-apartment figures.
---

# Compare BRFs

Step 3 of 3 (extract-brf-annual-review → verify-brf-data → **compare-brfs**). Only compare years that passed
verify-brf-data; say so in the answer, and name any year that did not.

## Run

```
python3 tools/brf/compare.py 712400-1467=Sjötungan 712400-0089=Bergfoten --years 2024-2025              # kr per m²
python3 tools/brf/compare.py 712400-1467=Sjötungan 712400-0089=Bergfoten --years 2025 --basis income   # % of income
#   --basis running (% of running costs) | outflows (% of all money out) | --csv out.csv
```

## Method

**Cash view.** Amounts are what flowed in the year (financial_events), not accounting results: interest is
interest paid, depreciation and write-offs are left out, investments and loan repayments are shown as money out.
Working-capital changes are timing, not spending, and are left out.

**Buckets.** Reports itemise operating costs differently (one prints "Fastighetsskötsel och förvaltning" as one
line, another lists skötsel, städ, snö and förvaltning apart), so note lines are grouped before comparing:

| bucket | event categories |
|---|---|
| Income | 0 Nettoomsättning + 4 Övriga intäkter |
| Heating | 23 |
| Interest (net paid) | 7 + 15 (interest paid minus received) |
| Property care & management | 2 (with 50–55) + 29 Förvaltningsarvoden |
| Repairs & maintenance | 21 + 31 + 32 + 16 |
| Water / Electricity / Waste / Insurance / Property tax / Cable-TV | 24 / 22 / 25 / 26 / 28 / 27 |
| Board, staff & admin | 5 Övriga externa + 6 Personalkostnader |
| Other operating | 30, plus any Driftkostnader not in a bucket (so running costs add up to the report) |
| Running costs | the sum of the buckets above (not Income) |
| Investments | 8 |
| Net loan repayment / new borrowing | 3 (financing), split by sign |
| Debt, liquid funds at year end | states 0+1, states 2+3+4+5 |

Keep the buckets in `compare.py` as the single definition; change them there, not in an ad-hoc script.

**Which basis to use:**

- **kr per m²** for comparing BRFs of different size. Denominator: the report's total area (property_states 7
  "Total yta", else the sum of the areas 8–11, else 12 + 10 on the older basis). This is the base HSB uses for its
  key figures, so `Debt at year end` per m² must equal the report's "Skuldsättning kr/kvm" (Sjötungan 2025: 4 462,
  Bergfoten 2025: 7 086) — check it, it validates both the area and the loans.
  (Årsavgift per m² in the reports uses bostadsrättsyta (8) instead: Bergfoten 16 596 301 / 15 909 = 1 043.)
- **% of income** for "how much of what members pay goes to X".
- **% of running costs** for the cost structure without one-off projects.
- **% of outflows** to show how much goes to investments and loan repayment.

Always state the denominator. "20% of the money" is ambiguous between income, running costs and outflows.

## Pitfalls

- **One-off years.** A large project dominates a year (Bergfoten 2024: 84% of outflows was its stambyte). Compare
  running costs, or several years, before concluding.
- **Area basis changes.** Property figures are stored as printed, and reports change basis (Sjötungan: 55 548 m²
  "bostadsyta" until 2019, 50 388 + 567 + 710 m² from 2020). Per-m² values across such a change are not
  comparable; `validate.py` lists these as PROP lines, and compare.py prints the area and its basis per column.
- **Merged lines.** If one BRF's report merges two buckets, compare at the merged level (that is why
  förvaltning is in "Property care & management").
- **Pass-through income.** Consumption-based fees (el, vatten) billed to members raise both income and cost;
  mention it when electricity or water differ a lot.
- **Net interest.** Some reports net interest received into one line (Sjötungan 2025), so interest is compared net.

## Report

Lead with the answer (which BRF spends more on what, per m² or share), then the table, then the 2–4 differences
that matter with their reason from the reports, then caveats (one-off years, basis changes, merged lines).
