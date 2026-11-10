---
name: related-work-audit
name_zh: 相关工作覆盖核查
description: Audit a PDF or AMiner-indexed manuscript for potentially missing related work using year-bounded AMiner search and optional verified backward-reference evidence. Use for related-work coverage questions; do not use for experimental-baseline checks, definitive novelty claims, or acceptance prediction.
homepage: https://github.com/AMinerOrg/aminer-research-skills/tree/main/skills/related-work-audit
metadata:
  version: "0.4.9.dev23"
---

# Related Work Audit

Use this standalone Skill to find prior-work candidates and check whether they
appear in a manuscript bibliography. It owns manuscript ingestion, retrieval,
bibliography segmentation, and coverage comparison; it does not import the
paper-review Demo, repository Core, or Baseline Skill.

For target-based retrieval plans, each query item is one scholarly query.
The caller uses AMiner; a legacy `methods` array cannot replace `query` or
`search_terms`. Coverage dimensions describe discussion needs, while roles
describe candidate/query use. Invalid targets record their failed checks. A unique explicit linked-query facet
or existing domain phrase can repair an enum/ordinal, but cannot bypass the
source quotation check. Queries belonging to an unvalidated target keep their
own recoverable context and are marked query-only; they do not become validated
coverage. Unresolved queries never inherit the first accepted target's domain.

Install the directory in an isolated Python 3.10+ environment:

```bash
python -m pip install /path/to/related-work-audit
```

Provide exactly one manuscript source:

```bash
# Defaults: AMiner candidate search, GLM-5.3, semantic preselection,
# and one indexed-year allowance beyond the current year.
related-work-audit --pdf /absolute/paper.pdf \
  --env-file /secure/review.env --output /private/related-work.json

# Explicit AMiner ID input. This full-text path requires Interface-B access.
related-work-audit --pub-id 0123456789abcdef01234567 \
  --review-year 2026 \
  --env-file /secure/review.env

# Explicit external provider; AMiner errors never trigger this silently.
related-work-audit --pdf /absolute/paper.pdf \
  --search-provider arxiv-openalex --review-year 2026 \
  --env-file /secure/review.env
```

A PDF must be non-empty and no larger than 25 MiB. A `pub_id` must be a
24-character hexadecimal AMiner identifier.

## Publication window

`--review-year YYYY` remains the base inclusive publication year; without it,
the base is the current year at run time. The default
`--publication-year-grace 1` also admits records indexed under the following
publication year, which can recover already discoverable preprints assigned a
later venue year. For example, `--review-year 2024` admits records through 2025.
Use `--publication-year-grace 0` for a strict stated-year boundary. The effective
year is used consistently for AMiner requests, local filtering and selection.
Reports retain the base year and allowance and label final records admitted
after the base year. AMiner indexing alone does not establish when a paper first
became publicly available, so grace-admitted records are candidates for author
review, not proven omissions at a historical review date. Compare results only
under the same publication window; do not relabel a wider-window run as strict
historical recall or infer a manuscript year when the caller supplied a year.

## Retrieval providers and cost

AMiner is the default. `--aminer-search-tier auto` first diagnoses the
manuscript's task, individual method components, and data/evaluation protocol,
then builds six to ten short complementary query items. Named methods and
accurate titles are searched individually rather than concatenated. Query
shape is inferred from each item's content. Empty results or weak title-overlap
hints can advance to the next documented AMiner route within the existing budget.
For discovery, every returned valid ID/title is retained, including zero-overlap
titles; overlap affects order and routing only. Exact full-title lookups still
require identity equality. The bounded semantic pool and evidence selector decide
relevance from title, abstract and manuscript context:

- exact titles and short title phrases → free AMiner v1.5 search
  `paper_search(query, year_to, page=1, size≤20)`; exact-title identity is
  enforced after retrieval rather than by the legacy `title` alias;
