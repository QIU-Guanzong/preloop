# Full-repo review presets (architecture-strategy, code health, standards walk, docs currency)

These flow presets review a **whole repository** rather than a diff. They
complement the diff-scoped [Pull Request Reviewer preset](pull-request-review.md): PR review runs
on every change and stays cheap; these run rarely (per release, on a
schedule, or on demand), sample the repository deterministically, and
declare exactly what they covered. Each run is a single execution that
ends by writing `/workspace/result.json` with a versioned schema —
captured as a first-class execution artifact and retrievable via
`GET /api/v1/flows/executions/{execution_id}/result` — plus a
human-readable evidence pack under `/workspace/evidence/`.

| Preset | Lens | result.json schema |
| --- | --- | --- |
| Architecture and Strategy Conformance Review | Declared intent vs observed structure: conformance register over the repo's own architecture/mission/ADR declarations, drift findings (responsibility drift, dependency direction violations, undeclared load-bearing components, dead declared components, technology drift, non-goal violations) | `preloop.review.arch/v1` |
| Full Repo Code Health Review | Correctness risk, quality, performance hotspots, dead code, and test coverage **shape** over a sampled whole-repo pass, with a per-module health register | `preloop.review.codehealth/v1` |
| Standards Compliance Walk | Payload-named standards normalized into a requirement register (`met | gap | partial | declared` plus mandatory `not_checkable`) | `preloop.review.standards/v1` |
| Docs Currency Review | Is the README still true: five checkable claim types (entry points, services, dependencies, environment variables, build or run commands) extracted from the documentation and verified against the code, emitted as a drift list of (claim, where the doc says it, what the code shows) | `preloop.review.docscurrency/v1` |

They are a **family sharing one skeleton**, not one parameterized preset:
the four lenses have different required inputs, different failure modes
when inputs are missing, different result schemas, and different run
cadences — and the layered preset loader lets an installation override or
disable one lens without touching the others.

**Security posture is not re-reviewed here.** SBOM verification,
vulnerability matching, secrets hygiene, and CI hardening belong to the
[security audit presets](security-audit-presets.md) (referenced, not
duplicated). If a review pass trips over something security-shaped, it
files one referral finding pointing at that family — a `file:line`
pointer only, never a secret value — and moves on. The Standards
Compliance Walk marks security rows of a named standard
`covered_elsewhere: release-security-audit` instead of re-checking them.

**One-minute verdict cover.** Every review report leads with the same
one-page cover every [audit preset](security-audit-presets.md) requires
on its human-readable report (`audit-report.md`, `vuln-report.md`,
`dossier.md`), so nobody has to write a verdict summary by hand. The
cover sits at the top of `architecture-review.md`,
`code-health-report.md`, `standards-report.md`, or
`docs-currency-report.md`: a verdict sentence
first, then three labelled boxes (What we checked / What we did not
check / What you should do next week). Strictly one page. The cover may
only summarize findings already present in the body; the "What we did
not check" box is mandatory and may not be empty when anything was out
of scope (security always is, so that box always names the Release
Security Audit family at minimum). The machine `result.json` contract is
unchanged.

## The shared skeleton

All four presets follow the same guarantees:

- **Strictly read-only.** No issue creation, no comments, no commits, no
  pushes, no external mutation. `allowed_mcp_servers` and
  `allowed_mcp_tools` are both empty: a repo walk needs only the checkout
  and the sandbox, and every deliverable leaves through the artifact
  channel.
- **One repository per run.** Checkouts from the flow's git clone config
  live under `/workspace` (`target_repo_path` disambiguates when several
  exist), or the payload supplies `repository_url` for an anonymous
  read-only clone. Every `file:line` pointer refers to the recorded HEAD
  commit SHA.
- **Phased for cost.** A command-only inventory first (`git ls-files`,
  size/extension buckets, churn from `git log` name-only output), then a
  **deterministic sampling plan** (entry points, module boundary files,
  top-by-size and top-by-churn per module, everything under
  `focus_paths`, nothing under `exclude_paths`) — no random sampling, so
  consecutive runs stay comparable.
