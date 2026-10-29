"""Pure, non-coercing validators mirroring src/lib/data.ts and urls.ts.

TS's `rejected` includes accepted records with dropped optional URLs. Source
metadata counters accept signed integers in TS (only dataset counts are >= 0).
No I/O is performed. The existing httpx dependency supplies idna for contextual
Unicode hostname validation that Python's built-in IDNA codec does not provide.
"""

from datetime import datetime, timedelta
from encodings.idna import nameprep
import ipaddress
import idna
import math
import re
import unicodedata
from urllib.parse import unquote, urlsplit

from models import (
    DATA_ORIGINS, DATASET_FIELDS, DATASET_NAMES, SCHEMA_VERSION,
    SOURCE_IDS, SOURCE_STATUSES, FieldSpec, RejectedRecord, ValidationReport,
)

# ECMAScript trim whitespace (Python strip additionally removes U+001C..U+001F).
_JS_SPACE = "\u0009\u000a\u000b\u000c\u000d\u0020\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_ISO = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})")
_MISSING = object()


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip(_JS_SPACE))


def _one_of(value: object, choices: tuple[str, ...]) -> bool:
    return isinstance(value, str) and value in choices


def _finite(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, ValueError):
        return False


def _integer(value: object) -> bool:
    return _finite(value) and value == math.trunc(value)


def _ipv4_number(part: str) -> int:
    if part.lower().startswith("0x"):
        if re.fullmatch(r"0[xX][0-9a-fA-F]*", part) is None:
            raise ValueError("Invalid IPv4 hexadecimal part.")
        return int(part[2:] or "0", 16)
    if not part.isascii() or not part.isdigit():
        raise ValueError("Invalid IPv4 decimal part.")
    if len(part) > 1 and part.startswith("0"):
        return int(part[1:] or "0", 8)
    return int(part, 10)


def _valid_hostname(host: str) -> bool:
    host = unquote(host, errors="strict")
    if host.startswith("[") and host.endswith("]"):
        ipaddress.IPv6Address(host[1:-1])
        return "%" not in host  # WHATWG does not permit IPv6 zone IDs.
    # Browsers permit empty/long labels; Python's full IDNA encoder adds DNS
    # length restrictions that new URL() does not enforce. Nameprep/punycode
    # separately retain those browser cases, including mapped-away characters.
    if not host.isascii():
        labels = []
        host = host.translate(str.maketrans({"\u3002": ".", "\uff0e": ".", "\uff61": "."}))
        for label in host.split("."):
            if "\u200c" in label or "\u200d" in label:
                try:
                    idna.core.check_label(idna.uts46_remap(label, std3_rules=False))
                except idna.IDNAError:
                    return False
            mapped = nameprep(label) if label else ""
            if mapped and unicodedata.category(mapped[0]).startswith("M"):
                return False
            labels.append(mapped if mapped.isascii() else "xn--" + mapped.encode("punycode").decode("ascii"))
        host = ".".join(labels)
    if not host or re.search(r"[\x00-\x20\x7f#%/:<>?@\[\\\]^|]", host):
        return False
    parts = host.split(".")
    if parts[-1] == "":
        parts.pop()
    if not parts:
        return True
    # WHATWG treats numeric final labels as IPv4, including hex/octal forms.
    last = parts[-1]
    try:
        _ipv4_number(last)
        numeric = True
    except ValueError:
        numeric = last.isascii() and last.isdigit()
    if numeric:
        if len(parts) > 4:
            return False
        numbers = [_ipv4_number(part) for part in parts]
        return (all(0 <= n <= 255 for n in numbers[:-1])
                and 0 <= numbers[-1] < 256 ** (5 - len(numbers)))
    return True


