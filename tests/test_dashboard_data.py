import pandas as pd

from signal_app.connectors import manual
from signal_app.dashboard import data
from signal_app.db.models import Company, Founder, TrackingStatus
from tests.test_csv_import import SEED


def load_seed(session):
    for kind in ("companies", "founders", "rounds", "signals"):
        manual.IMPORTERS[kind](session, SEED / f"{kind}.csv")


def test_founder_coverage_counts_known_fields(manual_source):
    empty = Founder(name="A", source=manual_source)
    partial = Founder(name="B", prior_exit=False, domain_years=3, source=manual_source)
    assert data.founder_coverage([]) is None
    assert data.founder_coverage([empty]) == 0
    expected = 2 / (2 * len(data.FOUNDER_SIGNAL_FIELDS))
    assert data.founder_coverage([empty, partial]) == expected


def test_company_table_filters_by_status_and_keeps_unknowns_blank(session):
    load_seed(session)
    watch = data.company_table(session, TrackingStatus.WATCHLIST)
    expected = session.query(Company).filter_by(status=TrackingStatus.WATCHLIST).count()
    assert len(watch) == expected > 0
    assert set(watch["Status"]) == {"watchlist"}
    # Companies with no GitHub data show NA, never 0.
    no_stars = [c.name for c in session.query(Company)
                if not any(s.metric == "github.stars" for s in c.snapshots)]
    everyone = data.company_table(session)
    blanks = everyone[everyone["Company"].isin(no_stars)]["GitHub stars"]
    assert blanks.isna().all()


def test_latest_metrics_takes_most_recent_value(session):
    load_seed(session)
    metrics = data.latest_metrics(session).set_index("company_id")
    for company in session.query(Company):
        stars = sorted((s.observed_at, s.value) for s in company.snapshots
                       if s.metric == "github.stars")
        if stars:
            assert metrics.loc[company.id, "GitHub stars"] == stars[-1][1]


def test_detail_frames_render_unknowns_as_text(session):
    load_seed(session)
    company = next(c for c in session.query(Company) if c.founder_links and c.rounds)
    founders = data.founders_frame(company)
    assert len(founders) == len(company.founder_links)
    # All text, so Arrow can serialise "unknown" next to numbers.
    assert all(pd.api.types.is_string_dtype(founders[c]) for c in founders)
    rounds = data.rounds_frame(company)
    assert set(rounds["Pro rata"]) <= {"yes", "no", "unknown"}