- **Declared coverage.** `result.json` carries a `coverage` block
  (`files_total`, `files_opened`, `plan_completed`, per-module sampling
  basis (`full_repo_searches` in the standards walk, which does not
  sample), `not_reviewed`). Absence claims are valid only for opened files
  or recorded full-repo searches, and `pass` requires the plan to have
  completed.
- **Budget knobs** (payload, all optional): `depth`
  (`quick | standard | deep` → 60 / 150 / 400 files opened),
  `max_files_opened`, `max_file_kb`, `focus_paths` / `exclude_paths`,
  plus a per-finding verification budget (at most 2 greps and 2 file
  reads) inherited from the Pull Request Reviewer.
- **Freeze-floor drift.** Deliver a previous run's `result.json`
  (`previous_result_path` in the seed, or a hygiene-checked URL) and the
  run classifies everything as new / persisting / resolved. Previous open
  items are a floor: each must reappear re-verified against the current
  checkout or be resolved with a reason and evidence; silently dropping
  one fails the `freeze_floor` check. No baseline delivered → `drift` is
  `null`; a baseline is never guessed.
- **Verdict honesty.** Verdicts (`pass | pass_with_findings | fail`) are
  computed only from open findings, open gap/partial rows, coverage, and
  the freeze floor. **The register can never upgrade a verdict**: `met`
  rows, `declared` rows, resolved items, and positive prose never raise
  it, and `declared` (a stated commitment the files cannot verify) is
  not a pass. `not_checkable` is required, never empty by assumption.
- **One-minute verdict cover.** The human-readable report artifact
  opens with a verdict sentence and the three-box cover described
  above, before any table or finding list. Additive on top of the
  existing report body; `result.json` is unchanged.
- **Evidence envelope.** Same `checks[]` (deterministic facts) vs
  `assessments[]` (marked judgment) split as the Observe/Eval and
  security presets; every artifact carries the line:

> Machine-generated review evidence. Not a certification, audit opinion,
> or legal advice.

Payload URLs are treated as hostile input, with the same hygiene rule as
the security presets: http(s) only, refusing loopback, private-range,
link-local, and cloud-metadata targets, with refused URLs recorded as
skipped inputs.

## Input contracts per lens

### Architecture and Strategy Conformance Review

Declared intent is attached or discovered — in order of precedence:

1. Payload `intent_docs`: workspace-relative paths (deliverable inline
   via the standard [`workspace_files` seed](../../webhook-triggers.md))
   or hygiene-checked URLs.
2. Repository conventions: `ARCHITECTURE.md`, `docs/architecture*`,
   `README.md` (head), `MISSION.md`, `STRATEGY.md`, `VISION.md`,
   `ROADMAP.md`, accepted ADRs under `docs/adr/` or `docs/decisions/`,
   `CONTRIBUTING.md` (head), and agent instruction files (`AGENTS.md`,
   `CLAUDE.md`).

Every declaration entering the conformance register carries a
`file:line` source pointer — a declaration the agent cannot point to
does not exist. **No intent docs is not a failure**: the run records "no
declared intent" as a gap row, marks conformance rows `not_checkable`,
still reports the observed architecture, and caps the verdict at
`pass_with_findings`. Purpose/fit commentary (does the code serve the
declared mission?) appears only in `assessments`, marked as judgment.

### Full Repo Code Health Review

Needs nothing beyond the checkout. It reads the project's own
conventions first (agent instruction files, README head, lint/formatter
configs) and judges the code by those, not generic taste. Five lenses:
correctness risk, quality, performance hotspots, dead code, and test
**shape** — the test map is derived from file layout (test-to-source
ratios, untested entry points) and is never presented as measured
coverage. Output includes a per-module health register (one row per
lens) and a findings ledger with stable ids
(`health:<lens>:<path>:<slug>`).

### Standards Compliance Walk

The payload must name the standards — the preset **refuses to run
without one** (guessing which standard applies would contaminate the
register):

