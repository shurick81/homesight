"""The nearest-school block on every listing: school_1..5_* and the avg_school_socio_index* rollups.

A port of the data repo's import_skolverket.import_schools, build_school_staging, flatten_schools and
flatten_school_aggregates (src/db/import_booli.py), from schools/skolverket/index_<year>_enriched.csv:

- the candidates are the school units with coordinates, each at its latest located edition (name and
  huvudmannatyp from that edition); the index per edition and the obehöriga share of the latest and the
  previous edition come from every edition of the unit, located or not;
- a listing's five nearest are ranked by great-circle distance (PostGIS' geography <->, on a sphere), ties by
  skolenhetskod; school_k_distance_m is the geodesic distance on WGS84 (ST_Distance on geography, which
  PostGIS computes with GeographicLib), rounded to whole metres;
- avg_school_socio_index_1 / _3 / avg_school_socio_index: the mean index of the nearest 1 / 3 / 5 schools in
  the latest edition (schools without one skipped), and the *_forecast columns: the mean of each school's
  least-squares projection to FORECAST_YEAR.

A listing without coordinates gets every school field NULL.
"""
import csv
import math
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from geographiclib.geodesic import Geodesic

from . import stats

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "schools" / "skolverket"
N_SCHOOLS = 5
SCHOOL_YEARS = (2023, 2024, 2025, 2026)
LATEST, PREV = SCHOOL_YEARS[-1], SCHOOL_YEARS[-2]
FORECAST_YEAR = LATEST + 3
SCHOOL_RADII = (1, 3, N_SCHOOLS)
EARTH_RADIUS_M = 6371008.7714150598   # PostGIS' sphere for geography (WGS84 mean radius)
CELL_DEG = 0.05                        # grid cell for the nearest-neighbour search


def agg_col(radius, forecast=False):
    base = "avg_school_socio_index" if radius == N_SCHOOLS else f"avg_school_socio_index_{radius}"
    return f"{base}_forecast" if forecast else base


def flat_columns():
    cols = []
    for k in range(1, N_SCHOOLS + 1):
        cols += [f"school_{k}_skolenhetskod", f"school_{k}_name", f"school_{k}_type", f"school_{k}_distance_m"]
        cols += [f"school_{k}_socio_index_{y}" for y in SCHOOL_YEARS]
        cols += [f"school_{k}_ineligible_pct_latest", f"school_{k}_ineligible_pct_prev"]
    return cols


def _numeric1(raw):
    """A CSV value stored as NUMERIC(n,1): rounded half away from zero to one decimal; None when empty."""
    raw = (raw or "").strip()
    return float(Decimal(raw).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)) if raw else None


