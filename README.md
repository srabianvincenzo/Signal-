# Signal

Signal is a research tool for finding and tracking **pre-seed** startups: companies
typically raising $250K to $2M, often on SAFEs, usually pre-revenue with one to five
people. It collects early founder and traction signals from public, permitted sources,
scores them with an explicit confidence level, estimates valuation ranges from round
terms, and turns the data into a weekly thesis brief.

It is built in phases. **This repository currently contains Phase 1**: the data model,
the connector framework, manual and CSV entry, the GitHub and Hacker News connectors,
and a fictional seed dataset of 20 AI infrastructure companies.

| Phase | Scope | Status |
|---|---|---|
| 1 | Schema, connector interface, manual/CSV entry, GitHub + HN connectors, seed data | Done |
| 2 | Snapshot scheduler, Founder and Traction Scores with confidence | Planned |
| 3 | Valuation estimator, dilution model, sensitivity table | Planned |
| 4 | Streamlit dashboard | Planned |
| 5 | Thesis brief generator, chart export, pick tracker | Planned |
| 6 | Product Hunt, SEC EDGAR, job boards, accelerator lists | Planned |

## Quickstart

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # add a GitHub token; SQLite is the default database

signal init-db                # create the schema (Alembic migrations)
signal seed                   # load the 20 fictional demo companies
signal list                   # see what's in the database
pytest                        # run the test suite
```

Everyday commands:

```bash
signal add-company                       # interactive; press Enter to skip any field
signal add-founder tensorfold.example    # by domain or name
signal add-round "Tensorfold"
signal template companies my_list.csv    # blank CSV with the right headers
signal import-csv companies my_list.csv  # also: founders, rounds, signals
signal discover github --days 30         # find young, fast-growing AI infra repos
signal discover hn --days 30             # Launch HN and Show HN posts
signal snapshot github                   # record current metrics for tracked companies
```

To use Postgres instead of SQLite, `pip install -e ".[postgres]"` and set
`DATABASE_URL=postgresql+psycopg://user:pass@host:5432/signal`. Nothing else changes.

## Design principles

**Missing data is normal.** Most pre-seed companies have no disclosed round terms, no
public repo, and a founder profile you assembled from a demo-day conversation. Signal
never fills gaps silently. A blank CSV cell becomes `NULL`, booleans are three-valued
(`True`, `False`, unknown), and the scores in Phase 2 report how many inputs were
actually present as a confidence level rather than pretending to know.

**Every fact has provenance.** Each founder, round and metric row stores the source it
came from and when it was collected, and round terms and metrics keep the URL they were
read from. When a number shows up in a valuation card or a brief, you can trace it.

**People over company metrics.** At pre-seed there is little company to measure, so
the model captures founder history in structured fields (prior startups, exits, years
in the domain, technical depth, team completeness) that Phase 2 will weight most
heavily.

**Discovery matters as much as tracking.** Connectors have a `discover` step that looks
for companies before they are well known, not just a `snapshot` step for companies
already on the list.

## Architecture

```
signal_app/
  config.py               settings from .env (no hardcoded keys)
  db/models.py            SQLAlchemy models (below)
  db/session.py           engine and session helpers
  ingest.py               dedupe + write path shared by every source
  connectors/
    base.py               Connector interface, rate-limited retrying HTTP client
    manual.py             CSV import and the parsers behind manual entry
    github.py             GitHub REST API
    hackernews.py         HN Firebase API + Algolia HN Search
  cli.py                  the `signal` command
migrations/               Alembic migrations
data/seed/                fictional demo dataset (companies, founders, rounds, signals)
data/templates/           blank CSV templates
tests/                    pytest suite; connectors are tested against mocked HTTP
```

### Data model

| Table | Holds | Notes |
|---|---|---|
| `source` | Where facts come from ("GitHub REST API", "Manual: YC Demo Day Sep 2026") | Includes a note on why the use is permitted |
| `company` | Identity, sector, launch date, accelerator, tracking status | `domain` is unique and the main dedupe key |
| `founder` | Structured founder history, pedigree lists, founder-market fit notes | One field per scoring input |
| `company_founder` | Links founders to companies with title and technical/commercial role | Many-to-many, so repeat founders link to every company |
| `round` | Instrument, amount, cap and cap type, discount, lead, date, confidence | Whole US dollars; `confidence` is reported or estimated |
| `signal_snapshot` | One metric value at one time (`github.stars = 412` on a date) | Long format; `observed_at` and `collected_at` kept separately |

`launch_date` matters more than `founded_date`: traction is measured in 30/60/90-day
windows after the first public launch so that companies are compared at the same age,
not on the same calendar date.

### Connectors