def is_safe_external_url(value: object) -> bool:
    """HTTPS, hostname, no credentials, <=2048 JS UTF-16 units, no trim.

    Normalize special-scheme slashes and embedded TAB/CR/LF like new URL().
    UTS-46 validates contextual Unicode joiners before hostname normalization.
    """
    try:
        if (not isinstance(value, str) or not value or value.strip(_JS_SPACE) != value
                or len(value.encode("utf-16-le", errors="surrogatepass")) // 2 > 2048):
            return False
        cleaned = value.strip("".join(chr(c) for c in range(33)))
        cleaned = cleaned.replace("\t", "").replace("\r", "").replace("\n", "")
        if not re.match(r"^https:", cleaned, re.IGNORECASE):
            return False
        # HTTPS is a WHATWG special scheme: https:example.com is also valid.
        parsed = urlsplit("https://" + cleaned[6:].replace("\\", "/").lstrip("/"))
        if parsed.username or parsed.password or not parsed.hostname:
            return False
        parsed.port  # Force validation of malformed/out-of-range ports.
        authority = parsed.netloc.rsplit("@", 1)[-1]
        host = authority[:authority.index("]") + 1] if authority.startswith("[") else parsed.hostname
        return _valid_hostname(host)
    except (ValueError, TypeError, UnicodeError, OverflowError):
        return False


def safe_external_url_or_none(value: object) -> str | None:
    return value if is_safe_external_url(value) else None


def parse_iso_datetime(value: object) -> datetime | None:
    """Aware ISO timestamp, with TS calendar/offset checks and midnight 24:00.

    Fractions beyond microseconds retain their acceptance, but datetime truncates
    their precision. Validation/output keeps the original timestamp string.
    """
    if not isinstance(value, str) or _ISO.fullmatch(value) is None:
        return None
    try:
        rollover = value[11:13] == "24"
        if rollover:
            if re.match(r"24:00:00(?:\.0+)?(?:Z|[+-])", value[11:]) is None:
                return None
            value = value[:11] + "00" + value[13:]
        # fromisoformat normalizes out-of-range offset minutes; TS rejects them.
        if value[-1] != "Z" and (int(value[-5:-3]) > 23 or int(value[-2:]) > 59):
            return None
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if rollover:
            try:
                return result + timedelta(days=1)
            except OverflowError:
                # This value is used as a validity sentinel at Python's upper date boundary.
                return result
        return result
    except (ValueError, OverflowError):
        return None


def _field_valid(spec: FieldSpec, value: object) -> bool:
    match spec.kind:
        case "string":
            return _nonempty(value)
        case "datetime":
            return parse_iso_datetime(value) is not None
        case "number":
            return _finite(value)
        case "nullable_number":
            return value is None or _finite(value)
        case "boolean":
            return isinstance(value, bool)
        case "strings":
            return isinstance(value, list) and all(isinstance(v, str) for v in value)
        case "enum":
            return _one_of(value, spec.choices)
        case "url":
            return is_safe_external_url(value)
    return False


def _reason(spec: FieldSpec) -> str:
    descriptions = {
        "string": "a non-empty string", "datetime": "a valid ISO date-time string",
        "number": "a finite number", "nullable_number": "null or a finite number",
        "boolean": "a boolean", "strings": "an array of strings", "url": "a safe HTTPS URL",
    }
    if spec.kind == "enum":
        return f"{spec.name} must be one of: {', '.join(spec.choices)}."
    return f"{spec.name} must be {descriptions[spec.kind]}{' when present' if not spec.required else ''}."


def _validate_record(dataset: str, record: object, index: int) -> ValidationReport:
    report = ValidationReport()
    if not _one_of(dataset, DATASET_NAMES):
        report.rejected.append(RejectedRecord(index, None, ["Dataset kind is invalid."]))
        return report
    if not isinstance(record, dict):
        report.rejected.append(RejectedRecord(index, None, ["record must be an object."]))
        return report
    record_id = record.get("id") if _nonempty(record.get("id")) else None
    cleaned: dict = {}
    fatal: list[str] = []
    dropped: list[str] = []
    specs = DATASET_FIELDS[dataset]
    # Match TS diagnostic order: required fields, ID, optional non-URLs, URLs.
    ordered = ([s for s in specs if s.required and s.name != "id"]
               + [s for s in specs if s.name == "id"]
               + [s for s in specs if not s.required and s.kind != "url"]
               + [s for s in specs if not s.required and s.kind == "url"])
    for spec in ordered:
        if not spec.required and spec.name not in record:
            continue
        value = record.get(spec.name, _MISSING)
        if _field_valid(spec, value):
            cleaned[spec.name] = list(value) if spec.kind == "strings" else value
        elif not spec.required and spec.kind == "url":
            dropped.append(f"{spec.name} was dropped because it is not a safe HTTPS URL.")
        else:
            fatal.append(_reason(spec))
    if not fatal:
        report.accepted.append({s.name: cleaned[s.name] for s in specs if s.name in cleaned})
    if fatal or dropped:
        report.rejected.append(RejectedRecord(index, record_id, fatal + dropped))
    if dropped:
        report.dropped_optional.append(RejectedRecord(index, record_id, dropped))
    return report


def validate_record(dataset: str, record: object) -> ValidationReport:
    return _validate_record(dataset, record, 0)


def validate_dataset_items(dataset: str, items: object) -> ValidationReport:
    if not _one_of(dataset, DATASET_NAMES) or not isinstance(items, list):
        return ValidationReport(rejected=[RejectedRecord(0, None, ["Dataset kind or items is invalid."])])
    report = ValidationReport()
    seen: set[str] = set()
    for index, record in enumerate(items):
        result = _validate_record(dataset, record, index)
        if result.accepted:
            record_id = result.accepted[0]["id"]
            if record_id in seen:
                result.accepted.clear()
                if not result.rejected:
                    result.rejected.append(RejectedRecord(index, record_id, []))
                result.rejected[0].reasons.append("duplicate id; first occurrence was kept.")
            else:
                seen.add(record_id)
        report.accepted.extend(result.accepted)
        report.rejected.extend(result.rejected)
        report.dropped_optional.extend(result.dropped_optional)
    return report


def _schema_valid(obj: dict) -> bool:
    return (_finite(obj.get("schemaVersion")) and obj.get("schemaVersion") == SCHEMA_VERSION
            and _one_of(obj.get("dataOrigin"), DATA_ORIGINS))


def validate_envelope(dataset: str, obj: object) -> tuple[bool, str | None]:
    if not _one_of(dataset, DATASET_NAMES):
        return False, "Dataset kind is invalid."
    if (not isinstance(obj, dict) or not _schema_valid(obj)
            or parse_iso_datetime(obj.get("generatedAtIso")) is None
            or not isinstance(obj.get("items"), list)):
        return False, "Dataset envelope is invalid."
    return True, None


def _nullable_date(value: object) -> bool:
    return value is None or parse_iso_datetime(value) is not None


def validate_metadata(obj: object) -> tuple[bool, str | None]:
    if (not isinstance(obj, dict) or not _schema_valid(obj)
            or parse_iso_datetime(obj.get("lastUpdatedIso")) is None
            or not _nullable_date(obj.get("lastAttemptIso", _MISSING))
            or not isinstance(obj.get("counts"), dict)
            or not isinstance(obj.get("sources"), dict)):
        return False, "Metadata is invalid."
    if any(not _integer(obj["counts"].get(name)) or obj["counts"][name] < 0 for name in DATASET_NAMES):
        return False, "Metadata counts are invalid."
    for source_id, entry in obj["sources"].items():
        if (not _one_of(source_id, SOURCE_IDS) or not isinstance(entry, dict)
                or not _one_of(entry.get("status"), SOURCE_STATUSES)
                or any(not _nullable_date(entry.get(f, _MISSING)) for f in ("lastSuccessIso", "lastAttemptIso"))
                or any(entry.get(f, _MISSING) is not None and not _integer(entry.get(f))
                       for f in ("recordsFetched", "recordsAccepted"))
                or (entry.get("errorSummary", _MISSING) is not None
                    and (not isinstance(entry.get("errorSummary"), str)
                         or len(entry["errorSummary"]) > 300))):
            return False, "Metadata source status is invalid."
    return True, None
