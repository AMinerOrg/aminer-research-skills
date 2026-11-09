# Changelog

## 0.4.9.dev23 — 2026-11-09 (unreleased standalone PR candidate)

- Default the indexed-publication-year allowance to one year, matching the
  ten-paper evaluation setting. With no review year supplied, the base remains
  the current year at run time; AMiner can return already indexed records with
  a following-year venue label. Keep `--publication-year-grace 0` for strict
  historical-year checks and label grace-admitted output.

## 0.4.9.dev22 — 2026-11-09 (unreleased standalone PR candidate)

- Default CLI and programmatic runs to the evaluated GLM-5.3 model and
  semantic preselection path. Keep explicit model profiles and add
  `--no-semantic-preselection` for the previous lexical path.
- Leave the year-grace setting, retrieval, ranking, evidence checks, and
  500/60/30 limits unchanged in dev22. The ten-paper dev21 run already used both new
  defaults explicitly; it is not an independent dev22 quality evaluation.

## 0.4.9.dev21 — 2026-11-06 (unreleased standalone PR candidate)

- Send host-aware `X-Platform`, `X-Skill-Name`, and `X-Skill-Version` on all
  AMiner search, relation, and Interface-B acquisition requests, matching the
  repository's Baseline Finder header convention. Retrieval and selection
  behavior remain the evaluated dev20 behavior.
- Register the independent Skill in the repository marketplace and catalog.

## 0.4.9.dev20 — 2026-11-06 (unreleased AMiner reference default)

- Default AMiner reference expansion to three verified manuscript-cited seeds,
  matching the evaluated dev16 configuration. Explicit 0 still disables it;
  external search providers retain their previous default of 0.
- Keep the relation strategy, candidate filters, 500/60/30 limits, model
  prompts, and API routing unchanged from the frozen dev16 source.

## 0.4.9.dev14 — 2026-11-04 (unreleased task evidence and year policy)

- Admit source-backed task/dataset precedents as extended reading, alongside
  mechanism and evaluation relationships. T/M/E anchors locate a facet without
  restricting its discussion use. Keep source quotes and limitations required.
- Default to an inclusive publication year; --review-year accepts YYYY and the
  legacy --review-as-of date selects its whole year. Preserve requested/effective
  boundaries in reports and traces, without fabricating candidate dates.
- Validate frozen candidate evidence offline; old runs and scores remain frozen.

## 0.4.9.dev13 — 2026-11-04 (unreleased query-contract repair)

- Target-based plans use query/search_terms and ignore the ambiguous legacy
  methods array, which a live planner filled with search-platform names.
- Preserve domain context for keyword discovery even when the model labels
  its role direct_baseline; named lookups require a title-shaped query.
- Report individual target-validation failures without guessing a replacement
  dimension or relaxing source evidence. Target-free legacy plans retain their
  named-method expansion behavior.
- Record failed selector calls as selection_unavailable, rather than counting
  every unassessed candidate as a deliberate model omission.
- Validate the saved dev12 planner response offline. No new retrieval or
  model call; dev12 partial/rate-limited results remain frozen.

## 0.4.9.dev12 — 2026-11-04 (unreleased balanced RW coverage)

- Ground explicit discussion targets in manuscript excerpts, and share the
  existing query budget across supported task, method, alternative, dataset,
  evaluation and finding directions instead of reserving it for mechanisms.
- Preserve complete domain qualifiers for ordinary keyword discovery; retain
  exact named titles. Record targets, skipped queries and coverage evidence.
- Separate a transfer facet's original M/E category from its mechanism or
  evaluation use, while keeping both source quotations and scope checks.
- Keep the 500/60/30 experiment limits, two retrieval rounds, cutoff,
  bibliography exclusions, evidence gates and cross-round retention.
- Preserve old target-free query plans through a visible legacy fallback.

## 0.4.9.dev11 — 2026-11-04 (unreleased same-domain field compatibility)

- Reuse an existing textual top-level reason when a supplied same-domain
  evidence object omits its equivalent relationship explanation; record its
  actual source without mutating the saved model response.
- Preserve anchor categories, both source-quote checks, cross-domain rules,
  relevance and hard eligibility boundaries. Do not generate evidence.
- Validate against immutable dev10 payloads offline, without paid calls,
  new retrieval, or changes to the frozen dev10 results.

## 0.4.9.dev10 — 2026-11-04 (unreleased anchor-schema repair)

- Give manuscript profile entries stable domain/task/mechanism/evaluation IDs
  so response labels can paraphrase the same anchor without false rejection.
- Keep source quotes, concrete explanations, cross-domain limitations, date,
  identity, already-cited, selection and retention gates. Invalid IDs fail.
