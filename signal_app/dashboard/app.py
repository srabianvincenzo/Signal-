"""Signal dashboard (Streamlit). Launch with ``signal dashboard``.

Pages in this first version: Discovery feed, Watchlist, Company detail, Add company.
Founder and Traction Scores arrive in Phase 2 and the valuation card in Phase 3; until
then the tables show how much founder data each company has, which is what the score's
confidence level will be built from.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy import inspect

from signal_app.connectors import manual
from signal_app.dashboard import data
from signal_app.db.models import Company, SourceKind, TrackingStatus
from signal_app.db.session import get_engine, session_scope
from signal_app.ingest import get_or_create_source

st.set_page_config(page_title="Signal", layout="wide")

# Categorical slot 1 of the reference data-viz palette (light / dark variants are
# close enough that one value reads on both Streamlit themes).
SERIES_COLOR = "#2a78d6"

MONEY = st.column_config.NumberColumn(format="dollar")
TABLE_CONFIG = {
    "company_id": None,  # hidden
    "Raised ($)": MONEY,
    "Cap ($)": MONEY,
    "Founder data": st.column_config.ProgressColumn(
        "Founder data", help="Share of founder fields filled in. Scores in Phase 2 "
        "will use this for their confidence level.", format="%d%%", min_value=0,
        max_value=100),
    "GitHub stars": st.column_config.NumberColumn(format="%d"),
    "HN launch points": st.column_config.NumberColumn(format="%d"),
    "Waitlist": st.column_config.NumberColumn(format="%d"),
    "Discovered": st.column_config.DateColumn(),
    "Launched": st.column_config.DateColumn(),
}


@contextmanager
def db() -> Iterator:
    with session_scope(get_engine()) as session:
        yield session


def database_ready() -> bool:
    return inspect(get_engine()).has_table("company")


def setup_screen() -> None:
    st.title("Signal")
    st.info("The database is empty. Create it and load the 20 fictional demo companies "
            "to explore the dashboard, or run `signal init-db` and import your own CSVs.")
    if st.button("Create database and load demo data", type="primary"):
        from signal_app.cli import load_seed
        from signal_app.db.models import Base

        Base.metadata.create_all(get_engine())
        load_seed()
        st.rerun()


# ------------------------------------------------------------------ table pages


def _filters(df: pd.DataFrame, key: str) -> pd.DataFrame:
    col1, col2, col3 = st.columns([2, 2, 3])
    sources = col1.multiselect("Source", sorted(df["Source"].dropna().unique()), key=f"{key}-src")
    sectors = col2.multiselect("Sub-sector", sorted(df["Sub-sector"].dropna().unique()),
                               key=f"{key}-sub")
    text = col3.text_input("Search", placeholder="Company, lead investor, notes...",
                           key=f"{key}-q")
    if sources:
        df = df[df["Source"].isin(sources)]
    if sectors:
        df = df[df["Sub-sector"].isin(sectors)]
    if text:
        needle = text.lower()
        df = df[df["Company"].str.lower().str.contains(needle)
                | df["Lead"].fillna("").str.lower().str.contains(needle)]
    return df


def _company_table(status: TrackingStatus, key: str, columns: list[str]) -> None:
    with db() as session:
        df = data.company_table(session, status)
    if df.empty:
        st.caption("Nothing here yet.")
        return
    df = _filters(df, key)
    st.caption(f"{len(df)} companies. Select a row to open it or change its status. "
               "Empty cells mean unknown, not zero.")
    event = st.dataframe(
        df[["company_id", *columns]], hide_index=True, width="stretch",
        column_config=TABLE_CONFIG, on_select="rerun", selection_mode="single-row",
        key=f"{key}-table",
    )
    rows = event.selection.rows if event else []
    if not rows:
        return
    company_id = int(df.iloc[rows[0]]["company_id"])
    name = df.iloc[rows[0]]["Company"]
    col1, col2, col3, _ = st.columns([1, 1, 1, 3])
    if col1.button(f"Open {name}", type="primary"):
        st.session_state["company_id"] = company_id
        st.switch_page(PAGES["detail"])
    other = [s for s in TrackingStatus if s != status]
    for col, target in zip((col2, col3), other, strict=True):
        if col.button(f"Move to {target.value}"):
            with db() as session:
                session.get(Company, company_id).status = target
            st.rerun()


def discovery_page() -> None:
    st.title("Discovery feed")
    st.write("Companies found by connectors or imports that you haven't reviewed yet, "
             "newest first.")
    _company_table(TrackingStatus.DISCOVERED, "feed", [
        "Company", "Sub-sector", "Source", "Found via", "Discovered", "Launched",
        "Accelerator", "GitHub stars", "HN launch points", "Founders", "Founder data",
    ])


def watchlist_page() -> None:
    st.title("Watchlist")
    st.write("Companies you are tracking. Founder and Traction Scores, confidence and "
             "Seed Readiness are added in Phase 2.")
    _company_table(TrackingStatus.WATCHLIST, "watch", [
        "Company", "Sub-sector", "Launched", "Accelerator", "Founders", "Founder data",
        "Raised ($)", "Cap ($)", "Lead", "GitHub stars", "HN launch points", "Waitlist",
    ])


def archived_page() -> None:
    st.title("Archived")
    _company_table(TrackingStatus.ARCHIVED, "archived", [
        "Company", "Sub-sector", "Source", "Launched", "Founders", "Raised ($)", "Cap ($)",
    ])


# ------------------------------------------------------------------ detail


def _metric_chart(frame: pd.DataFrame, label: str, unit: str | None) -> go.Figure:
    fig = go.Figure(go.Scatter(
        x=frame["Observed"], y=frame["Value"], mode="lines+markers", name=label,
        line={"color": SERIES_COLOR, "width": 2}, marker={"size": 8},
        hovertemplate="%{x|%b %d, %Y}<br>%{y:,.0f} " + (unit or "") + "<extra></extra>",
    ))
    fig.update_layout(
        title={"text": label, "font": {"size": 14}}, height=260, showlegend=False,
        margin={"l": 8, "r": 8, "t": 40, "b": 8}, hovermode="x unified",
        xaxis={"showgrid": False},
        yaxis={"rangemode": "tozero", "gridcolor": "rgba(128,128,128,0.15)"},
    )
    return fig


def detail_page() -> None:
    with db() as session:
        options = data.company_names(session)
        if not options:
            st.caption("No companies yet.")
            return
        ids = [i for i, _ in options]
        names = dict(options)
        current = st.session_state.get("company_id", ids[0])
        company_id = st.selectbox(
            "Company", ids, index=ids.index(current) if current in ids else 0,
            format_func=names.get,
        )
        st.session_state["company_id"] = company_id
        c = session.get(Company, company_id)

        st.title(c.name)
        if c.description:
            st.write(c.description)
        coverage = data.founder_coverage(c.founders)
        accelerator = " ".join(filter(None, [c.accelerator, c.accelerator_batch]))
        facts = {
            "Status": c.status.value,
            "Sub-sector": c.sub_sector or "unknown",
            "Launched": str(c.launch_date) if c.launch_date else "unknown",
            "Accelerator": accelerator or "none known",
            "Founder data": "no founders" if coverage is None else f"{coverage:.0%} filled in",
        }
        for col, (label, value) in zip(st.columns(len(facts)), facts.items(), strict=True):
            col.caption(label)
            col.markdown(f"**{value}**")
        details = [f"**Website:** {c.domain}" if c.domain else "**Website:** unknown",
                   f"**HQ:** {', '.join(filter(None, [c.hq_city, c.hq_country])) or 'unknown'}",
                   f"**Found via:** {c.discovery_source.name if c.discovery_source else 'unknown'}"]
        st.markdown(" · ".join(details))
        if c.notes:
            st.caption(c.notes)

        st.subheader("Founders")
        founders = data.founders_frame(c)
        if founders.empty:
            st.caption("No founders recorded. Add them with `signal add-founder` or founders.csv.")
        else:
            st.dataframe(founders, hide_index=True, width="stretch")

        st.subheader("Rounds")
        rounds = data.rounds_frame(c)
        if rounds.empty:
            st.caption("No round known. That is common at pre-seed.")
        else:
            st.dataframe(rounds, hide_index=True, width="stretch",
                         column_config={"Raised ($)": MONEY, "Cap ($)": MONEY})
        st.caption("The valuation card (range, dilution and sensitivity) is added in Phase 3.")

        st.subheader("Traction")
        snaps = data.snapshots_frame(session, company_id)
    if snaps.empty:
        st.caption("No traction data yet. Run `signal snapshot github` or add publicly "
                   "stated numbers through signals.csv.")
        return
    metrics = list(dict.fromkeys(snaps["Metric"]))
    charts = st.columns(2)
    for i, metric in enumerate(metrics):
        frame = snaps[snaps["Metric"] == metric]
        label = data.HEADLINE_METRICS.get(metric, metric)
        unit = frame["Unit"].dropna().iloc[0] if frame["Unit"].notna().any() else None
        with charts[i % 2]:
            if len(frame) >= 2:
                st.plotly_chart(_metric_chart(frame, label, unit), width="stretch")
            else:
                row = frame.iloc[-1]
                st.metric(label, f"{row['Value']:,.0f} {unit or ''}".strip(),
                          help=f"Observed {row['Observed']:%Y-%m-%d}. One data point only.")
    with st.expander("All observations with sources"):
        st.dataframe(snaps, hide_index=True, width="stretch",
                     column_config={"URL": st.column_config.LinkColumn()})


# ------------------------------------------------------------------ add company


def add_page() -> None:
    st.title("Add a company")
    st.write("Leave anything you don't know blank. Blanks are stored as unknown.")
    p = manual
    with st.form("add-company", clear_on_submit=True):
        col1, col2 = st.columns(2)
        raw = {
            "name": col1.text_input("Company name *"),
            "domain": col2.text_input("Website"),
            "description": st.text_input("One-line description"),
            "sector": col1.text_input("Sector", value="AI Infrastructure"),
            "sub_sector": col2.text_input("Sub-sector"),
            "hq_country": col1.text_input("HQ country (2-letter code)"),
            "hq_city": col2.text_input("HQ city"),
            "launch_date": col1.text_input("Public launch date (YYYY-MM-DD)"),
            "github_org": col2.text_input("GitHub org"),
            "accelerator": col1.text_input("Accelerator"),
            "accelerator_batch": col2.text_input("Batch"),
            "notes": st.text_area("Notes"),
        }
        status = st.radio("Status", [s.value for s in TrackingStatus], index=1, horizontal=True)
        where = st.text_input("Where did you find it?", placeholder="YC Demo Day, Sep 2026")
        submitted = st.form_submit_button("Save company", type="primary")
    if not submitted:
        return
    try:
        values = {col: p.COMPANY_COLUMNS[col](val) for col, val in raw.items()}
    except p.RowError as exc:
        st.error(str(exc))
        return
    values["status"] = TrackingStatus(status)
    if not values["name"]:
        st.error("Company name is required.")
        return
    with db() as session:
        source = get_or_create_source(session, SourceKind.MANUAL,
                                      p.parse_str(where) or "Manual entry (dashboard)")
        company, outcome = p.upsert_company(session, values, source)
        name = company.name
    st.success(f"{name}: {outcome}. Add founders and rounds with `signal add-founder` "
               f"and `signal add-round`, or with CSV import.")


# ------------------------------------------------------------------ navigation

PAGES = {
    "feed": st.Page(discovery_page, title="Discovery feed", url_path="feed", default=True),
    "watchlist": st.Page(watchlist_page, title="Watchlist", url_path="watchlist"),
    "detail": st.Page(detail_page, title="Company detail", url_path="company"),
    "add": st.Page(add_page, title="Add company", url_path="add"),
    "archived": st.Page(archived_page, title="Archived", url_path="archived"),
}

if database_ready():
    st.navigation(list(PAGES.values())).run()
else:
    setup_screen()
