"""Command-line interface. Run ``signal --help`` for the full list.

The interactive ``add-*`` commands are built for speed after a demo day: every prompt
can be skipped with Enter, and a skipped prompt is stored as unknown, never as a guess.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import click
from sqlalchemy import func, select

from signal_app.connectors import manual
from signal_app.db.models import Company, SignalSnapshot, SourceKind
from signal_app.db.session import session_scope
from signal_app.ingest import (
    find_company,
    get_or_create_source,
    record_observations,
    source_for,
    upsert_candidate,
)

ROOT = Path(__file__).resolve().parent.parent
SEED_DIR = ROOT / "data" / "seed"
SEED_ORDER = ("companies", "founders", "rounds", "signals")


def _ask(label: str, parser: Callable[[str | None], Any], default: str = "") -> Any:
    """Prompt until the answer parses. Enter on an empty prompt means unknown."""
    while True:
        raw = click.prompt(label, default=default, show_default=bool(default))
        try:
            return parser(raw)
        except manual.RowError as exc:
            click.secho(f"  {exc}", fg="red")


def _source(session, where: str | None):
    name = where or f"Manual entry {datetime.now(UTC):%Y-%m-%d}"
    return get_or_create_source(session, SourceKind.MANUAL, name)


def _lookup(session, company: str) -> Company:
    found = find_company(session, domain=company, name=company)
    if found is None:
        raise click.ClickException(f"No company matches {company!r}.")
    return found


@click.group()
def cli() -> None:
    """Signal: pre-seed startup discovery and tracking."""


@cli.command("init-db")
def init_db() -> None:
    """Create or upgrade the database schema (runs Alembic migrations)."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(cfg, "head")
    click.echo("Database is up to date.")


@cli.command("add-company")
def add_company() -> None:
    """Add a company interactively, then optionally its founders and round."""
    p = manual
    values = {
        "name": _ask("Company name", p.parse_str),
        "domain": _ask("Website or domain", p.normalize_domain),
        "description": _ask("One-line description", p.parse_str),
        "sector": _ask("Sector", p.parse_str, "AI Infrastructure"),
        "sub_sector": _ask("Sub-sector", p.parse_str),
        "hq_country": _ask("HQ country (2-letter code)", p.parse_str),
        "launch_date": _ask("Public launch date (YYYY-MM-DD)", p.parse_date),
        "accelerator": _ask("Accelerator", p.parse_str),
        "accelerator_batch": _ask("Batch", p.parse_str),
        "github_org": _ask("GitHub org", p.parse_str),
        "status": _ask("Status (discovered/watchlist/archived)",
                       p.parse_enum(p.TrackingStatus), "watchlist"),
        "notes": _ask("Notes", p.parse_str),
    }
    where = _ask("Where did you find it? (e.g. 'YC Demo Day Sep 2026')", p.parse_str)
    with session_scope() as session:
        company, outcome = p.upsert_company(session, values, _source(session, where))
        click.secho(f"{company.name}: {outcome}.", fg="green")
        while click.confirm("Add a founder?", default=not company.founder_links):
            _prompt_founder(session, company, where)
        if click.confirm("Add a round?", default=not company.rounds):
            _prompt_round(session, company, where)


@cli.command("add-founder")
@click.argument("company")
def add_founder(company: str) -> None:
    """Add a founder to COMPANY (name or domain)."""
    where = _ask("Source of this information", manual.parse_str)
    with session_scope() as session:
        _prompt_founder(session, _lookup(session, company), where)


@cli.command("add-round")
@click.argument("company")
def add_round(company: str) -> None:
    """Add a financing round to COMPANY (name or domain)."""
    where = _ask("Source of this information", manual.parse_str)
    with session_scope() as session:
        _prompt_round(session, _lookup(session, company), where)


def _prompt_founder(session, company: Company, where: str | None) -> None:
    p = manual
    values = {
        "name": _ask("  Founder name", p.parse_str),
        "prior_startups_founded": _ask("  Startups founded before", p.parse_int),
        "prior_exit": _ask("  Prior exit? (y/n)", p.parse_bool),
        "early_employee_at_scaled_startup": _ask(
            "  Early employee at a startup that scaled? (y/n)", p.parse_bool),
        "domain_years": _ask("  Years in this problem space", p.parse_float),
        "technical_background": _ask("  Technical background? (y/n)", p.parse_bool),
        "github_username": _ask("  GitHub username", p.parse_str),
        "publications_count": _ask("  Published papers", p.parse_int),
        "notable_employers": _ask("  Notable employers (; separated)", p.parse_list),
        "schools": _ask("  Schools (; separated)", p.parse_list),
        "founder_market_fit_notes": _ask("  Founder-market fit notes", p.parse_str),
    }
    link = {
        "title": _ask("  Title (CEO, CTO...)", p.parse_str),
        "role": _ask("  Role (technical/commercial/other)", p.parse_str),
    }
    founder, outcome = p.upsert_founder(session, company, values, link, _source(session, where))
    click.secho(f"  {founder.name}: {outcome}.", fg="green")