- Require the supplied manuscript quote for cross-domain transfers even when
  the facet matches the profile; an anchor never substitutes for source text.
- Ask for narrower factual claims when source abstracts are truncated.
- Validate on saved dev9 candidate pools without new retrieval calls; do not
  represent selector-only runs as full-pipeline or fair-timing measurements.

## 0.4.9.dev9 — 2026-11-04 (unreleased query and evidence repair)

- Preserve full exact titles and planner-provided search terms when methods
  is empty, avoiding unintended query fragments and lost domain qualifiers.
- Use bounded section excerpts for query planning, coarse selection and final
  evidence checking, retaining late Related Work and evaluation material;
  subsection headings inherit their parent and omitted gaps remain explicit.
- Accept complete qualified domain/task labels and source-verified elided
  quotations. Missing transfer-profile facets need manuscript source evidence.
- Separate model omissions from downstream gate rejections in diagnostics.
- Count a domain-plus-specific-facet match as topical evidence without an
  additional title-word threshold. Reuse a source-verified evidence explanation
  when the model omits its duplicate top-level reason field.
- Keep AMiner-only routing, cutoff/citation/identity checks, budgets, concurrency
  and cross-round retention; no added model or search calls for these repairs.

## 0.4.9.dev8 — 2026-11-04 (unreleased retention experiment)

- Preserve source-verified selections across feedback reranking, subject to
  refreshed identity/date/bibliography checks and the shared Core-first cap of 30.
- Preserve original evidence and expose per-round and final merge decisions;
  no extra model/search calls or changes to selector concurrency.
- Validate through deterministic replay of saved ten-paper inputs and outputs;
  replay results are separate from fresh full-pipeline measurements.

## 0.4.9.dev7 — 2026-11-04 (unreleased report correction)

- Preserved assessed candidate records and a truthful partial status when
  candidate-pool IDs or retrieval responses are incomplete. Such runs are no
  longer described as having no semantically relevant candidates.
- Retrieval and selection behavior remain identical to the evaluated dev6
  snapshot; the ten-paper results remain attributed to dev6.

## 0.4.9.dev6 — 2026-11-04 (unreleased validation build)

- Fixed semantic-pool integration with compact already-cited control schemas;
  original citation evidence survives the downstream partition and ledger.
- Prevented initial-plus-surname author lines from prematurely closing the
  primary bibliography and hiding later references from citation exclusions.

## 0.4.9.dev5 — 2026-11-04 (unreleased recall experiment)

- Added opt-in semantic preselection over a bounded 500-record corpus before
  the final 60-candidate evidence selector, with explicit skipped-date, cap,
  invalid-ID and partial-failure diagnostics. No additional search API calls.

## 0.4.9.dev4 — 2026-11-04 (unreleased selector experiment)

- Made selection contribution-aware: benchmark and dataset papers include
  useful scope/evaluation comparisons; method and system papers include
  component alternatives. Retained concrete evidence and the shared cap of 30.

## 0.4.9.dev3 — 2026-11-04 (unreleased recall experiment)

- Added source-quoted same-domain evidence to rescue semantic relevance that
  lexical facet matching misses; rescued mismatches remain extended reading.
- Kept all cutoff, identity, self-match and already-cited checks independent
  from semantic evidence. Increased response capacity for the evidence fields.

## 0.4.9.dev2 — 2026-11-04 (unreleased recall experiment)

- Preserved discovery facets when named titles exceed the ten-query budget,
  excluded manuscript self-queries, and clarified primary-domain planning.
- Balanced the discovery pool across queries, retained domain qualifiers as
  ranking hints, and moved local cited controls outside the discovery budget.
- Retained publication months and explicit arXiv identifier month evidence,
  using conservative time windows without fabricating a day or moving cutoff.
- Preserved DOI/arXiv identifiers supplied by free AMiner metadata enrichment.

## 0.4.9.dev1 — 2026-11-04 (unreleased opt-in trial)

- Added one-step backward-reference expansion from at most three verified
  manuscript-cited seeds, with direction/provenance, separate identity checks,
  cost accounting, and the original cutoff/citation/selection gates.
- Retained structured bibliography authors and publication years for seed
  identity verification without guessing dates from title text.
- Added balanced batches of up to twenty candidates with one to three workers,
  unique global IDs, deterministic cross-batch merging, a shared thirty-result
  cap, and explicit partial status when a batch fails.
- Made private trace writes safe for concurrent model responses.
- Both features remain opt-in pending review of live pilot evidence.

## 0.4.9.dev0 — 2026-11-04 (unreleased evaluation build)

- Raised the PDF limit to 25 MiB, preselection to 80, semantic shortlist to 60,
  and shared output cap to 30; core candidates take priority, without padding.
