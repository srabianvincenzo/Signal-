"""Manual entry and CSV import: the fastest path for companies found through networking,
demo days and newsletters.

Four CSV templates live in ``data/templates/``: companies, founders, rounds and signals
(for publicly stated numbers such as a waitlist size). Rules:

* Blank cells mean "unknown" and are stored as NULL. Nothing is filled in for you.
* Money accepts ``1500000``, ``1,500,000``, ``$1.5M`` or ``750k``.
* Booleans accept yes/no, true/false, y/n, 1/0; ``unknown`` or blank stays NULL.
* Lists (employers, schools, investors) are separated by semicolons.
* A bad row is reported with its line number and skipped; the rest of the file still
  imports.
* Importing the same file twice changes nothing. Existing values are kept unless you
  pass ``overwrite=True``; empty fields are always filled.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from signal_app.connectors.base import SignalObservation
from signal_app.db.models import (
    CapType,
    Company,
    CompanyFounder,
    CompanyStage,
    DataConfidence,
    Founder,
    Instrument,
    Round,
    RoundType,
    Source,
    SourceKind,
    TrackingStatus,
)
from signal_app.ingest import (
    find_company,
    get_or_create_source,
    normalize_domain,
    normalize_name,
    record_observations,
)

# ------------------------------------------------------------------ cell parsers

_TRUE = {"yes", "y", "true", "t", "1"}
_FALSE = {"no", "n", "false", "f", "0"}
_UNKNOWN = {"", "unknown", "n/a", "na", "?", "-"}


class RowError(ValueError):
    pass


def blank(value: str | None) -> bool:
    return value is None or value.strip().lower() in _UNKNOWN


def parse_str(value: str | None) -> str | None:
    return None if blank(value) else value.strip()


def parse_bool(value: str | None) -> bool | None:
    if blank(value):
        return None
    v = value.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise RowError(f"not a yes/no value: {value!r}")


def parse_int(value: str | None) -> int | None:
    if blank(value):
        return None
    try:
        return int(value.replace(",", "").strip())
    except ValueError as exc:
        raise RowError(f"not a whole number: {value!r}") from exc


def parse_float(value: str | None) -> float | None:
    if blank(value):
        return None
    try:
        return float(value.replace(",", "").strip())
    except ValueError as exc:
        raise RowError(f"not a number: {value!r}") from exc


_MONEY = re.compile(r"^\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*([kmb])?$", re.I)
_MULTIPLIER = {None: 1, "k": 1_000, "m": 1_000_000, "b": 1_000_000_000}


def parse_money(value: str | None) -> int | None:
    """``"$1.5M"`` -> ``1500000``. Whole US dollars."""
    if blank(value):
        return None
    match = _MONEY.match(value.strip())
    if not match:
        raise RowError(f"not a dollar amount: {value!r}")
    number = float(match.group(1).replace(",", ""))
    suffix = match.group(2).lower() if match.group(2) else None
    return round(number * _MULTIPLIER[suffix])


def parse_rate(value: str | None) -> float | None:
    """Accepts ``0.2``, ``20%`` or ``20`` for a 20% discount."""
    if blank(value):
        return None
    v = value.strip()
    pct = v.endswith("%")
    number = parse_float(v.rstrip("%"))
    if pct or number > 1:
        number /= 100
    return number


def parse_date(value: str | None) -> date | None:
    if blank(value):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise RowError(f"dates must be YYYY-MM-DD: {value!r}") from exc


def parse_datetime(value: str | None) -> datetime | None:
    if blank(value):
        return None
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise RowError(f"not an ISO date/time: {value!r}") from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def parse_list(value: str | None) -> list[str] | None:
    if blank(value):
        return None
    items = [part.strip() for part in value.split(";") if part.strip()]
    return items or None


def parse_enum(enum_cls: type) -> Callable[[str | None], Any]:
    def parser(value: str | None):
        if blank(value):
            return None
        v = value.strip().lower().replace("-", "_").replace(" ", "_")
        try:
            return enum_cls(v)
        except ValueError as exc:
            allowed = ", ".join(m.value for m in enum_cls)
            raise RowError(f"{value!r} is not one of: {allowed}") from exc

    return parser


# ------------------------------------------------------------------ column specs

COMPANY_COLUMNS: dict[str, Callable[[str | None], Any]] = {
    "name": parse_str,
    "domain": normalize_domain,
    "description": parse_str,
    "sector": parse_str,
    "sub_sector": parse_str,
    "hq_country": parse_str,
    "hq_city": parse_str,
    "founded_date": parse_date,
    "launch_date": parse_date,
    "stage": parse_enum(CompanyStage),
    "status": parse_enum(TrackingStatus),
    "accelerator": parse_str,
    "accelerator_batch": parse_str,
    "github_org": parse_str,
    "github_repo": parse_str,
    "notes": parse_str,
}

FOUNDER_COLUMNS: dict[str, Callable[[str | None], Any]] = {
    "name": parse_str,
    "prior_startups_founded": parse_int,
    "prior_exit": parse_bool,
    "early_employee_at_scaled_startup": parse_bool,
    "domain_years": parse_float,
    "technical_background": parse_bool,
    "github_username": parse_str,
    "publications_count": parse_int,
    "notable_employers": parse_list,
    "schools": parse_list,
    "founder_market_fit_notes": parse_str,
    "profile_url": parse_str,
}

FOUNDER_LINK_COLUMNS: dict[str, Callable[[str | None], Any]] = {
    "title": parse_str,
    "role": parse_str,
}

ROUND_COLUMNS: dict[str, Callable[[str | None], Any]] = {
    "round_type": parse_enum(RoundType),
    "instrument": parse_enum(Instrument),
    "amount_raised_usd": parse_money,
    "valuation_cap_usd": parse_money,
    "cap_type": parse_enum(CapType),
    "discount_rate": parse_rate,
    "pre_money_usd": parse_money,
    "post_money_usd": parse_money,
    "pro_rata_rights": parse_bool,
    "announced_date": parse_date,
    "closed_date": parse_date,
    "lead_investor": parse_str,
    "investors": parse_list,
    "confidence": parse_enum(DataConfidence),
    "source_url": parse_str,
    "notes": parse_str,
}

SIGNAL_COLUMNS = ("metric", "value", "unit", "observed_at", "source_url")
REF_COLUMNS = ("company_domain", "company_name", "source_name")

TEMPLATES: dict[str, tuple[str, ...]] = {
    "companies": (*COMPANY_COLUMNS, "source_name"),
    "founders": ("company_domain", "company_name", *FOUNDER_LINK_COLUMNS,
                 *FOUNDER_COLUMNS, "source_name"),
    "rounds": ("company_domain", "company_name", *ROUND_COLUMNS, "source_name"),
    "signals": ("company_domain", "company_name", *SIGNAL_COLUMNS, "source_name"),
}

ROLE_VALUES = {"technical", "commercial", "other"}


# ------------------------------------------------------------------------ report


@dataclass
class ImportReport:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    errors: list[tuple[int, str]] = field(default_factory=list)

    def summary(self) -> str:
        text = (f"{self.created} created, {self.updated} updated, "
                f"{self.unchanged} unchanged, {len(self.errors)} errors")
        for line, message in self.errors:
            text += f"\n  line {line}: {message}"
        return text


# ------------------------------------------------------------------------ helpers


def _parse_row(row: dict[str, str], columns: dict[str, Callable]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for column, parser in columns.items():
        if column in row:
            try:
                values[column] = parser(row[column])
            except RowError as exc:
                raise RowError(f"{column}: {exc}") from exc
    return values


def _apply(obj: Any, values: dict[str, Any], overwrite: bool) -> bool:
    """Set non-None values on ``obj``. Returns True if anything changed."""
    changed = False
    for attr, value in values.items():
        if value is None:
            continue
        current = getattr(obj, attr)
        if current == value:
            continue
        if current is None or overwrite:
            setattr(obj, attr, value)
            changed = True
    return changed


def _row_source(session: Session, row: dict[str, str], default: Source) -> Source:
    name = parse_str(row.get("source_name"))
    if not name:
        return default
    return get_or_create_source(session, SourceKind.MANUAL, name)


def _company_for_row(session: Session, row: dict[str, str]) -> Company:
    domain = normalize_domain(row.get("company_domain"))
    name = parse_str(row.get("company_name"))
    if not domain and not name:
        raise RowError("company_domain or company_name is required")
    company = find_company(session, domain=domain, name=name)
    if company is None:
        raise RowError(
            f"no company matches {domain or name!r}; import it in companies.csv first"
        )
    return company


def _read_csv(path: Path) -> Iterable[tuple[int, dict[str, str]]]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            return
        reader.fieldnames = [f.strip().lower() for f in reader.fieldnames]
        for row in reader:
            if all(blank(v) for v in row.values() if isinstance(v, str)):
                continue
            yield reader.line_num, row


def _run(
    session: Session,
    path: Path,
    handle_row: Callable[[dict[str, str], Source], str],
    source: Source | None = None,
) -> ImportReport:
    """Import each row in its own savepoint so one bad row can't sink the file.

    Rows without a ``source_name`` are attributed to ``source``, or to a source named
    after the file when none is given.
    """
    report = ImportReport()
    default_source = source or get_or_create_source(
        session, SourceKind.CSV, f"CSV import: {path.name}"
    )
    for line, row in _read_csv(path):
        savepoint = session.begin_nested()
        try:
            outcome = handle_row(row, _row_source(session, row, default_source))
            session.flush()
        except (RowError, ValueError) as exc:
            savepoint.rollback()
            report.errors.append((line, str(exc)))
            continue
        except IntegrityError as exc:
            savepoint.rollback()
            report.errors.append((line, f"conflicts with an existing row: {exc.orig}"))
            continue
        savepoint.commit()
        setattr(report, outcome, getattr(report, outcome) + 1)
    return report


# ------------------------------------------------------------------------ writers


def upsert_company(
    session: Session, values: dict[str, Any], source: Source, overwrite: bool = False
) -> tuple[Company, str]:
    """Create or update one company from parsed values. Shared by CSV and the CLI."""
    if not values.get("name"):
        raise RowError("name is required")
    company = find_company(session, domain=values.get("domain"), name=values["name"])
    if company is None:
        company = Company(discovery_source=source)
        _apply(company, values, overwrite=True)
        session.add(company)
        session.flush()
        return company, "created"
    changed = _apply(company, values, overwrite)
    return company, "updated" if changed else "unchanged"


def import_companies(
    session: Session, path: Path, overwrite: bool = False, source: Source | None = None
) -> ImportReport:
    def handle(row: dict[str, str], source: Source) -> str:
        values = _parse_row(row, COMPANY_COLUMNS)
        return upsert_company(session, values, source, overwrite)[1]

    return _run(session, path, handle, source)


def upsert_founder(
    session: Session,
    company: Company,
    values: dict[str, Any],
    link_values: dict[str, Any],
    source: Source,
    overwrite: bool = False,
) -> tuple[Founder, str]:
    if not values.get("name"):
        raise RowError("name is required")
    role = link_values.get("role")
    if role is not None:
        role = role.lower()
        if role not in ROLE_VALUES:
            raise RowError(f"role must be one of {sorted(ROLE_VALUES)}")
        link_values = {**link_values, "role": role}

    key = normalize_name(values["name"])
    link = next(
        (lk for lk in company.founder_links if normalize_name(lk.founder.name) == key), None
    )
    if link is None:
        founder = Founder(source=source)
        _apply(founder, values, overwrite=True)
        link = CompanyFounder(company=company, founder=founder)
        _apply(link, link_values, overwrite=True)
        session.add(link)
        return founder, "created"
    changed = _apply(link.founder, values, overwrite)
    changed |= _apply(link, link_values, overwrite)
    return link.founder, "updated" if changed else "unchanged"


def import_founders(
    session: Session, path: Path, overwrite: bool = False, source: Source | None = None
) -> ImportReport:
    def handle(row: dict[str, str], source: Source) -> str:
        company = _company_for_row(session, row)
        values = _parse_row(row, FOUNDER_COLUMNS)
        link_values = _parse_row(row, FOUNDER_LINK_COLUMNS)
        return upsert_founder(session, company, values, link_values, source, overwrite)[1]

    return _run(session, path, handle, source)


def import_rounds(
    session: Session, path: Path, overwrite: bool = False, source: Source | None = None
) -> ImportReport:
    """A round is the same round if company, type and announced date (or, when the
    date is unknown, amount) match."""

    def handle(row: dict[str, str], source: Source) -> str:
        company = _company_for_row(session, row)
        values = _parse_row(row, ROUND_COLUMNS)
        if values.get("valuation_cap_usd") and not values.get("cap_type"):
            raise RowError("cap_type (post_money or pre_money) is required when a cap is given")
        round_type = values.get("round_type") or RoundType.PRE_SEED
        existing = next(
            (
                r for r in company.rounds
                if r.round_type == round_type
                and (
                    r.announced_date == values.get("announced_date")
                    if values.get("announced_date") or r.announced_date
                    else r.amount_raised_usd == values.get("amount_raised_usd")
                )
            ),
            None,
        )
        if existing is None:
            rnd = Round(company=company, source=source)
            _apply(rnd, {**values, "round_type": round_type}, overwrite=True)
            session.add(rnd)
            return "created"
        return "updated" if _apply(existing, values, overwrite) else "unchanged"

    return _run(session, path, handle, source)


def import_signals(
    session: Session, path: Path, overwrite: bool = False, source: Source | None = None
) -> ImportReport:
    """Publicly stated numbers, e.g. ``manual.waitlist_size``. A source_url is required
    because an unsourced traction claim is not evidence."""

    def handle(row: dict[str, str], source: Source) -> str:
        company = _company_for_row(session, row)
        metric = parse_str(row.get("metric"))
        value = parse_float(row.get("value"))
        observed_at = parse_datetime(row.get("observed_at"))
        url = parse_str(row.get("source_url"))
        if not metric or value is None or observed_at is None:
            raise RowError("metric, value and observed_at are required")
        if not url:
            raise RowError("source_url is required for manually entered signals")
        if "." not in metric:
            metric = f"manual.{metric}"
        obs = SignalObservation(metric, value, observed_at,
                                unit=parse_str(row.get("unit")), source_url=url)
        added = record_observations(session, company, [obs], source)
        return "created" if added else "unchanged"

    return _run(session, path, handle, source)


IMPORTERS: dict[str, Callable[..., ImportReport]] = {
    "companies": import_companies,
    "founders": import_founders,
    "rounds": import_rounds,
    "signals": import_signals,
}


def write_template(kind: str, path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerow(TEMPLATES[kind])