def _prompt_round(session, company: Company, where: str | None) -> None:
    p = manual
    values = {
        "round_type": _ask("  Round type", p.parse_enum(p.RoundType), "pre_seed"),
        "instrument": _ask("  Instrument (safe_post/safe_pre/convertible_note/priced_equity)",
                           p.parse_enum(p.Instrument), "safe_post"),
        "amount_raised_usd": _ask("  Amount raised (e.g. 1.5M)", p.parse_money),
        "valuation_cap_usd": _ask("  Valuation cap (e.g. 12M)", p.parse_money),
        "announced_date": _ask("  Announced date (YYYY-MM-DD)", p.parse_date),
        "lead_investor": _ask("  Lead investor", p.parse_str),
        "source_url": _ask("  Source URL", p.parse_str),
        "notes": _ask("  Notes", p.parse_str),
    }
    if values["valuation_cap_usd"]:
        default = "post_money" if values["instrument"] == p.Instrument.SAFE_POST else ""
        values["cap_type"] = _ask("  Cap type (post_money/pre_money)",
                                  p.parse_enum(p.CapType), default)
    rnd = p.Round(company=company, source=_source(session, where))
    p._apply(rnd, values, overwrite=True)
    session.add(rnd)
    click.secho("  Round added.", fg="green")


@cli.command("import-csv")
@click.argument("kind", type=click.Choice(list(manual.IMPORTERS)))
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--overwrite", is_flag=True, help="Replace existing values, not only blanks.")
def import_csv(kind: str, path: Path, overwrite: bool) -> None:
    """Import a CSV of KIND (companies, founders, rounds, signals)."""
    with session_scope() as session:
        report = manual.IMPORTERS[kind](session, path, overwrite=overwrite)
    click.echo(f"{path.name}: {report.summary()}")


@cli.command("template")
@click.argument("kind", type=click.Choice(list(manual.TEMPLATES)))
@click.argument("path", type=click.Path(dir_okay=False, path_type=Path))
def template(kind: str, path: Path) -> None:
    """Write an empty CSV template for KIND to PATH."""
    manual.write_template(kind, path)
    click.echo(f"Wrote {path}")


@cli.command("seed")
def seed() -> None:
    """Load the fictional demo dataset in data/seed/."""
    with session_scope() as session:
        source = get_or_create_source(
            session, SourceKind.SEED, "Signal demo data (fictional)",
            terms_note="Invented companies and people for demos and tests; not real data.",
        )
        for kind in SEED_ORDER:
            path = SEED_DIR / f"{kind}.csv"
            report = manual.IMPORTERS[kind](session, path, source=source)
            click.echo(f"{kind}: {report.summary()}")


def _connector(name: str):
    if name == "github":
        from signal_app.connectors.github import GitHubConnector
        return GitHubConnector()
    from signal_app.connectors.hackernews import HackerNewsConnector
    return HackerNewsConnector()


@cli.command("discover")
@click.argument("source", type=click.Choice(["github", "hn"]))
@click.option("--days", default=30, show_default=True, help="Look back this many days.")
def discover(source: str, days: int) -> None:
    """Find new companies on SOURCE and add them with status 'discovered'."""
    connector = _connector(source)
    since = datetime.now(UTC) - timedelta(days=days)
    created = matched = 0
    with session_scope() as session:
        src = source_for(session, connector)
        for candidate in connector.discover(since):
            _, is_new = upsert_candidate(session, candidate, src)
            created += is_new
            matched += not is_new
    click.echo(f"{source}: {created} new companies, {matched} already known.")


@cli.command("snapshot")
@click.argument("source", type=click.Choice(["github", "hn"]))
@click.option("--company", help="Only this company (name or domain).")
def snapshot(source: str, company: str | None) -> None:
    """Record current metrics from SOURCE for tracked companies."""
    connector = _connector(source)
    with session_scope() as session:
        src = source_for(session, connector)
        companies = [_lookup(session, company)] if company else session.scalars(
            select(Company).where(Company.status != manual.TrackingStatus.ARCHIVED)).all()
        total = 0
        for c in companies:
            total += record_observations(session, c, connector.snapshot(c), src)
    click.echo(f"{source}: {total} observations recorded across {len(companies)} companies.")


@cli.command("list")
@click.option("--status", type=click.Choice([s.value for s in manual.TrackingStatus]))
def list_companies(status: str | None) -> None:
    """List companies with founder, round and snapshot counts."""
    with session_scope() as session:
        query = select(Company).order_by(Company.name)
        if status:
            query = query.where(Company.status == status)
        counts = dict(session.execute(
            select(SignalSnapshot.company_id, func.count()).group_by(SignalSnapshot.company_id)
        ).all())
        rows = session.scalars(query).all()
        click.echo(f"{'Company':<24}{'Sub-sector':<22}{'Status':<12}{'Fdrs':>5}"
                   f"{'Rnds':>5}{'Snaps':>6}")
        for c in rows:
            click.echo(f"{c.name[:23]:<24}{(c.sub_sector or '-')[:21]:<22}{c.status:<12}"
                       f"{len(c.founder_links):>5}{len(c.rounds):>5}{counts.get(c.id, 0):>6}")
        click.echo(f"{len(rows)} companies")


if __name__ == "__main__":
    cli()