- controlled keyword/title phrases or bulk expansion →
  `paper_search_pro(query, year_to)` (¥0.01, `size≤100`); verified exact
  titles and the deterministic mechanism-plus-domain feedback subtype use its
  fielded `title` parameter;
- natural-language questions → available legacy `paper_qa_search` (¥0.05).

Preserve each explicitly supplied full title as one lookup, including commas.
An empty `methods` list does not override the planner's `search_terms`;
keep those terms when constructing the API search phrase.

The current organization token does not have QA-Pro permission. Select
`--aminer-search-tier qa-pro` only to use/probe that capability; a rejected or
empty call degrades inside AMiner to legacy QA, a generated short phrase for
Search Pro, and free title search. `free`, `pro`, and `qa` remain explicit
diagnostic modes. AMiner errors never trigger arXiv/OpenAlex automatically.

Query planning identifies the manuscript's discussion needs across tasks,
method/theoretical precedents, alternatives, datasets, evaluation methods, and
related findings or limitations. These are possible directions, not six quotas:
keep only targets supported by a checked literal manuscript quotation and a
valid primary-domain D anchor. The planner marks central targets as primary.
Up to 12 validated targets travel with the initial queries. The initial query
budget remains 10, including at most two named-work lookups.

The planner aims for one topic plus its task/domain in about seven content
words. The caller removes repeated terms and duplicate qualifiers without
cutting a multiword search concept. A combination over twelve content words
uses the validated short topic or another supplied proposal; unresolved or
duplicate queries are recorded rather than silently truncated. These discovery
and feedback phrases use AMiner keyword query routing. Exact named-work lookups keep
their original title. Distinguish dataset needs from evaluation methods when
both are supported. Source-grounded targets and the effective query strings
are saved in `retrieval_plan.json`.

After initial ranking, an admitted, confirmed-uncited same-domain candidate
can support a target only when its supplied title/abstract contains that
target's concept terms; core and extended candidates may both provide this
evidence. Query roles and retrieved counts alone cannot establish coverage.
This is a conservative retrieval hint, not a relevance or usefulness score.
Uncovered primary targets or a supported direction with no covered target
trigger the single feedback round. Its eight-query budget is shared in rounds
across supported directions, with primary targets first within each direction.
Mechanisms cannot reserve the whole budget before other directions. Repeated
queries and cited exact titles are skipped; deferred targets remain visible.
If a planner supplies no valid targets, the previous technical-coverage path
remains an explicit legacy fallback in `coverage_profile_diagnostics`.

Previously verified selections survive a later
shortlist omission or missing model evidence. Recheck their source identity,
publication date, and bibliography coverage; preserve the original relevance
evidence unless new facts invalidate it. Deduplicate the verified union, with
Core first and earlier verified order within each tier, under the shared cap
of 30. Reports expose retained records, exclusions, and cap evictions under
`cross_round_retention`; per-round decisions remain separate. This adds no
search or model calls. The hard stop is two total retrieval rounds.
Reports expose `related_work_coverage`, effective query targets, the eight-query
budget, and every target's queried/deferred/duplicate/cited disposition.

Exact identifiers and high-confidence local-bibliography title matches are
positive retrieval seeds and coverage controls, never missing-work outputs.
They are retained under `already_cited_controls` with their evidence. After
removing those controls, the coverage check may trigger the feedback round.
The planner uses cited neighborhoods plus manuscript discussion targets to
search for adjacent uncited work;
it cannot simply return the cited titles again.

Public `paper/relation` `target_references` are version-dependent discovery
hints. Recheck them against the actual input manuscript's ready bibliography.
Only exact identifiers or high-confidence local title matches become
`already_cited_controls`; external edges alone never exclude a candidate or
establish that the uploaded PDF cites it. Unmatched graph records join the normal
candidate pool, share its 500/60/30 limits, and must pass the same date, relevance,
evidence and bibliography checks. An unready local ledger leaves citation status
unknown. Neighbor expansion uses only locally confirmed cited seeds. External
reference hints remain visible in the report without overriding local coverage.

