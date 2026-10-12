"""Aggregates with the semantics of the Postgres functions the data repo's import uses, so that a value
derived here equals the one the data repo computes from the same rows.

- regr_slope / regr_intercept: least squares of y on x (Youngs-Cramer); None with < 2 points or no x-variance.
- round_numeric: ROUND(numeric[, n]) rounds half away from zero.
- round_double_numeric: ROUND(double::numeric, n): the cast keeps 15 significant digits (DBL_DIG), then
  rounds half away from zero.
- round_double: ROUND(double precision) rounds half to even (rint).
- round_fraction: ROUND of an exact numeric result (AVG over integers or numerics, a numeric division).
- percentile_cont: PERCENTILE_CONT(0.5), interpolated in double precision.
"""
import math
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, getcontext
from fractions import Fraction

getcontext().prec = 40

SECONDS_PER_YEAR = 31557600.0   # EXTRACT(EPOCH FROM sold_date) / 31557600.0 in the data repo


def years_since_epoch(d):
    """A date as fractional years since 1970-01-01 (midnight UTC), the x axis of the per-BRF trends."""
    return (date.fromisoformat(d) - date(1970, 1, 1)).days * 86400 / SECONDS_PER_YEAR


def regr(points):
    """(slope, intercept) of y on x over (x, y) points, or (None, None) as regr_slope/regr_intercept: the
    Youngs-Cramer accumulation Postgres (12+) uses, in the order given, so that a value on a rounding tie
    lands on the same side more often than a two-pass fit would (pass points in ascending x)."""
    n = sx = sy = sxx = sxy = 0.0
    for x, y in points:
        n0, n = n, n + 1
        sx += x
        sy += y
        if n0 > 0:
            tx, ty, scale = x * n - sx, y * n - sy, 1.0 / (n * n0)
            sxx += tx * tx * scale
            sxy += tx * ty * scale
    if n < 2 or sxx == 0:
        return None, None
    slope = sxy / sxx
    return slope, (sy - sx * slope) / n


def round_numeric(v, decimals=0):
    if v is None:
        return None
    q = Decimal(1).scaleb(-decimals)
    r = Decimal(str(v)).quantize(q, rounding=ROUND_HALF_UP)
    return int(r) if decimals == 0 else float(r)


def round_double_numeric(v, decimals):
    if v is None:
        return None
    r = Decimal(f"{v:.15g}").quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    return int(r) if decimals == 0 else float(r)


def round_fraction(v, decimals=0):
    """v: a Fraction (exact), or None."""
    if v is None:
        return None
    r = (Decimal(v.numerator) / Decimal(v.denominator)).quantize(Decimal(1).scaleb(-decimals),
                                                                  rounding=ROUND_HALF_UP)
    return int(r) if decimals == 0 else float(r)


def exact_mean(values):
    """AVG over integer or numeric values, exactly (as Postgres numeric does); None if empty."""
    values = [v if isinstance(v, Fraction) else Fraction(Decimal(str(v))) for v in values if v is not None]
    return sum(values) / len(values) if values else None


def percentile_cont(values, q=0.5):
    values = sorted(float(v) for v in values if v is not None)
    if not values:
        return None
    pos = q * (len(values) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    return values[lo] if lo == hi else values[lo] + (pos - lo) * (values[hi] - values[lo])


def round_double(v):
    return None if v is None else int(round(v))


def mean(values):
    return sum(values) / len(values) if values else None


def regr_two_pass(points):
    """regr as a two-pass least-squares fit (mean first, then deviations). Same value as regr up to float
    rounding; on an exact .5 tie the two can round apart. Used where it matches the data repo more often
    (per-BRF yearly series: measured with tools/cloud/parity.py)."""
    if len(points) < 2:
        return None, None
    n = len(points)
    mx = sum(x for x, _ in points) / n
    my = sum(y for _, y in points) / n
    sxx = sum((x - mx) ** 2 for x, _ in points)
    if sxx == 0:
        return None, None
    slope = sum((x - mx) * (y - my) for x, y in points) / sxx
    return slope, my - slope * mx
