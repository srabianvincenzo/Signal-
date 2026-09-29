"""Read-side queries for the dashboard, returned as plain dicts and DataFrames.

Kept separate from the Streamlit pages so the logic is testable without a browser and
so the pages only deal with layout.

Missing values stay missing. A company with no GitHub data shows a blank, not a zero,
because "we have not measured it" and "it has no stars" are different facts.
"""

from __future__ import annotations

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from signal_app.db.models import (
    Company,
    Founder,
    Round,
    SignalSnapshot,
    Source,
    TrackingStatus,
)

# Founder fields that feed the Founder Score in Phase 2. Coverage = share known.
FOUNDER_SIGNAL_FIELDS = (
    "prior_startups_founded", "prior_exit", "early_employee_at_scaled_startup",
    "domain_years", "technical_background", "publications_count",
    "notable_employers", "schools", "founder_market_fit_notes",
)

HEADLINE_METRICS = {
    "github.stars": "GitHub stars",
    "hn.launch_points": "HN launch points",
    "manual.waitlist_size": "Waitlist",
}


def latest_metrics(session: Session) -> pd.DataFrame:
    """The most recent value of each headline metric, one row per company."""
    latest = (
        select(
            SignalSnapshot.company_id,
            SignalSnapshot.metric,
            func.max(SignalSnapshot.observed_at).label("observed_at"),
        )
        .where(SignalSnapshot.metric.in_(HEADLINE_METRICS))
        .group_by(SignalSnapshot.company_id, SignalSnapshot.metric)
        .subquery()
    )
    rows = session.execute(
        select(SignalSnapshot.company_id, SignalSnapshot.metric, SignalSnapshot.value)
        .join(latest, (SignalSnapshot.company_id == latest.c.company_id)
              & (SignalSnapshot.metric == latest.c.metric)
              & (SignalSnapshot.observed_at == latest.c.observed_at))
    ).all()
    if not rows:
        return pd.DataFrame(columns=["company_id", *HEADLINE_METRICS.values()])
    df = pd.DataFrame(rows, columns=["company_id", "metric", "value"])
    wide = df.pivot_table(index="company_id", columns="metric", values="value", aggfunc="last")
    return wide.rename(columns=HEADLINE_METRICS).reset_index()


def founder_coverage(founders: list[Founder]) -> float | None:
    """Share of founder signal fields that are filled in, across the team (0 to 1).

    This is not a score. It is a preview of how much the Phase 2 Founder Score will
    have to work with, and it will become part of that score's confidence level.
    """
    if not founders:
        return None
    known = sum(getattr(f, field) is not None for f in founders for field in FOUNDER_SIGNAL_FIELDS)
    return known / (len(founders) * len(FOUNDER_SIGNAL_FIELDS))


def _latest_round(rounds: list[Round]) -> Round | None:
    dated = [r for r in rounds if r.announced_date]
    if dated:
        return max(dated, key=lambda r: r.announced_date)
    return rounds[-1] if rounds else None


def company_table(session: Session, status: TrackingStatus | None = None) -> pd.DataFrame:
    """One row per company with the columns the feed and watchlist show."""
    query = select(Company).order_by(Company.discovered_at.desc())
    if status is not None:
        query = query.where(Company.status == status)
    companies = session.scalars(query).all()
    records = []
    for c in companies:
        rnd = _latest_round(c.rounds)
        coverage = founder_coverage(c.founders)
        records.append({
            "company_id": c.id,
            "Company": c.name,
            "Sub-sector": c.sub_sector,
            "Status": c.status.value,
            "Source": c.discovery_source.kind.value if c.discovery_source else None,
            "Found via": c.discovery_source.name if c.discovery_source else None,
            "Discovered": c.discovered_at.date() if c.discovered_at else None,
            "Launched": c.launch_date,
            "Accelerator": " ".join(filter(None, [c.accelerator, c.accelerator_batch])) or None,
            "Founders": len(c.founder_links),
            "Founder data": None if coverage is None else round(coverage * 100),
            "Raised ($)": rnd.amount_raised_usd if rnd else None,
            "Cap ($)": rnd.valuation_cap_usd if rnd else None,
            "Lead": rnd.lead_investor if rnd else None,
        })
    columns = ["company_id", "Company", "Sub-sector", "Status", "Source", "Found via",
               "Discovered", "Launched", "Accelerator", "Founders", "Founder data",
               "Raised ($)", "Cap ($)", "Lead"]
    df = pd.DataFrame.from_records(records, columns=columns)
    metrics = latest_metrics(session)
    df = df.merge(metrics, on="company_id", how="left")
    for label in HEADLINE_METRICS.values():
        if label not in df:
            df[label] = pd.NA
    return df


def company_names(session: Session) -> list[tuple[int, str]]:
    return list(session.execute(select(Company.id, Company.name).order_by(Company.name)).all())


def snapshots_frame(session: Session, company_id: int) -> pd.DataFrame:
    rows = session.execute(
        select(SignalSnapshot.metric, SignalSnapshot.value, SignalSnapshot.unit,
               SignalSnapshot.observed_at, Source.name, SignalSnapshot.source_url)
        .join(Source, Source.id == SignalSnapshot.source_id)
        .where(SignalSnapshot.company_id == company_id)
        .order_by(SignalSnapshot.metric, SignalSnapshot.observed_at)
    ).all()
    return pd.DataFrame(rows, columns=["Metric", "Value", "Unit", "Observed", "Source", "URL"])


def founders_frame(company: Company) -> pd.DataFrame:
    def show(value):
        if value is None:
            return "unknown"
        if isinstance(value, bool):
            return "yes" if value else "no"
        if isinstance(value, list):
            return ", ".join(value)
        # Text throughout, so "unknown" can sit in a numeric column.
        return f"{value:g}" if isinstance(value, float) else str(value)

    records = []
    for link in company.founder_links:
        f = link.founder
        records.append({
            "Name": f.name,
            "Title": link.title or "unknown",
            "Role": link.role or "unknown",
            "Startups founded": show(f.prior_startups_founded),
            "Prior exit": show(f.prior_exit),
            "Early employee at scaled startup": show(f.early_employee_at_scaled_startup),
            "Years in domain": show(f.domain_years),
            "Technical": show(f.technical_background),
            "Papers": show(f.publications_count),
            "Employers": show(f.notable_employers),
            "Schools": show(f.schools),
            "Founder-market fit": show(f.founder_market_fit_notes),
            "Source": f.source.name,
        })
    return pd.DataFrame.from_records(records)


def rounds_frame(company: Company) -> pd.DataFrame:
    records = [{
        "Type": r.round_type.value,
        "Instrument": r.instrument.value,
        "Raised ($)": r.amount_raised_usd,
        "Cap ($)": r.valuation_cap_usd,
        "Cap type": r.cap_type.value if r.cap_type else None,
        "Announced": r.announced_date,
        "Lead": r.lead_investor,
        "Pro rata": {True: "yes", False: "no", None: "unknown"}[r.pro_rata_rights],
        "Confidence": r.confidence.value,
        "Notes": r.notes,
        "Source": r.source_url or r.source.name,
    } for r in company.rounds]
    return pd.DataFrame.from_records(records)
