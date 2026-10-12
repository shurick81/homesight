"""Query-parameter parsers, ported verbatim from the data repo's src/api/handler.py so that accepted
values and HTTP 400 messages are identical. Each returns the parsed value (None when absent) or raises
ValueError with the message the API returns."""
import re


def _parse_year(raw):
    if raw is None:
        return None
    try:
        year = int(raw)
    except ValueError:
        raise ValueError("year must be an integer")
    if not (1900 <= year <= 2100):
        raise ValueError("year out of range")
    return year


def _parse_rooms(raw):
    if raw is None:
        return None
    try:
        rooms = float(raw)
    except ValueError:
        raise ValueError("rooms must be a number")
    if not (0 < rooms <= 20):
        raise ValueError("rooms out of range")
    return rooms


def _parse_coord(raw, name, lo, hi):
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number")
    if not (lo <= value <= hi):
        raise ValueError(f"{name} out of range")
    return value


def _parse_radius_m(raw):
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        raise ValueError("radius_m must be an integer")
    if not (1 <= value <= 100000):
        raise ValueError("radius_m out of range (1-100000)")
    return value


def _parse_construction_year(raw):
    """A building construction year (can predate 1900, unlike a sale/listing year)."""
    if raw is None:
        return None
    try:
        year = int(raw)
    except ValueError:
        raise ValueError("construction year must be an integer")
    if not (1500 <= year <= 2100):
        raise ValueError("construction year out of range")
    return year


_ORG_NUMBER_RE = re.compile(r'^(\d{6})-?(\d{4})$')


def _parse_org_number(raw):
    """A Swedish org.nr — 10 digits, accepted with or without the hyphen and
    normalised to the stored `NNNNNN-NNNN` form for an exact match."""
    if raw is None:
        return None
    m = _ORG_NUMBER_RE.match(raw.strip())
    if not m:
        raise ValueError("brf_org_number must be 10 digits (NNNNNN-NNNN)")
    return f"{m.group(1)}-{m.group(2)}"


_PROPERTY_TYPES = ("bostadsrätt", "villa", "radhus", "fritidshus")

# The two orthogonal axes (see docs / schema.sql). object_form is the physical form
# (row-house family kept distinct); tenure is the property-right. Kept as explicit
# allowlists so an invalid value returns HTTP 400 rather than silently matching nothing.
_OBJECT_FORMS = ("lägenhet", "villa", "radhus", "parhus", "kedjehus", "fritidshus")
_TENURES = ("bostadsrätt", "äganderätt")


def _parse_property_type(raw):
    if raw is None:
        return None
    if raw not in _PROPERTY_TYPES:
        raise ValueError(
            f"property_type must be one of: {', '.join(_PROPERTY_TYPES)}"
        )
    return raw


def _parse_object_form(raw):
    if raw is None:
        return None
    if raw not in _OBJECT_FORMS:
        raise ValueError(
            f"object_form must be one of: {', '.join(_OBJECT_FORMS)}"
        )
    return raw


def _parse_tenure(raw):
    if raw is None:
        return None
    if raw not in _TENURES:
        raise ValueError(
            f"tenure must be one of: {', '.join(_TENURES)}"
        )
    return raw


def _parse_kommun(raw):
    """A kommun name used as an exact-match filter on /advertisements. Free text
    (parameterised, so no injection risk); we only trim and bound the length.
    Valid values are enumerable via GET /kommuns."""
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    if len(value) > 100:
        raise ValueError("kommun too long (max 100 chars)")
    return value


def _parse_omrade(raw):
    """An område (sub-kommun stadsdel) name used as an exact-match filter on
    /advertisements and /brfs. Free text (parameterised, so no injection risk);
    we only trim and bound the length. Valid values are enumerable via GET /areas."""
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    if len(value) > 100:
        raise ValueError("omrade too long (max 100 chars)")
    return value


def _parse_brf_name_search(raw):
    """A case-insensitive substring matched against brf_name on /brfs. LIKE
    metacharacters (\\ % _) are escaped so the term matches literally; the WHERE
    builder wraps the result as %term% with ESCAPE '\\'."""
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    if len(value) > 100:
        raise ValueError("brf_name_search too long (max 100 chars)")
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _parse_area(raw, name):
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number")
    if not (0 <= value <= 100000):
        raise ValueError(f"{name} out of range")
    return value


def _parse_limit(raw, default=10, maximum=10):
    if raw is None:
        return default
    try:
        limit = int(raw)
    except ValueError:
        raise ValueError("limit must be an integer")
    if not (1 <= limit <= maximum):
        raise ValueError(f"limit out of range (1-{maximum})")
    return limit


def _parse_sort(raw, allowed, default):
    if raw is None:
        return default
    if raw not in allowed:
        raise ValueError(f"sort must be one of: {', '.join(sorted(allowed))}")
    return raw


def _parse_nonneg_int(raw, name):
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _parse_int(raw, name):
    """Signed integer (e.g. a price/m² trend, which can be negative)."""
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer")


def _parse_number(raw, name):
    """Signed number (e.g. a fee/m² trend, which can be negative when a BRF's
    avgift has declined over time)."""
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number")


def _parse_index(raw, name):
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number")
    if not (0 <= value <= 1000):
        raise ValueError(f"{name} out of range")
    return value