Final ranking checks four axes separately: application/scientific domain,
research task, method mechanism, and data/evaluation protocol. The domain
profile uses specific manuscript-derived phrases; incidental overlap on broad
words such as `sequence`, `recommendation`, or `memory` is not domain evidence.
A cross-domain candidate cannot pass merely because it shares task or mechanism
tokens. It may enter extended reading only with a specific task/mechanism/evaluation
facet supported by the manuscript profile or a verbatim manuscript passage,
a verbatim candidate title/abstract passage,
an explanation of the connection, and its domain limitations. These fields
ground a transfer hypothesis; they do not prove that the methods transfer.
Cross-domain work never becomes a core recommendation or a required baseline.
Date, identity, self-match, and already-cited exclusions still apply.

The default semantic bank is at most 500 records before the 60-record full
selector shortlist. The explicit lexical path uses an 80-record preselection
budget instead. Independent discovery queries share the available budget in rounds;
within each query, coherent primary-domain phrases help rank candidates.
Already-cited controls travel outside this discovery budget. Exact-title seeds
cannot crowd out separately planned task, mechanism, evaluation and alternative
queries, and the manuscript itself is excluded from query planning.

Selection receives up to 16,000 characters of manuscript section excerpts,
the manuscript profile, and up to 1,500 characters of each candidate abstract.
The bounded context reserves space for abstract/introduction, Related Work,
methods, and evaluation; a late Related Work section must not disappear solely
because the introduction is long. The semantic coarse pass uses the same
section selection under its 10,000-character budget. Source excerpts stay
verbatim and omitted gaps are marked, including nonadjacent sections of the
same category. Descriptive subsection headings inherit the enclosing category.
Search abstracts are retained when enrichment is absent or shorter. The model
output budget accommodates up to 30 reasoned selections; token-limited outputs
are recorded and retried, never silently accepted as complete rankings.

Lexical facet matches are fallible hints. A model may retain a same-domain
candidate that fails these lexical checks only by providing verbatim passages
from both the supplied manuscript and candidate, a complete domain or task label
from the manuscript profile (with optional extra qualifiers), and a specific
explanation of the relationship. A retrieval profile is not exhaustive: a
specific transfer facet outside its task/mechanism/evaluation lists requires a
verbatim manuscript passage as well as candidate evidence and limitations.
Exact excerpts and ordered quoted fragments separated by explicit ellipses
are checked against the supplied source; unsupported paraphrases do not pass.
When domain plus a specific task, mechanism, or protocol already match, no
additional title-word threshold is required. If a model puts its explanation
only inside verified domain/transfer evidence, reuse that explanation as the
selection reason and record its source. Neither rule emits unselected records.
These passages ground a judgment for reading, not proof of semantic correctness.
Such a lexical mismatch is retained as extended reading; the existing core
criteria and date/identity/already-cited exclusions continue to apply.

The selector identifies domain/task entries with stable `D`/`T` anchor IDs and
mechanism/evaluation entries with `M`/`E` IDs from the supplied profile catalog.
Human-readable labels may use different wording; an ID points to the original
profile entry without another string-equality test. Same-domain evidence accepts
only D/T IDs; transfer evidence accepts T/M/E IDs. Nonexistent IDs fail.
For transfer evidence, `basis` is task, mechanism or evaluation and describes
the candidate's concrete use, independently of the profile's T/M/E category.
Keep the original profile field in `profile_anchor` and the stated use in
`evidence.basis` for audit; do not relabel the profile to fit the response.
Both source quotations and the relationship explanation remain
required; IDs alone do not establish relevance. Legacy responses without IDs
retain the preceding complete-label validation. This is a response-schema
contract; task precedents and narrower datasets may inform scope comparisons without requiring an algorithm transfer. They remain extended reading when the setting differs.

