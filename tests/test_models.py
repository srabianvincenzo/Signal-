from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from signal_app.db.models import (
    Company,
    CompanyFounder,
    Founder,
    Round,
    SignalSnapshot,
    TrackingStatus,
)


def test_company_defaults_and_unknowns(session):
    company = Company(name="Acme")
    session.add(company)
    session.flush()
    assert company.status == TrackingStatus.DISCOVERED
    assert company.domain is None and company.launch_date is None
    assert company.discovered_at is not None


def test_booleans_are_tri_state(session, manual_source):
    unknown = Founder(name="A", source=manual_source)
    no_exit = Founder(name="B", prior_exit=False, source=manual_source)
    session.add_all([unknown, no_exit])
    session.flush()
    session.expire_all()
    assert unknown.prior_exit is None
    assert no_exit.prior_exit is False


def test_domain_is_unique(session):
    session.add_all([Company(name="A", domain="a.example"), Company(name="B", domain="a.example")])
    with pytest.raises(IntegrityError):
        session.flush()


def test_round_rejects_nonpositive_amount(session, manual_source):
    company = Company(name="Acme")
    session.add(Round(company=company, amount_raised_usd=-1, source=manual_source))
    with pytest.raises(IntegrityError):
        session.flush()


def test_round_rejects_discount_of_100_percent(session, manual_source):
    company = Company(name="Acme")
    session.add(Round(company=company, discount_rate=1.0, source=manual_source))
    with pytest.raises(IntegrityError):
        session.flush()


def test_repeat_founder_links_to_both_companies(session, manual_source):
    founder = Founder(name="Repeat Founder", prior_exit=True, source=manual_source)
    old, new = Company(name="Old Co"), Company(name="New Co")
    session.add_all([
        CompanyFounder(company=old, founder=founder, role="technical"),
        CompanyFounder(company=new, founder=founder, role="technical"),
    ])
    session.flush()
    assert old.founders == [founder] and new.founders == [founder]
    assert len(founder.company_links) == 2


def test_snapshot_observation_is_unique(session, manual_source):
    company = Company(name="Acme")
    at = datetime(2026, 9, 1, tzinfo=UTC)
    for _ in range(2):
        session.add(SignalSnapshot(company=company, metric="github.stars", value=10,
                                   observed_at=at, source=manual_source))
    with pytest.raises(IntegrityError):
        session.flush()


def test_deleting_company_removes_its_rows(session, manual_source):
    company = Company(name="Acme")
    session.add(Round(company=company, source=manual_source))
    session.add(SignalSnapshot(company=company, metric="m", value=1,
                               observed_at=datetime.now(UTC), source=manual_source))
    session.flush()
    session.delete(company)
    session.flush()
    assert session.query(Round).count() == 0
    assert session.query(SignalSnapshot).count() == 0
