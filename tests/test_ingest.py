from datetime import UTC, date, datetime

import pytest

from signal_app.connectors.base import CompanyCandidate, SignalObservation
from signal_app.db.models import Company
from signal_app.ingest import (
    find_company,
    normalize_domain,
    normalize_name,
    record_observations,
    upsert_candidate,
)


@pytest.mark.parametrize("raw, expected", [
    ("https://www.Acme.ai/about?x=1", "acme.ai"),
    ("acme.ai", "acme.ai"),
    ("WWW.ACME.AI", "acme.ai"),
    ("http://app.acme.ai:8080", "app.acme.ai"),
    ("", None),
    ("   ", None),
    (None, None),
])
def test_normalize_domain(raw, expected):
    assert normalize_domain(raw) == expected


def test_normalize_name_ignores_suffixes_and_punctuation():
    assert normalize_name("Acme, Inc.") == normalize_name("ACME") == "acme"
    assert normalize_name("Acme Labs") == "acme"


def test_find_company_prefers_domain_over_name(session):
    by_domain = Company(name="Totally Different", domain="acme.ai")
    by_name = Company(name="Acme")
    session.add_all([by_domain, by_name])
    session.flush()
    assert find_company(session, domain="https://acme.ai", name="Acme") is by_domain


def test_find_company_by_github_org_case_insensitive(session):
    company = Company(name="Acme", github_org="AcmeHQ")
    session.add(company)
    session.flush()
    assert find_company(session, github_org="acmehq") is company


def test_name_match_skipped_when_domains_conflict(session):
    session.add(Company(name="Acme", domain="acme.ai"))
    session.flush()
    assert find_company(session, domain="acme.io", name="Acme") is None


def test_upsert_candidate_never_overwrites_existing_values(session, manual_source):
    company = Company(name="Acme", domain="acme.ai", description="My own notes")
    session.add(company)
    session.flush()
    candidate = CompanyCandidate(
        name="acme", domain="acme.ai", description="From an API",
        launch_date=date(2026, 3, 1), github_org="acme",
    )
    found, created = upsert_candidate(session, candidate, manual_source)
    assert found is company and not created
    assert company.description == "My own notes"
    assert company.launch_date == date(2026, 3, 1) and company.github_org == "acme"


def test_upsert_candidate_creates_with_discovery_source(session, manual_source):
    candidate = CompanyCandidate(name="NewCo", domain="https://newco.dev")
    company, created = upsert_candidate(session, candidate, manual_source)
    assert created and company.domain == "newco.dev"
    assert company.discovery_source is manual_source


def test_record_observations_is_idempotent(session, manual_source):
    company = Company(name="Acme")
    session.add(company)
    session.flush()
    obs = [SignalObservation("github.stars", 42, datetime(2026, 9, 1, tzinfo=UTC))]
    assert record_observations(session, company, obs, manual_source) == 1
    assert record_observations(session, company, obs, manual_source) == 0