For same-domain responses with an existing `domain_evidence` object, a missing
or blank `explanation` may reuse the model's existing textual top-level `reason`.
Both fields request the concrete relationship and Related Work purpose. Record
`explanation_source` as `reason` for this alias, preserve the original payload,
and keep the profile anchor and both source-quote checks. Never create missing
source evidence or replace an already supplied explanation through this alias.

Semantic preselection is enabled by default. It replaces the lexical
80-record boundary with semantic review of up to 500 deduplicated, time-eligible
discovery records using titles and 500-character abstract excerpts. It selects
at most 60 for the existing full-evidence selector. This adds no AMiner search
calls, but can add one model request per retrieval round (at most two rounds).
Already-cited and unresolved-date records remain outside this bank with their
diagnostics; larger banks retain an explicit cap-omission count. A failed
semantic request falls back to the existing ordered candidates and marks the
overall result partial. Use `--no-semantic-preselection` to select the lexical
path explicitly. The default model is GLM-5.3 with low reasoning; use
`--model-profile` to select another configured provider.
Partial retrieval preserves coverage checks on valid returned candidates; the
report does not describe those runs as having no semantically relevant records.

Judge usefulness by the manuscript's contribution. Benchmark/dataset papers
need prior-data and evaluation-scope comparisons, including work covering only
some abilities or shorter interactions. Method/system papers also need related
components and alternatives. A narrower/broader task within the same research
field is not automatically cross-domain; every suggestion still needs a
concrete discussion purpose and source-supported evidence.

Across both rounds, auto mode may make at most one bounded
`paper_list_by_keywords` call (¥0.10) with two or three atomic terms drawn
from one coherent short theme. It never combines several long query themes
into an accidental AND expression. The
trigger uses prequalified candidates and qualified role coverage, not raw hit
count, so a large noisy pool cannot suppress the rescue search. This broad
source is always subjected to publication-year cutoff filtering, deduplication, and
GLM relevance selection; it is not trusted as a final list.

All candidate IDs are batch-enriched with free `paper_info`. By default, at
most five candidates whose info card lacks an abstract may use `paper_detail`
(¥0.01 each); change this with `--aminer-detail-limit N`. Reports retain the
query plan and every API, documented parameter shape, route/fallback reason,
response status, and estimated CNY fee.
- `--search-provider arxiv`, `openalex`, or `arxiv-openalex` selects external
  retrieval explicitly. These providers are never a hidden fallback from
  AMiner.

For legacy query plans without grounded coverage targets, mechanism-feedback
queries are normalized to two to five
manuscript-grounded content words: one or two words from an explicitly missing
mechanism plus a minimal task/application-domain phrase aligned with that
branch's first-round query context. If no branch-local context exists, the
shortest qualified explicit domain phrase is used. This completion is
deterministic and still runs when the model planner omits a mechanism or fails.
Only this `controlled_mechanism_domain` subtype uses Pro's fielded `title`
parameter; ordinary keyword discovery continues to use `query`. Other
feedback phrases keep their ordinary routing, and long prose is never sent as
a fielded title query.

Prices and request shapes reflect AMiner Open Platform v1.5.0 documentation;
gateway billing is
authoritative.

Every AMiner gateway request includes `X-Skill-Name: related-work-audit` and
this package version. `X-Platform` detects the host (`claude-code`, `cursor`,
`codex`, or `openclaw`) and falls back to `unknown`. Set
`AMINER_SKILL_RUNTIME` only when detection needs an override.

## Manuscript identity and reference relations

The AMiner reference expansion used in the evaluated configuration is on by
default. The selector batching switch remains opt-in:

