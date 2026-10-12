# Schools (Skolverket)

Skolverket's *Index på skolenhetsnivå*: for every school unit, a socio-economic index and the expected
share of pupils not eligible for upper-secondary school (andel obehöriga), published yearly for the grant
*Statsbidrag för stärkt likvärdighet och kunskapsutveckling*. A higher index means a higher modelled share
of ineligible pupils (a harder socio-economic intake).

```
skolverket/index_<year>_enriched.csv   one row per school unit and edition, with address and coordinates
```

Columns: `year, orgnr, huvudman, typ, skolenhetskod, skolenhet, obehoriga, index, reg_namn, adress, postnr,
ort, lat, lng, kommun, kommunkod, status`. `index` is empty where Skolverket suppresses it; `typ`
(huvudmannatyp) is printed from 2026 only; `lat, lng` come from Skolenhetsregistret (about 3% of units have
none and are not used as nearest schools).

**Source.** Copied from the data repo (data/processed/skolverket/, extracted there from the PDFs below by
src/extract/skolverket/ and joined with Skolenhetsregistret addresses; snapshot 2026-06-14).

| edition | PDF |
|---|---|
| 2023 | https://www.skolverket.se/download/18.2cb9d0c18340c7dae35b1/1663940117161/index%20per%20skolenhet-statsbidrag%20Likv%C3%A4rdig%20skola%202023.pdf |
| 2024 | https://www.skolverket.se/download/18.c47cd8718cd1c8095f22/1704381892054/Index-pa-skolenhetsniva-2.pdf |
| 2025 | https://www.skolverket.se/download/18.ae4e775192d63b9d1269/1730214695806/Index%20p%C3%A5%20skolenhetsniv%C3%A5%202025%20(pdf).pdf |
| 2026 | https://www.skolverket.se/download/18.4a4f973719c9357f7ba4abf9/1780035430441/Index%20p%C3%A5%20skolenhetsniv%C3%A5%202026-2.pdf |

**Use.** `tools/readmodel/schools.py` puts the five nearest located schools on every listing (geodesic
distance) with their index per edition, the averages over the nearest 1, 3 and 5 and a 2029 projection;
`tools/readmodel/geo.py` averages them per område, kommun and city.