Every source implements `Connector` (`signal_app/connectors/base.py`):

- `discover(since)` returns `CompanyCandidate` objects for companies the source found.
- `snapshot(company)` returns `SignalObservation` objects for a company already tracked.
  Returning nothing is a normal result (plenty of pre-seed companies have no repo).

Connectors never write to the database. `ingest.py` matches candidates to existing
companies (domain, then GitHub org, then normalized name), fills only empty fields so a
connector never overwrites something you entered by hand, and skips duplicate
observations so re-running a job is safe.

HTTP goes through `RateLimitedClient`, which spaces requests to a per-source budget and
retries 429s, 5xx and network errors with exponential backoff, honouring `Retry-After`
and GitHub's `X-RateLimit-Reset` headers.

**GitHub** (discovery and tracking). Discovery searches configurable AI infrastructure
topics for repositories created in the last 90 days with at least 50 stars. Snapshots
record stars, forks, open issues, contributors, commits in the last 30 days, and issues
opened in the last 30 days by people outside the team (a usage signal that is harder to
fake than stars). For repos with up to 1,000 stars it also reconstructs the daily star
curve from stargazer timestamps, so a company discovered at day 60 still gets its
first-60-day history.

**Hacker News** (discovery and tracking). Discovery reads every Launch HN post and Show
HN posts above 20 points, extracting the product name, the linked domain or GitHub repo,
and the YC batch where the title includes one. Snapshots record the launch post's
points and comments and the total stories and points for the company's domain.

### Manual and CSV entry

This is expected to be the best source: companies from demo days, conversations and
newsletters. The CSV importer accepts `$1.5M`, `750k` or `1,500,000` for money; yes/no,
y/n or 1/0 for booleans; semicolons between list items; and blank or `unknown` for
anything you don't know. A bad row is reported with its line number and skipped while
the rest of the file imports, and importing the same file twice changes nothing. Pass
`--overwrite` to replace existing values instead of only filling blanks.

Manually entered traction numbers (`signals.csv`, e.g. a waitlist size from a blog
post) require a `source_url`, because an unsourced traction claim is not evidence.

## Seed data

`data/seed/` holds 20 **fictional** pre-seed AI infrastructure companies across
inference serving, evals, retrieval, agent tooling, GPU scheduling, fine-tuning,
observability, model routing, synthetic data, AI security and on-device AI. Names and
people are invented, and domains use the reserved `.example` TLD so they can never
collide with a real company. Round sizes and caps are set to plausible 2025–2026
pre-seed ranges.

The data is deliberately incomplete: some companies have no known round, some rounds
have no cap, many founder fields are blank, and only some companies have traction data.
That is what the real pipeline sees, and it exercises the confidence logic in Phase 2.

## Data sources and compliance

Signal only uses official APIs, public filings, or sources whose terms allow this use.
It does **not** scrape LinkedIn or X/Twitter; founder profiles are entered by hand from
information you have read yourself.

| Source | Access | Used for | Phase |
|---|---|---|---|
| Manual entry / CSV | You | Companies, founders, rounds, stated traction | 1 |
| GitHub | Official REST API with a personal token | Discovery, repo activity | 1 |
| Hacker News | Official Firebase API, public Algolia HN Search API | Discovery, launch reception | 1 |
| Product Hunt | Official API | Launches, upvotes | 6 |
| SEC EDGAR | Public Form D filings | Small raises | 6 |
| Greenhouse / Lever / Ashby | Public job board APIs | First hire | 6 |
| Accelerator directories | Public directories where terms allow | Batches and cohorts | 6 |

## Limitations

- **Sparse data.** Most pre-seed rounds are never announced, and most founders have no
  public track record you can verify. Scores will often be low-confidence, and that is
  the honest answer.
- **Survivorship bias.** Companies are found because they launched publicly, got
  upvoted, or raised from someone who announces deals. Quiet companies, and those that
  died before launch, are invisible, so any "hit rate" measured here overstates how
  predictable pre-seed outcomes are.
- **Source bias.** GitHub and Hacker News favour developer tools and open-source
  models. A strong enterprise or vertical-AI company can look weak on both. Stars can be
  bought, and HN points depend on posting time.
- **Form D is incomplete (Phase 6).** Many pre-seed rounds on SAFEs are never filed on
  Form D, or are filed late, so EDGAR under-counts pre-seed activity and cannot be
  treated as a census.
- **Pedigree bias (Phase 2).** Employer and school prestige correlate with access to
  capital more than with founder ability, and weighting them reinforces who already gets
  funded. The schema stores pedigree as raw lists so its weight can stay small,
  configurable and visible.
- **Fictional seed data.** The demo dataset is invented and says nothing about real
  markets.