- `--reference-seed-limit 3` (AMiner default): rank manuscript-cited references by specific
  manuscript facet/title overlap and try at most three seeds. Each seed must
  already have a verified reference ID or pass exact-title plus bibliography
  author/year verification. Query each verified seed's backward references
  once; do not recurse. The resulting `reference_neighbor` records mean
  **the seed cites the candidate**, not that the uploaded manuscript cites it.
  Merge them with keyword results, then reapply cutoff, self-match, dedupe,
  manuscript bibliography matching, and relevance selection. They share the
  existing 500/60/30 semantic-bank, shortlist and output limits. Pass
  `--reference-seed-limit 0` to disable this additional search. External
  search providers continue to default to 0 because this expansion requires
  AMiner; passing a positive limit with an external provider is invalid.
- `--selector-batch-size 20 --selector-workers 3`: split the same role-balanced
  shortlist into up to three near-equal batches and evaluate concurrently.
  Use workers `1` with batch size `20` for the controlled serial comparison.
  Global candidate IDs stay unique. Merge in a fixed order, prioritize core
  evidence, and interleave batch ranks within equal tiers before the shared
  30-result cap; there is no ten-result quota per batch. A failed batch is
  reported as partial selection. Default batch size `0` keeps the original
  single request. Parallelism repeats manuscript context and may cost more
  tokens than a single request even when it reduces wall time.

Inspect `retrieval_diagnostics.reference_expansion`, `selector_batching`, and
the private trace for seed identities, citation paths, API cost, batch times,
and failures. Do not infer a forward-citation capability from this expansion.

For PDF input with the default AMiner provider, the Skill searches the exact
parsed title with free `paper_search`, enriches exact-title results with
`paper_info`, and accepts a `pub_id` only when one candidate is uniquely
corroborated by the author and/or explicit manuscript year available in the
PDF. A title alone, a metadata conflict, or multiple verified candidates never
activates a relation graph. Inspect private `identity_resolution.json`.

`--relation-pub-id` may supply an already verified ID for a PDF. With
`--pub-id`, the same source ID is reused and a different relation ID is
rejected.

The Skill calls only the documented public relation endpoint:

`GET /gateway/open_platform/api/paper/relation?id=<pub_id>`

It returns papers cited by the target—backward references only. The Skill uses
these records as version-dependent discovery hints and batch-enriches
their IDs with free `paper_info`. It does **not** claim to retrieve papers that
cite the target. Forward citations require a different documented capability;
the former internal bidirectional `pub_relation` endpoint is not used.

## Input quality and credentials

- PDF is GROBID-required by default. Use `--allow-pypdf-fallback` only when a
  lower-quality run is explicitly acceptable.
- `--pub-id` retains the v0.3 Interface-B full-text adapter. It requires
  permission for AMiner extracted files and is not presented as a general
  public Open Platform endpoint. PDF is the portable open-release input.
- Configure `AMINER_API_KEY` for default AMiner search, automatic identity
  resolution, relation lookup, or `--pub-id` input.
- Configure only the selected model key: `DEEPSEEK_API_KEY`, `GLM_API_KEY`, or
  both `GPT_API_KEY` and an explicitly trusted `GPT_BASE_URL`.
- `GROBID_URL` defaults to `http://127.0.0.1:8070`. Optional `HTTPS_PROXY` or
  `HTTP_PROXY` applies to configured network providers.
- Generic variables such as `API_KEY`, `LLM_API_KEY`, and `LLM_BASE_URL` are
  not accepted. Process environment values take priority over repeated
  `--env-file` entries. Never commit credentials.

## Interpretation and retained evidence

The product filters by indexed publication year. Without `--review-year`, the
base is the current UTC year and the default one-year allowance admits currently
retrievable records labeled with the following year. A paper in the effective
year is eligible even if AMiner supplies no month or day; records after the
effective year are excluded, and missing/invalid years still need verification.
Month/day metadata is retained as evidence but does not impose an intra-year
cutoff. Eligibility by year does not establish availability on a particular
earlier day.