- Gave selection manuscript context and retained AMiner abstracts that were
  previously discarded or overwritten by shorter enrichment responses.
- Added separately labeled cross-domain extended reading with grounded
  mechanism/evaluation evidence and explicit transfer limitations. Same-domain
  related tasks need not share the exact method to qualify as core work.
- Preserved date provenance and admissible same-title versions without
  fabricating dates or relaxing the historical review cutoff.
- Added private replay traces, unique candidate decisions, model token/finish
  diagnostics, truncation rejection, and source/version identity in every run.
- Retained the standalone AMiner routes and bounded two-round feedback flow.
  Live quality and cost comparisons are pending; this is not a published release.

## 0.4.8 — 2026-10-30

- Added deterministic target conservation for the feedback round. Every
  still-missing manuscript mechanism now receives one bounded controlled
  query even when the model omits that branch or the model planner fails.
- Aligned each mechanism with a minimal manuscript-grounded task/domain phrase
  recovered from its first-round query context; unrelated task wording no
  longer displaces a more specific setting.
- Raised the explicit feedback budget to eight queries, matching the maximum
  mechanism profile size, while retaining at most four model auxiliary
  suggestions. Deferred or unqueryable targets are reported rather than
  silently dropped.
- Added branch, origin, route, budget, and target-conservation diagnostics to
  feedback plans and AMiner retrieval traces.

## 0.4.7 — 2026-10-30

- Changed bounded mechanism-feedback planning to combine one or two words from
  an explicitly missing mechanism with the shortest qualified manuscript
  domain phrase, instead of substituting broad task or protocol wording.
- Added an explicit `controlled_mechanism_domain` query subtype. Only this
  deterministic feedback subtype and verified exact titles use Search Pro's
  documented fielded `title` parameter; ordinary discovery phrases continue
  to use relevance-ranked `query` search.
- Preserved the subtype in retrieval, downgrade, API-call, and candidate
  diagnostics so live routing decisions remain auditable.

## 0.4.6 — 2026-10-30

- Migrated free and Pro retrieval to the official AMiner Open Platform v1.5.0
  request contract. Free search now sends `query` plus `year_to`; Pro sends
  `query` plus `year_to` for discovery and retains fielded `title` only for a
  verified exact paper title.
- Compressed every non-title feedback query to two to five content words and,
  when available, joined the missing mechanism with the manuscript task rather
  than submitting long prose through title matching.
- Made the bounded `paper_list_by_keywords` rescue use two or three atomic
  terms from one coherent query, or skip the call when no safe theme exists;
  separate long themes are no longer combined as an accidental conjunction.
- Kept all retrieval on documented public AMiner endpoints with no automatic
  arXiv/OpenAlex fallback, and synchronized package, runtime, relation, and
  request-header versions at 0.4.6.

## 0.4.5 — 2026-10-30

- Added a manuscript-derived application/scientific-domain axis alongside
  task, mechanism, and data/evaluation protocol. Domain matching now requires
  coherent phrase-token coverage, so generic shared words cannot promote an
  otherwise cross-domain paper.
- Made deterministic gates authoritative over model ranking. A Core record now
  requires confirmed `uncited` status, non-empty abstract and ranking reason,
  in-domain same-task evidence, mechanism or protocol support, and a resolved
  admissible cutoff date.
- Disabled deterministic Top-10 filling. Only records explicitly selected by
  the semantic ranker and accepted by the hard gates are returned; Top-10 is a
  cap and eligible-but-unselected records remain diagnostic only.
- Excluded cutoff-year records whose exact date is unresolved from both Core
  and Extended output. They remain available for date-verification diagnostics
  unless a parsed local citation independently establishes admissibility.
- Split exact-ID and high-confidence local-bibliography matches into an
  `already_cited_controls` diagnostic before semantic selection. These works
  remain retrieval seeds and coverage controls but cannot be emitted as
  missing Related Work.
- Required every Core result to be bibliography-confirmed `uncited`; Extended
  results may retain an explicit `unknown` citation status, while
  `already_cited` is forbidden from both tiers.
- Based the bounded second-round trigger on confirmed-uncited Core and
  mechanism coverage after control removal. Feedback planning now uses cited
  neighborhoods plus manuscript mechanisms to find adjacent uncited work.
- Distinguished confirmed title matches (similarity at least 0.92) from
  ambiguous matches (0.78 to 0.92). Ambiguous records remain human-reviewable
  and cannot satisfy Core coverage or suppress the feedback round.
- Kept the two-round maximum, cutoff policy, public-AMiner-only boundary, and
  cross-domain hard gate; when no qualified uncited work remains, the report
  stays short and declares `insufficient_candidates`.
