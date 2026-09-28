from pathlib import Path

import pytest

from signal_app.connectors import manual
from signal_app.db.models import CapType, Company, Instrument, SignalSnapshot

SEED = Path(__file__).resolve().parent.parent / "data" / "seed"


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


# ------------------------------------------------------------------ parsers

@pytest.mark.parametrize("raw, expected", [
    ("1500000", 1_500_000), ("1,500,000", 1_500_000), ("$1.5M", 1_500_000),
    ("750k", 750_000), ("$2m", 2_000_000), ("", None), ("unknown", None),
])
def test_parse_money(raw, expected):
    assert manual.parse_money(raw) == expected


def test_parse_money_rejects_garbage():
    with pytest.raises(manual.RowError):
        manual.parse_money("about a million")


@pytest.mark.parametrize("raw", ["0.2", "20%", "20"])
def test_parse_rate_accepts_fraction_or_percent(raw):
    assert manual.parse_rate(raw) == pytest.approx(0.2)


def test_parse_rate_blank_is_unknown():
    assert manual.parse_rate("") is None


@pytest.mark.parametrize("raw, expected", [
    ("yes", True), ("Y", True), ("true", True), ("no", False), ("0", False),
    ("", None), ("unknown", None),
])
def test_parse_bool(raw, expected):
    assert manual.parse_bool(raw) is expected


def test_parse_list():
    assert manual.parse_list("Stripe; NVIDIA ;") == ["Stripe", "NVIDIA"]
    assert manual.parse_list("") is None


# ------------------------------------------------------------------ companies

COMPANIES = """
name,domain,sector,launch_date,status,source_name
Acme,https://www.acme.ai,AI Infrastructure,2026-03-01,watchlist,YC Demo Day
Beta,beta.dev,,not-a-date,,
,nameless.dev,,,,
Gamma,,AI Infrastructure,,discovered,
"""


def test_import_companies_reports_bad_rows_and_keeps_good_ones(session, tmp_path):
    report = manual.import_companies(session, write(tmp_path, "c.csv", COMPANIES))
    assert report.created == 2
    assert [line for line, _ in report.errors] == [3, 4]
    assert "launch_date" in report.errors[0][1]
    acme = session.query(Company).filter_by(name="Acme").one()
    assert acme.domain == "acme.ai"
    assert acme.discovery_source.name == "YC Demo Day"
    gamma = session.query(Company).filter_by(name="Gamma").one()
    assert gamma.discovery_source.name == "CSV import: c.csv"


def test_import_companies_is_idempotent(session, tmp_path):
    path = write(tmp_path, "c.csv", COMPANIES)
    manual.import_companies(session, path)
    second = manual.import_companies(session, path)
    assert second.created == 0 and second.updated == 0 and second.unchanged == 2
    assert session.query(Company).count() == 2


def test_import_fills_blanks_but_keeps_values_unless_overwrite(session, tmp_path):
    manual.import_companies(session, write(tmp_path, "a.csv", "name,domain,sector\nAcme,acme.ai,"))
    manual.import_companies(session, write(tmp_path, "b.csv",
                                           "name,domain,sector\nAcme,acme.ai,Dev tools"))
    acme = session.query(Company).one()
    assert acme.sector == "Dev tools"  # blank was filled

    manual.import_companies(session, write(tmp_path, "c.csv",
                                           "name,domain,sector\nAcme,acme.ai,Fintech"))
    assert acme.sector == "Dev tools"  # existing value kept
    manual.import_companies(session, write(tmp_path, "d.csv",
                                           "name,domain,sector\nAcme,acme.ai,Fintech"),
                            overwrite=True)
    assert acme.sector == "Fintech"


def test_blank_cells_stay_unknown(session, tmp_path):
    manual.import_companies(session, write(tmp_path, "c.csv", "name,domain,hq_country\nAcme,,"))
    acme = session.query(Company).one()
    assert acme.domain is None and acme.hq_country is None


# ------------------------------------------------------------------ founders & rounds

def _acme(session, tmp_path):
    manual.import_companies(session, write(tmp_path, "c.csv", "name,domain\nAcme,acme.ai"))
    return session.query(Company).one()


def test_import_founders(session, tmp_path):
    acme = _acme(session, tmp_path)
    csv_text = """
company_domain,company_name,name,role,prior_exit,domain_years,schools
acme.ai,,Maya Okafor,Technical,yes,6,MIT; Stanford
,Acme,Arjun Iyer,commercial,,,
nope.dev,,Ghost,technical,,,
acme.ai,,Bad Role,wizard,,,
"""
    report = manual.import_founders(session, write(tmp_path, "f.csv", csv_text))
    assert report.created == 2
    assert [line for line, _ in report.errors] == [4, 5]
    assert "import it in companies.csv first" in report.errors[0][1]
    maya = next(f for f in acme.founders if f.name == "Maya Okafor")
    assert maya.prior_exit is True and maya.domain_years == 6
    assert maya.schools == ["MIT", "Stanford"]
    assert maya.company_links[0].role == "technical"
    arjun = next(f for f in acme.founders if f.name == "Arjun Iyer")
    assert arjun.prior_exit is None


def test_import_rounds_requires_cap_type_with_cap(session, tmp_path):
    acme = _acme(session, tmp_path)
    csv_text = """
company_domain,instrument,amount_raised_usd,valuation_cap_usd,cap_type,announced_date
acme.ai,SAFE_post,$1M,$10M,post_money,2026-04-01
acme.ai,safe_post,$1M,$10M,,2026-05-01
"""
    report = manual.import_rounds(session, write(tmp_path, "r.csv", csv_text))
    assert report.created == 1 and len(report.errors) == 1
    rnd = acme.rounds[0]
    assert rnd.instrument == Instrument.SAFE_POST and rnd.cap_type == CapType.POST_MONEY
    assert rnd.amount_raised_usd == 1_000_000 and rnd.valuation_cap_usd == 10_000_000

    again = manual.import_rounds(session, write(tmp_path, "r2.csv", csv_text))
    assert again.created == 0 and again.unchanged == 1


def test_import_signals_requires_source_url(session, tmp_path):
    _acme(session, tmp_path)
    csv_text = """
company_domain,metric,value,unit,observed_at,source_url
acme.ai,waitlist_size,1500,signups,2026-05-01,https://acme.ai/blog
acme.ai,waitlist_size,3000,signups,2026-06-01,
"""
    report = manual.import_signals(session, write(tmp_path, "s.csv", csv_text))
    assert report.created == 1
    assert "source_url" in report.errors[0][1]
    snap = session.query(SignalSnapshot).one()
    assert snap.metric == "manual.waitlist_size"
    assert snap.observed_at.date().isoformat() == "2026-05-01"


# ------------------------------------------------------------------ seed data

def test_seed_data_loads_cleanly(session):
    for kind in ("companies", "founders", "rounds", "signals"):
        report = manual.IMPORTERS[kind](session, SEED / f"{kind}.csv")
        assert report.errors == [], f"{kind}: {report.summary()}"
    companies = session.query(Company).all()
    assert len(companies) == 20
    assert all(c.sector == "AI Infrastructure" and c.domain.endswith(".example")
               for c in companies)
    # Sparse by design: some companies have no round or no traction data.
    assert any(not c.rounds for c in companies)
    assert any(not c.snapshots for c in companies)