The legacy `--review-as-of YYYY-MM-DD` spelling remains accepted and now selects
the whole stated year. The runner normalizes it to December 31 for all search,
relation, filter, selector and retention steps, and records
`publication_cutoff_policy: inclusive_year`, `publication_cutoff_year`, the
requested input and the effective boundary. This does not invent a publication
date: a year-only candidate stays `published: YYYY`, `date_precision: year`.
Past evaluations with an exact-day cutoff must use their frozen snapshots;
their results cannot be silently relabeled as year-based results.

Search/info/detail publication evidence is retained separately. An explicit
arXiv identifier already supplied in AMiner metadata may establish an earlier
preprint year/month without calling arXiv. No time is decoded from an AMiner ID
or an arbitrary DOI. Earlier preprint evidence is retained alongside later
venue metadata, with the original source and precision. It does not establish
which revisions were public then. This is not an external search fallback.

Result labels are conservative:

- `covered`: an exact identifier or high-confidence title match exists;
- `ambiguous_match`: a partial title match needs human resolution;
- `candidate_to_check`: an unmatched candidate for manual review, not proof of
  an omission;
- `not_assessed`: input, bibliography, or retrieval did not pass its quality
  gate.

The evaluation view contains at most 30 records; 30 is a cap, not a
quota. Candidates are reported in two tiers:

- `core` / `high`: selected with a non-empty evidence-grounded reason, backed
  by a non-empty abstract, confirmed `uncited` against a reviewable local
  bibliography, in-domain with a specific task, mechanism, or evaluation
  connection, and admitted by a resolved cutoff check. A related task or
  alternative approach need not share the exact experimental mechanism;
- `extended` / `medium`: explicitly selected by the semantic ranker as a
  plausible exploratory record and also passed deterministic metadata, date,
  identity, role, and relevance gates. Cross-domain reading additionally
  requires the grounded transfer evidence described above and carries
  `relation_scope: cross_domain_transfer` and `transfer_evidence`. Citation
  status may remain `unknown`, but it can never be `already_cited`.

If fewer than 30 records meet those standards, the diagnostics report
`insufficient_candidates` rather than padding the list with unselected or
low-confidence records. Top-30 is an upper bound, never a target. Core records
take priority under the shared cap. Eligible but
unselected shortlist entries remain only in private selection diagnostics.
Every returned record carries `candidate_label:
candidate_to_check`, `tier`, `confidence`, `selection_evidence`, `verification`,
source, and publication-date metadata. `missing_status` means only:
`already_cited` for a verified bibliography/relation match, `uncited` for no
match in a reviewable parsed bibliography, or `unknown` when coverage is not
resolvable. `already_cited` records appear only in the separate controls
diagnostic, never in the missing-work `records` list. Neither `uncited` nor the
`extended` tier proves that citing the paper is required.

Before sending an unpublished manuscript, confirm that the selected model,
AMiner, GROBID host, proxy, and any explicitly selected external provider may
receive the relevant content or generated queries.

The CLI prints a manifest and keeps the complete private report under
`~/.local/share/related-work-audit/runs/<run_id>/` by default. Inspect
`source_parse.json`, `identity_resolution.json`, `relation_graph.json` when
present, `reference_ledger.json`, and the report's `retrieval_diagnostics`.
The private `audit_trace.jsonl` (mode 0600) retains `search_responses`: a
bibliographic projection of every parsed search-response row before identity
or title filtering, including provider rank, supplied abstract fields, date
evidence, overlap hint and disposition. Headers are excluded. This is not a
byte-for-byte HTTP capture. It also retains normalized AMiner records per round,
filter dispositions, model requests/responses, token usage, finish
reasons, and one final selector disposition per candidate. It contains
manuscript content and must be treated like the manuscript; credentials are
excluded/redacted. Run metadata identifies the standalone entry point, version,
source SHA-256, and the active limits so evaluation snapshots can be verified.
Distinguish model nonselection from rejection of a model-selected candidate.
Failed metadata checks on an unselected record remain diagnostic facts, not
evidence that a hard gate caused the model's omission.
Failures may leave partial private artifacts; delete that specific run after
diagnosis when retention is not required.