- Routed public relation-graph `target_references` through a dedicated
  known-cited control input, so they reach feedback and diagnostics without
  occupying the discovery shortlist or satisfying Core/stop conditions.
- Added a whole-ledger readiness gate on local citation matching and excluded
  `resolution_eligible: false` entries. Exact matches from an unready ledger
  remain provisional feedback seeds rather than confirmed citation evidence.
- Cross-matched discovery hits against known-cited controls before applying
  the Top-40 cap: exact AMiner ID first, then title similarity of at least 0.92
  only when a stable ID is absent. Duplicate cited hits can no longer enter
  Core or prematurely stop feedback retrieval.

## 0.4.3 — 2026-10-30

- Replaced broad first-round phrases with a manuscript-derived retrieval
  profile covering task, individual method components, and data/evaluation
  protocol; named works and mechanisms remain independent queries.
- Changed the second-round trigger from returned-record and role counts to
  verifiable Core coverage of those method components, while retaining the
  hard two-round maximum.
- Added task/mechanism/protocol alignment to shortlist ranking and hard gates,
  suppressing candidates that share only a generic mechanism in an unrelated
  application domain.
- Reused the parsed local bibliography during discovery. Exact locally cited
  works receive local relevance evidence, and a year-only cutoff-year record
  may be marked `admissible_via_local_citation`; external-only candidates keep
  the existing date policy.

## 0.4.2 — 2026-10-29

- Replaced the single conservative final list with a backward-compatible
  two-tier evaluation view: model-supported high-confidence `core` records and
  hard-gated medium-confidence `extended` records, capped at ten in total.
- Ranked a role-balanced 24-record shortlist with the model, then retained
  eligible unselected records through a deterministic role-balanced medium
  gate so a short model response no longer silently discards useful candidates.
- Made Top-10 a cap rather than a quota. Low-confidence records never enter the
  final list, and a shorter qualified list is explicitly marked
  `insufficient_candidates`.
- Added neutral `candidate_to_check`, tier, confidence, fill provenance,
  selection evidence, verification, and `missing_status` metadata. Extended
  records are never described as proven omissions.
- Split named methods and exact paper titles into independent searches, and
  replaced position-based query routing with content- and result-aware routing.
- Added one dynamically planned feedback-search round when initial technical
  category or role coverage is insufficient; the hard stop is two rounds.
- Changed the single keyword-batch trigger from raw hit count to prequalified
  candidates and qualified role coverage, while preserving one call maximum
  across both rounds.
- Preferred provider-supplied exact publication dates and hard-filtered records
  later than the full review cutoff without fabricating dates for year-only
  metadata.

## 0.4.1 — 2026-10-29

- Replaced the fixed free-search path with query-shape-aware AMiner routing:
  exact/short titles use free Search, controlled phrases use Search Pro, and
  natural-language questions use the available legacy QA endpoint.
- Corrected free Search to send only documented `title`, `page`, and `size`
  parameters; corrected Search Pro to use fielded literal parameters and never
  send a sentence or unsupported `query`/`year_to` fields.
- Kept QA Pro as an explicit capability mode. Permission/API failures degrade
  only inside AMiner through legacy QA and generated short literal phrases.
- Added one bounded multi-keyword expansion when the merged pool is small,
  batch `paper_info`, default top-five missing-abstract `paper_detail`, and
  complete request-shape, fallback-reason, response, and cost diagnostics.
- Changed query planning from six undifferentiated strings to six routed query
  plans while preserving compatibility with older string-query callers.

## 0.4.0 — 2026-10-29

- Made AMiner Open Platform the default candidate-search provider.
- Added free `paper_search` → batch `paper_info` retrieval, optional paid
  `paper_search_pro`, `paper_qa_search_pro`, and bounded `paper_detail`
  enrichment, with provider calls, estimated fees, and AMiner-only degradation
  recorded in every retrieval diagnostic.
- Kept arXiv and OpenAlex as explicit provider choices; AMiner failures no
  longer silently send manuscript-derived queries to an external provider.
- Added conservative PDF title → AMiner `pub_id` resolution. An exact title
  must be uniquely corroborated by available author and/or explicit manuscript
  year metadata before the ID is used.
- Replaced the internal bidirectional `pub_relation` call with the documented
  public `paper/relation?id=` endpoint. The Skill now promises only backward
  references and explicitly reports forward citations as unsupported.
- Kept year-only results from the cutoff year visible as
  `uncertain_same_year`; no synthetic month or day is assigned.

## 0.3.0

- Standalone Related Work audit with PDF and AMiner `pub_id` inputs, GROBID
  parsing, arXiv/OpenAlex discovery, and optional citation expansion.