def load():
    """(candidates, index): candidates = [(code, name, type, lat, lng)] at each unit's latest located
    edition; index = {code: {socio_index_<y>, ineligible_pct_latest, ineligible_pct_prev}}."""
    rows = {}
    for path in sorted(DATA.glob("index_*_enriched.csv")):
        with open(path, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                code, year = (r["skolenhetskod"] or "").strip(), (r["year"] or "").strip()
                if code and year:
                    rows[(code, int(year))] = r            # last wins, as the data repo's upsert
    located = {}
    index = {}
    for (code, year), r in sorted(rows.items(), key=lambda kv: kv[0][1]):
        if r["lat"] and r["lng"]:
            located[code] = (code, r["skolenhet"] or f"Skolenhet {code}", r["typ"] or None,
                             float(r["lat"]), float(r["lng"]))
        i = index.setdefault(code, {})
        i[f"socio_index_{year}"] = _numeric1(r["index"])
        if year == LATEST:
            i["ineligible_pct_latest"] = _numeric1(r["obehoriga"])
        if year == PREV:
            i["ineligible_pct_prev"] = _numeric1(r["obehoriga"])
    return list(located.values()), index


def _sphere_m(lat1, lng1, lat2, lng2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


class Nearest:
    """Nearest located schools to a point: a lat/lng grid, searched in growing rings until the n-th nearest
    found is closer than anything an unsearched ring could hold."""

    def __init__(self, candidates):
        self.grid = {}
        for c in candidates:
            self.grid.setdefault(self._cell(c[3], c[4]), []).append(c)

    @staticmethod
    def _cell(lat, lng):
        return math.floor(lat / CELL_DEG), math.floor(lng / CELL_DEG)

    def find(self, lat, lng, n=N_SCHOOLS):
        ci, cj = self._cell(lat, lng)
        found, ring = [], 0
        # a ring r cell away is at least (r - 1) cells away; a cell is >= CELL_DEG * 111 km * cos(lat) wide
        min_cell_m = CELL_DEG * 111_000 * math.cos(math.radians(min(abs(lat) + CELL_DEG * 4, 89)))
        while True:
            for i in range(ci - ring, ci + ring + 1):
                for j in range(cj - ring, cj + ring + 1):
                    if max(abs(i - ci), abs(j - cj)) != ring:
                        continue
                    for c in self.grid.get((i, j), ()):
                        found.append((_sphere_m(lat, lng, c[3], c[4]), c[0], c))
            found.sort(key=lambda t: (t[0], t[1]))
            if len(found) >= n and found[n - 1][0] <= ring * min_cell_m:
                return [t[2] for t in found[:n]]
            ring += 1
            if ring > 200:
                return [t[2] for t in found[:n]]


def flatten(ads):
    """flatten_schools + flatten_school_aggregates onto listing documents (in place)."""
    candidates, index = load()
    nearest = Nearest(candidates)
    geod = Geodesic.WGS84
    for a in ads:
        a.update(dict.fromkeys(flat_columns()))
        loc = a.get("location")
        if loc and loc.get("lat") is not None and loc.get("lng") is not None:
            for k, (code, name, typ, lat, lng) in enumerate(nearest.find(loc["lat"], loc["lng"]), 1):
                i = index.get(code, {})
                a[f"school_{k}_skolenhetskod"] = code
                a[f"school_{k}_name"] = name
                a[f"school_{k}_type"] = typ
                a[f"school_{k}_distance_m"] = int(round(geod.Inverse(loc["lat"], loc["lng"], lat, lng)["s12"]))
                for y in SCHOOL_YEARS:
                    a[f"school_{k}_socio_index_{y}"] = i.get(f"socio_index_{y}")
                a[f"school_{k}_ineligible_pct_latest"] = i.get("ineligible_pct_latest")
                a[f"school_{k}_ineligible_pct_prev"] = i.get("ineligible_pct_prev")
        for r in SCHOOL_RADII:
            a[agg_col(r)] = avg_index(a, r)
            forecasts = [f for f in (school_forecast(a, k) for k in range(1, r + 1)) if f is not None]
            a[agg_col(r, forecast=True)] = (stats.round_double_numeric(sum(forecasts) / len(forecasts), 1)
                                           if forecasts else None)


def avg_index(a, n=N_SCHOOLS, year=LATEST):
    """_avg_school_index_expr: ROUND(AVG(index of the n nearest in `year`), 1), schools without one skipped."""
    return stats.round_fraction(stats.exact_mean([a.get(f"school_{k}_socio_index_{year}")
                                                  for k in range(1, n + 1)]), 1)


def school_forecast(a, k):
    """_school_forecast_expr: school k's least-squares index projected to FORECAST_YEAR (None with < 2 points)."""
    # the latest edition first, then the others ascending: the order in which the data repo's Postgres
    # accumulates them (it decides which side of a .x5 tie the projection lands on; measured with parity.py)
    order = (LATEST,) + tuple(y for y in SCHOOL_YEARS if y != LATEST)
    points = [(float(y), a.get(f"school_{k}_socio_index_{y}")) for y in order]
    slope, icpt = stats.regr([(y, v) for y, v in points if v is not None])
    return None if slope is None else icpt + slope * FORECAST_YEAR
