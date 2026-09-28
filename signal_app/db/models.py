"""SQLAlchemy models for Signal.

Design rules that every table follows:

1. **Missing is normal.** Pre-seed data is sparse. Almost every descriptive column is
   nullable, and ``NULL`` always means "we don't know", never "zero" or "no". Booleans
   are tri-state for the same reason: ``prior_exit = None`` (unknown) is a different
   fact from ``prior_exit = False`` (we checked, and there was no exit). Scoring code in
   Phase 2 reads these nulls to compute a confidence level instead of imputing values.

2. **Every fact has provenance.** Rows that hold facts carry a ``source_id`` (which
   source said it) and ``collected_at`` (when we recorded it). Round terms and
   snapshots also keep a ``source_url`` so a number can be traced to the page it came
   from, which matters when defending a valuation estimate.

3. **Signals are stored long, not wide.** ``SignalSnapshot`` has one row per metric per
   observation. New connectors add new metric names without schema changes, and
   velocity calculations become simple windowed queries over ``observed_at``.

Money is stored as whole US dollars in integer columns. Pre-seed rounds never need
cents, and integers avoid floating-point drift when we later compute dilution.
"""

from __future__ import annotations

import enum
from datetime import UTC, date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


def _enum(cls: type[enum.Enum]) -> SAEnum:
    # Stored as plain strings (not a native Postgres ENUM) so adding a value later
    # is a code change, not a migration.
    return SAEnum(cls, native_enum=False, validate_strings=True, length=32,
                  values_callable=lambda e: [m.value for m in e])


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------- enums


class SourceKind(enum.StrEnum):
    MANUAL = "manual"
    CSV = "csv"
    GITHUB = "github"
    HACKERNEWS = "hackernews"
    PRODUCTHUNT = "producthunt"
    EDGAR = "edgar"
    ACCELERATOR = "accelerator"
    JOB_BOARD = "job_board"
    WEB = "web"
    SEED = "seed"  # fictional demo data shipped with the repo


class CompanyStage(enum.StrEnum):
    PRE_SEED = "pre_seed"
    SEED = "seed"
    SERIES_A_PLUS = "series_a_plus"
    ACQUIRED = "acquired"
    DEAD = "dead"


class TrackingStatus(enum.StrEnum):
    DISCOVERED = "discovered"  # found by a connector or import, not yet reviewed
    WATCHLIST = "watchlist"  # actively tracked
    ARCHIVED = "archived"  # reviewed and set aside


class RoundType(enum.StrEnum):
    PRE_SEED = "pre_seed"
    SEED = "seed"
    SERIES_A = "series_a"
    BRIDGE = "bridge"
    OTHER = "other"


class Instrument(enum.StrEnum):
    SAFE_POST = "safe_post"  # YC post-money SAFE, the pre-seed default since 2018
    SAFE_PRE = "safe_pre"
    CONVERTIBLE_NOTE = "convertible_note"
    PRICED_EQUITY = "priced_equity"
    UNKNOWN = "unknown"


class CapType(enum.StrEnum):
    POST_MONEY = "post_money"
    PRE_MONEY = "pre_money"


class DataConfidence(enum.StrEnum):
    REPORTED = "reported"  # stated by the company, an investor, or a filing
    ESTIMATED = "estimated"  # inferred by us; the notes field must say how


# -------------------------------------------------------------------------- tables


