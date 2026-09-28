"""Turns connector output into database rows, with deduplication and provenance.

Every source flows through here, so the rules live in one place:

* A company is matched by domain first, then GitHub org, then normalized name.
  Domain is the most reliable identifier a startup has; names collide constantly.
* Existing values are never overwritten by a connector. A field is filled only when it
  is empty, so something you typed in by hand is not replaced by a guess from an API.
* Observations are idempotent: re-running a connector for the same moment adds nothing.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlparse

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from signal_app.connectors.base import CompanyCandidate, Connector, SignalObservation
from signal_app.db.models import Company, SignalSnapshot, Source, SourceKind


def normalize_domain(value: str | None) -> str | None:
    """``"https://www.Acme.ai/about"`` -> ``"acme.ai"``. Returns None for blanks."""
    if not value or not value.strip():
        return None
    value = value.strip().lower()
    if "://" not in value:
        value = "//" + value
    host = urlparse(value).hostname or ""
    host = host.removeprefix("www.")
    return host or None


def normalize_name(name: str) -> str:
    """Case- and punctuation-insensitive key: ``"Acme, Inc."`` -> ``"acme"``."""
    key = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    key = re.sub(r"\b(inc|llc|ltd|corp|co|labs?|ai|hq)\b", " ", key)
    return re.sub(r"\s+", "", key)


def get_or_create_source(
    session: Session,
    kind: SourceKind,
    name: str,
    url: str | None = None,
    terms_note: str | None = None,
) -> Source:
    source = session.scalar(select(Source).where(Source.kind == kind, Source.name == name))
    if source is None:
        source = Source(kind=kind, name=name, url=url, terms_note=terms_note)
        session.add(source)
        session.flush()
    return source


def source_for(session: Session, connector: Connector) -> Source:
    return get_or_create_source(
        session, connector.source_kind, connector.source_name,
        connector.source_url, connector.terms_note,
    )


def find_company(
    session: Session,
    *,
    domain: str | None = None,
    github_org: str | None = None,
    name: str | None = None,
) -> Company | None:
    domain = normalize_domain(domain)
    if domain:
        found = session.scalar(select(Company).where(Company.domain == domain))
        if found:
            return found
    if github_org:
        found = session.scalar(
            select(Company).where(func.lower(Company.github_org) == github_org.lower())
        )
        if found:
            return found
    if name:
        key = normalize_name(name)
        if key:
            # Name matching is the weakest key, so only use it when the stored
            # company has no conflicting domain.
            for company in session.scalars(select(Company)):
                if normalize_name(company.name) == key and (
                    not domain or not company.domain
                ):
                    return company
    return None


_CANDIDATE_FIELDS = (
    "description", "launch_date", "github_org", "github_repo", "hn_launch_item_id",
    "accelerator", "accelerator_batch",
)


def upsert_candidate(
    session: Session, candidate: CompanyCandidate, source: Source
) -> tuple[Company, bool]:
    """Insert a discovered company, or fill empty fields on the one we already have.

    Returns ``(company, created)``.
    """
    domain = normalize_domain(candidate.domain)
    company = find_company(
        session, domain=domain, github_org=candidate.github_org, name=candidate.name
    )
    created = company is None
    if company is None:
        company = Company(name=candidate.name, domain=domain, discovery_source=source)
        session.add(company)
    elif domain and not company.domain:
        company.domain = domain

    for attr in _CANDIDATE_FIELDS:
        value = getattr(candidate, attr)
        if value is not None and getattr(company, attr) is None:
            setattr(company, attr, value)

    session.flush()
    record_observations(session, company, candidate.observations, source)
    return company, created


def record_observations(
    session: Session,
    company: Company,
    observations: Iterable[SignalObservation],
    source: Source,
) -> int:
    """Store observations, skipping any already recorded. Returns the number added."""
    added = 0
    for obs in observations:
        exists = session.scalar(
            select(SignalSnapshot.id).where(
                SignalSnapshot.company_id == company.id,
                SignalSnapshot.metric == obs.metric,
                SignalSnapshot.observed_at == obs.observed_at,
                SignalSnapshot.source_id == source.id,
            )
        )
        if exists:
            continue
        session.add(
            SignalSnapshot(
                company_id=company.id,
                metric=obs.metric,
                value=float(obs.value),
                unit=obs.unit,
                observed_at=obs.observed_at,
                source_id=source.id,
                source_url=obs.source_url,
                raw_payload=obs.raw,
            )
        )
        added += 1
    session.flush()
    return added