```json
{
  "standards": [
    {"id": "styleguide", "name": "Example in-house style guide", "source": "docs/styleguide.md"}
  ],
  "depth": "standard",
  "previous_result_path": "previous/result.json"
}
```

`source` is inline text, a seeded workspace path, or a hygiene-checked
URL. Alternatively `"repo_declared": true` walks only the standards the
repository itself declares (lint configs, CONTRIBUTING rules, referenced
style guides, agent instruction files). With neither, the run ends with
an `error` verdict naming the missing input. Standards are normalized
into atomic requirements (`<standard>:R<n>`) with obligation levels
(`mandatory` vs `recommended`) taken from the standard's own wording; a
`gap` is an absence claim and must be backed by a recorded full-repo
search pattern, and requirements a repository cannot evidence
(organizational process, runtime behavior, personnel, hosted
infrastructure) land in `not_checkable`, never faked. Any `mandatory`
gap fails the run.

### Docs Currency Review

Needs nothing beyond the checkout, and takes `project_path` when the
project under review is one directory of a larger repository (everything
read, searched, and claimed is scoped to that subtree; `docs_paths`
overrides document discovery). It extracts claims from the project's
documentation and checks each one against the code. **Five checkable
claim types only** — `entry_point`, `service`, `dependency`, `env_var`,
`command` — because a sixth would turn the lens into a prose critic.
Everything else a document says is out of scope: no finding about
writing quality, tone, structure, or completeness is ever emitted, and
missing documentation is not drift (a claim that was never made cannot
be wrong). Statements skipped for being an unsupported claim type are
counted in `coverage.unsupported_statements_skipped`, never reported as
findings.

Each claim carries a `<path>:<line>` pointer into the document and the
**recorded search** that classified it (the exact pattern, the scope it
ran in, and the matches it returned), then lands in one of three
statuses:

| status | meaning | family grammar |
| --- | --- | --- |
| `holds` | the search found what the document says | `met` |
| `drifted` | the search was complete over its scope and the documented thing is absent, renamed, moved, or contradicted | `gap` |
| `not_checkable` | the checkout cannot decide it (hosted infrastructure, an operator-held credential, a tool installed elsewhere, a budget exhaustion), recorded with its reason | `not_checkable` |

`partial` and `declared` are not used by this lens, and a
`not_checkable` claim is never counted as holding. Severity applies to
drifted claims only: `high` when a reader following the document fails
immediately (a build or run command or an entry point that does not
exist), `medium` for a service, dependency, or environment variable the
code does not show, `low` when the documented thing still exists but
moved. Any high-severity drift fails the run; `pass` needs a completed
plan with nothing drifted and nothing `not_checkable`.

## Evidence pack layout

```
/workspace/evidence/
  inventory.json               # phase-1 command-only inventory (all four)
  findings.json                # machine-readable findings/register ledger (all four)
  drift-report.md              # only when a baseline was delivered (all four)
  architecture-review.md       # human-readable review; opens with the one-minute cover
  conformance-register.md      # declaration register (architecture-strategy)
  code-health-report.md        # human-readable review; opens with the one-minute cover
  health-register.md           # per-module register (code health)
  standards-report.md          # human-readable walk; opens with the one-minute cover
  requirements-register.md     # requirement register (standards walk)
  docs-currency-report.md      # human-readable review; opens with the one-minute cover
  claims-register.md           # claims register (docs currency)
```

`result.json` stays under 200 KB; long listings live in the pack and are
referenced from `artifacts`.

## Honest limits

- Coverage is sampled and declared, not total: a clean register row
  means "clean in the opened sample", and the `coverage` block is the
  scope of every claim.
- These are engineering reviews, not conformity assessments: no
  regime profile, no certification, and the standards walk checks only
  what a repository can show.
- The docs currency lens checks whether documentation is **true**, never
  whether it is well written, and it never edits a document: a clumsy
  but accurate README passes with zero findings, and documentation that
  simply says nothing about an area produces no claim at all.
- Freeze-floor enforcement is reported by the run and owned by
  downstream validation; the agent never self-grades the floor.