class Source(Base):
    """Where a fact came from, e.g. "GitHub API" or "Manual: YC Demo Day, Sep 2026"."""

    __tablename__ = "source"
    __table_args__ = (UniqueConstraint("kind", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[SourceKind] = mapped_column(_enum(SourceKind))
    name: Mapped[str] = mapped_column(String(200))
    url: Mapped[str | None] = mapped_column(String(500))
    terms_note: Mapped[str | None] = mapped_column(Text)  # why this use is permitted
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    def __repr__(self) -> str:
        return f"<Source {self.kind}:{self.name}>"


class Company(Base):
    __tablename__ = "company"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    # Lowercased, without scheme or "www.". The strongest dedupe key we have.
    domain: Mapped[str | None] = mapped_column(String(253), unique=True)
    description: Mapped[str | None] = mapped_column(Text)

    sector: Mapped[str | None] = mapped_column(String(100), index=True)
    sub_sector: Mapped[str | None] = mapped_column(String(100))
    hq_country: Mapped[str | None] = mapped_column(String(2))  # ISO 3166-1 alpha-2
    hq_city: Mapped[str | None] = mapped_column(String(100))

    founded_date: Mapped[date | None] = mapped_column(Date)
    # First public launch (Show HN, Product Hunt, public repo). Traction metrics are
    # measured in 30/60/90-day windows from this date so companies are compared at the
    # same age, not the same calendar date.
    launch_date: Mapped[date | None] = mapped_column(Date)

    stage: Mapped[CompanyStage] = mapped_column(
        _enum(CompanyStage), default=CompanyStage.PRE_SEED
    )
    status: Mapped[TrackingStatus] = mapped_column(
        _enum(TrackingStatus), default=TrackingStatus.DISCOVERED, index=True
    )

    accelerator: Mapped[str | None] = mapped_column(String(100))
    accelerator_batch: Mapped[str | None] = mapped_column(String(50))  # e.g. "F25"

    github_org: Mapped[str | None] = mapped_column(String(100), index=True)
    github_repo: Mapped[str | None] = mapped_column(String(200))  # "owner/name", main repo
    hn_launch_item_id: Mapped[int | None] = mapped_column(Integer)

    discovery_source_id: Mapped[int | None] = mapped_column(ForeignKey("source.id"))
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    notes: Mapped[str | None] = mapped_column(Text)

    discovery_source: Mapped[Source | None] = relationship()
    founder_links: Mapped[list[CompanyFounder]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )
    rounds: Mapped[list[Round]] = relationship(
        back_populates="company", cascade="all, delete-orphan", order_by="Round.announced_date"
    )
    snapshots: Mapped[list[SignalSnapshot]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )

    @property
    def founders(self) -> list[Founder]:
        return [link.founder for link in self.founder_links]

    def __repr__(self) -> str:
        return f"<Company {self.name}>"


class Founder(Base):
    """A person. Structured fields feed the Founder Score in Phase 2.

    Each field maps to one scoring input so the score can report which inputs were
    missing. Pedigree fields (employers, schools) are kept as lists, not as a
    pre-computed "top-tier" flag, so the pedigree rule stays in configurable scoring
    code where its bias can be seen and adjusted.
    """

    __tablename__ = "founder"
    __table_args__ = (
        CheckConstraint("prior_startups_founded IS NULL OR prior_startups_founded >= 0"),
        CheckConstraint("domain_years IS NULL OR domain_years >= 0"),
        CheckConstraint("publications_count IS NULL OR publications_count >= 0"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)

    # Prior startup experience
    prior_startups_founded: Mapped[int | None] = mapped_column(Integer)
    prior_exit: Mapped[bool | None] = mapped_column(Boolean)
    early_employee_at_scaled_startup: Mapped[bool | None] = mapped_column(Boolean)

    # Domain expertise
    domain_years: Mapped[float | None] = mapped_column(Float)

    # Technical depth
    technical_background: Mapped[bool | None] = mapped_column(Boolean)
    github_username: Mapped[str | None] = mapped_column(String(100))
    publications_count: Mapped[int | None] = mapped_column(Integer)

    # Pedigree (modest, configurable weight in scoring; see README on bias)
    notable_employers: Mapped[list[str] | None] = mapped_column(JSON)
    schools: Mapped[list[str] | None] = mapped_column(JSON)

    founder_market_fit_notes: Mapped[str | None] = mapped_column(Text)
    profile_url: Mapped[str | None] = mapped_column(String(500))  # a page you read, not scraped

    source_id: Mapped[int] = mapped_column(ForeignKey("source.id"))
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    source: Mapped[Source] = relationship()
    company_links: Mapped[list[CompanyFounder]] = relationship(
        back_populates="founder", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Founder {self.name}>"


class CompanyFounder(Base):
    """Links founders to companies. Many-to-many so a repeat founder's earlier
    company and current company both point at the same person."""

    __tablename__ = "company_founder"

    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"), primary_key=True)
    founder_id: Mapped[int] = mapped_column(ForeignKey("founder.id"), primary_key=True)
    title: Mapped[str | None] = mapped_column(String(100))  # "CEO", "CTO"
    # Team completeness in Phase 2 checks for one technical and one commercial cofounder.
    role: Mapped[str | None] = mapped_column(String(20))  # "technical" | "commercial" | "other"
    is_cofounder: Mapped[bool] = mapped_column(Boolean, default=True)

    company: Mapped[Company] = relationship(back_populates="founder_links")
    founder: Mapped[Founder] = relationship(back_populates="company_links")


class Round(Base):
    """A financing round and its terms.

    For a post-money SAFE, ``valuation_cap`` with ``cap_type = post_money`` is the
    closest thing pre-seed has to a price: the investor's ownership at conversion is
    at least ``amount / cap``. Phase 3 anchors valuation on it when present and falls
    back to ``amount / assumed dilution`` when it is not.
    """

    __tablename__ = "round"
    __table_args__ = (
        CheckConstraint("amount_raised_usd IS NULL OR amount_raised_usd > 0"),
        CheckConstraint("valuation_cap_usd IS NULL OR valuation_cap_usd > 0"),
        CheckConstraint("discount_rate IS NULL OR (discount_rate >= 0 AND discount_rate < 1)"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"), index=True)

    round_type: Mapped[RoundType] = mapped_column(_enum(RoundType), default=RoundType.PRE_SEED)
    instrument: Mapped[Instrument] = mapped_column(_enum(Instrument), default=Instrument.UNKNOWN)

    amount_raised_usd: Mapped[int | None] = mapped_column(BigInteger)
    valuation_cap_usd: Mapped[int | None] = mapped_column(BigInteger)
    cap_type: Mapped[CapType | None] = mapped_column(_enum(CapType))
    discount_rate: Mapped[float | None] = mapped_column(Float)  # 0.20 = 20% discount
    pre_money_usd: Mapped[int | None] = mapped_column(BigInteger)  # priced rounds only
    post_money_usd: Mapped[int | None] = mapped_column(BigInteger)
    pro_rata_rights: Mapped[bool | None] = mapped_column(Boolean)

    announced_date: Mapped[date | None] = mapped_column(Date)
    closed_date: Mapped[date | None] = mapped_column(Date)
    lead_investor: Mapped[str | None] = mapped_column(String(200))
    investors: Mapped[list[str] | None] = mapped_column(JSON)

    confidence: Mapped[DataConfidence] = mapped_column(
        _enum(DataConfidence), default=DataConfidence.REPORTED
    )
    notes: Mapped[str | None] = mapped_column(Text)  # follow-on intent, side letters, caveats

    source_id: Mapped[int] = mapped_column(ForeignKey("source.id"))
    source_url: Mapped[str | None] = mapped_column(String(500))
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    company: Mapped[Company] = relationship(back_populates="rounds")
    source: Mapped[Source] = relationship()


class SignalSnapshot(Base):
    """One observation of one metric for one company at one point in time.

    ``observed_at`` is when the value was true; ``collected_at`` is when we fetched
    it. They differ when a connector reconstructs history, e.g. GitHub stargazer
    timestamps let us rebuild a star curve for a repo we only just discovered.

    Metric names are namespaced by source, e.g. ``github.stars``, ``hn.points``,
    ``manual.waitlist_size``.
    """

    __tablename__ = "signal_snapshot"
    __table_args__ = (
        UniqueConstraint("company_id", "metric", "observed_at", "source_id",
                         name="uq_snapshot_observation"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"), index=True)
    metric: Mapped[str] = mapped_column(String(100), index=True)
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(30))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    source_id: Mapped[int] = mapped_column(ForeignKey("source.id"))
    source_url: Mapped[str | None] = mapped_column(String(500))
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    raw_payload: Mapped[dict | None] = mapped_column(JSON)

    company: Mapped[Company] = relationship(back_populates="snapshots")
    source: Mapped[Source] = relationship()
