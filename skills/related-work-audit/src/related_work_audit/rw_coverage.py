"""Manuscript-grounded RW coverage and bounded, dimension-balanced search.

Coverage is a retrieval diagnostic, never an output admission rule. A queried
role alone cannot establish that an accepted paper discusses that target.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any, Callable

from .evidence import normalize, profile_anchor, verified_quote, canonical_profile

DIMENSIONS = ("task", "method", "alternative", "dataset", "evaluation", "finding")
ROLES = dict(zip(DIMENSIONS, ("problem_setting", "method_mechanism",
    "alternative_formulation", "benchmark_or_evaluation",
    "benchmark_or_evaluation", "related_finding")))
STOPWORDS = {"a", "an", "and", "as", "at", "by", "for", "from", "in", "of", "on", "or", "the", "to", "with", "using"}
QUERY_TARGET_WORDS = 7
QUERY_WORD_LIMIT = 12


def attr(item: Any, field: str, default: Any = None) -> Any:
    return item.get(field, default) if isinstance(item, dict) else getattr(item, field, default)


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9][a-z0-9+-]*", normalize(text))


def source_targets(payload: dict[str, Any], context: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Keep targets with a valid primary-domain anchor and literal body quote."""
    payload = payload if isinstance(payload, dict) else {}
    raw_profile = payload.get("retrieval_profile")
    raw_profile = raw_profile if isinstance(raw_profile, dict) else {}
    profile = canonical_profile(raw_profile)
    raw = payload.get("coverage_targets") or []
    raw = raw if isinstance(raw, list) else []
    accepted, rejected, seen, contexts, repairs = [], [], set(), {}, []
    queries = payload.get("queries") if isinstance(payload.get("queries"), list) else []
    for index, row in enumerate(raw):
        if not isinstance(row, dict):
            rejected.append({"index": index, "reason": "not_an_object"})
            continue
        target_id = str(row.get("id") or "").strip().upper()
        dimension = str(row.get("dimension") or "")
        linked = [q for q in queries if isinstance(q, dict)
                  and str(q.get("target_id") or "").strip().upper() == target_id]
        if dimension not in DIMENSIONS:
            # Repair only a unique explicit facet, not a guessed semantic role.
            facets = {q.get("facet") for q in linked if q.get("facet") in DIMENSIONS}
            if len(facets) == 1:
                replacement = next(iter(facets))
                repairs.append({"id": target_id, "field": "dimension", "from": dimension,
                                "to": replacement, "basis": "unique_linked_query_facet"})
                dimension = replacement
        topic = " ".join(str(row.get("topic") or "").split())
        quote = str(row.get("source_quote") or "")
        # Resolve the model's original ordinal to its exact text before
        # canonical deduplication, so D2 cannot silently change meaning.
        source_domain_id = str(row.get("domain_id") or "").strip().upper()
        original_domains = raw_profile.get("domain_terms")
        original_domains = original_domains if isinstance(original_domains, list) else []
        domain = None
        if re.fullmatch(r"D[1-9][0-9]*", source_domain_id):
            index = int(source_domain_id[1:]) - 1
            raw_domain = original_domains[index] if index < len(original_domains) else None
            text = " ".join(raw_domain.split()) if isinstance(raw_domain, str) else ""
            if text and text in profile["domain_terms"]:
                canonical_id = f"D{profile['domain_terms'].index(text) + 1}"
                domain = profile_anchor(canonical_id, profile, ("domain_terms",))
        if not domain:
            # An explicit domain string / query can identify one existing anchor.
            # Never substitute the first accepted target's domain.
            context_texts = [str(row.get("domain_context") or "")]
            context_texts += [str(q.get("query") or "") for q in linked]
            matches = [d for d in profile["domain_terms"] if any(
                set(words(d)).issubset(set(words(text))) for text in context_texts if text)]
            if len(matches) == 1:
                canonical_id = f"D{profile['domain_terms'].index(matches[0]) + 1}"
                domain = profile_anchor(canonical_id, profile, ("domain_terms",))
                repairs.append({"id": target_id, "field": "domain_id", "from": source_domain_id,
                                "to": canonical_id, "basis": "unique_explicit_domain_phrase"})
        checks = {"valid_unique_id": bool(re.fullmatch(r"W[1-9][0-9]*", target_id)) and target_id not in seen,
            "known_dimension": dimension in DIMENSIONS, "searchable_topic": 2 <= len(words(topic)) <= 10,
            "valid_domain_anchor": bool(domain), "verbatim_source_quote": verified_quote(quote, context),
            "within_target_budget": len(accepted) < 12}
        if checks["valid_unique_id"]:
            # This is routing context only until all source checks pass.
            contexts[target_id] = {"domain_context": domain["text"] if domain else "",
                "dimension": dimension if dimension in DIMENSIONS else "",
                "status": "validated" if all(checks.values()) else "query_only_unvalidated_target"}
        seen.add(target_id)
        if not all(checks.values()):
            rejected.append({"id": target_id, "reason": "invalid_target_evidence_or_budget",
                "failed_checks": [name for name, passed in checks.items() if not passed]})
            continue
        accepted.append({"id": target_id, "dimension": dimension, "topic": topic,
            "source_quote": quote, "domain_id": domain["id"], "source_domain_id": source_domain_id,
            "domain_context": domain["text"],
            "priority": "primary" if row.get("priority") == "primary" else "supporting"})
    return accepted, {"status": "ready" if accepted else "unavailable_legacy_fallback",
        "accepted_count": len(accepted), "rejected_targets": rejected,
        "target_repairs": repairs, "query_target_contexts": contexts,
        "active_dimensions": list(dict.fromkeys(x["dimension"] for x in accepted))}


def scoped_phrase(phrase: str, domain: str, dimension: str = "") -> str:
    """Deduplicate a topic/domain without severing multiword search concepts.

    Seven words is a planner target, not permission to cut ``random number
    generation`` or ``earth mover distance`` in half. Oversized combinations
    return empty so callers can try the verified topic or record a deferral.
    """
    def terms(text):
        result, seen = [], set()
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9+.-]*", text):
            token = token.rstrip(".")
            key = token.casefold()
            if key and key not in STOPWORDS and key not in seen:
                result.append(token); seen.add(key)
        return result
    domain_terms = terms(domain)
    phrase_terms = terms(phrase)
    # Keep the complete query phrase in its original order. Appending only
    # missing domain terms avoids duplicating qualifications or breaking the
    # query's own contiguous named concepts.
    supplied = {x.casefold() for x in phrase_terms}
    merged = phrase_terms + [x for x in domain_terms if x.casefold() not in supplied]
    return " ".join(merged) if 2 <= len(merged) <= QUERY_WORD_LIMIT else ""


def balanced_targets(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One supported direction per pass; primary targets lead within it."""
    order = list(dict.fromkeys(x["dimension"] for x in sorted(targets, key=lambda x: x["priority"] != "primary")))
    buckets = {d: sorted((x for x in targets if x["dimension"] == d), key=lambda x: x["priority"] != "primary") for d in order}
    ordered = []
    while any(buckets.values()):
        for dimension in order:
            if buckets[dimension]:
                ordered.append(buckets[dimension].pop(0))
    return ordered


def attach_initial_targets(plans: list[Any], targets: list[dict[str, Any]],
                           diagnostics: dict[str, Any], query_type: Callable, limit: int = 10,
                           retrieval_profile: dict[str, Any] | None = None) -> list[Any]:
    profile = canonical_profile(retrieval_profile if retrieval_profile is not None
        else attr(plans[0], "retrieval_profile", {}) if plans else {})
    contexts = diagnostics.get("query_target_contexts", {})
    if not targets and not contexts:
        for plan in plans:
            plan.coverage_profile_diagnostics = diagnostics
        return plans[:limit]
    by_id = {x["id"]: x for x in targets}
    def named_lookup(plan):
        shape = attr(plan, "query_shape")
        return shape == "exact_title" or (shape == "title_phrase"
            and attr(plan, "role") in {"named_prior_work", "direct_baseline"})
    transformed = []
    transformations = []
    for plan in plans:
        target = by_id.get(attr(plan, "target_id", ""))
        fields = dict(vars(plan))
        fields.update(coverage_targets=targets, coverage_profile_diagnostics=diagnostics)
        text = str(plan)
        # Named work lookups keep their exact identity; discovery gets context.
        if not named_lookup(plan):
            context = contexts.get(attr(plan, "target_id", ""), {})
            domain = target["domain_context"] if target else context.get("domain_context", "")
            dimension = target["dimension"] if target else context.get("dimension") or attr(plan, "facet", "")
            raw_phrase = attr(plan, "fallback_phrase", "") or text
            if not domain:
                matches = [d for d in profile["domain_terms"]
                           if set(words(d)).issubset(set(words(raw_phrase)))]
                if len(matches) == 1:
                    domain = matches[0]
            text = scoped_phrase(raw_phrase, domain, dimension)
            repair_basis = "deduplicate_topic_and_domain"
            if not text and target:
                text = scoped_phrase(target["topic"], domain, dimension)
                repair_basis = "oversize_use_validated_topic"
            fields.update(query_shape="keyword_phrase", fallback_phrase=text, pro_field="keyword")
            if target:
                fields.update(role=ROLES[target["dimension"]], facet=target["dimension"])
            transformations.append({"target_id": attr(plan, "target_id", ""), "original": str(plan),
                "effective": text, "domain_context": domain, "dimension": dimension,
                "repair_basis": repair_basis,
                "target_status": "validated" if target else context.get("status", "unassigned_query"),
                "domain_status": "resolved" if domain else "unresolved_keep_query_context"})
        if text:
            transformed.append(query_type(text, **fields))
    named = [x for x in transformed if named_lookup(x)][:2]
    discovery = [x for x in transformed if not named_lookup(x)]
    chosen, seen = [], set()
    def add(plan):
        key = normalize(str(plan))
        if key and key not in seen and len(chosen) < limit:
            chosen.append(plan); seen.add(key)
    target_dispositions = []
    for target in balanced_targets(targets):
        if len(chosen) >= limit - len(named):
            target_dispositions.append({"target_id": target["id"], "status": "deferred_budget"})
            continue
        matching = next((p for p in discovery if attr(p, "target_id") == target["id"]
                         and normalize(str(p)) not in seen), None)
        if matching is None:
            # A duplicate first proposal must not hide a distinct backup; if
            # the planner omitted all proposals, the verified target itself is
            # a bounded search seed, without inventing a new discussion need.
            text = scoped_phrase(target["topic"], target["domain_context"], target["dimension"])
            matching = query_type(text, query_shape="keyword_phrase", fallback_phrase=text,
                role=ROLES[target["dimension"]], pro_field="keyword", facet=target["dimension"],
                retrieval_profile=profile,
                target_id=target["id"], coverage_targets=targets,
                coverage_profile_diagnostics=diagnostics,
                feedback_origin="initial_target_completion")
        before = len(chosen)
        add(matching)
        target_dispositions.append({"target_id": target["id"],
            "status": "queried" if len(chosen) > before else "invalid_query" if not str(matching) else "duplicate_query"})
    diagnostics["initial_target_dispositions"] = target_dispositions
    diagnostics["query_transformations"] = transformations
    diagnostics["query_word_limit"] = QUERY_WORD_LIMIT
    for plan in named + discovery:
        add(plan)
    final_ids = {attr(p, "target_id") for p in chosen}
    for disposition in target_dispositions:
        if disposition["target_id"] in final_ids:
            disposition["status"] = "queried"
    return chosen


def all_targets(queries: list[Any]) -> list[dict[str, Any]]:
    found = {}
    for query in queries:
        for target in attr(query, "coverage_targets", []) or []:
            found.setdefault(target["id"], target)
    return list(found.values())


def _supports_topic(topic: str, source: str) -> bool:
    # Conservative coverage hint. Failing it requests another search, never
    # removes a selected candidate. Only regular plural variants are folded.
    singular = lambda word: word[:-1] if len(word) > 4 and word.endswith("s") else word
    required = {singular(w) for w in words(topic) if w not in STOPWORDS}
    supplied = {singular(w) for w in words(source)}
    return bool(required) and required.issubset(supplied)


def related_work_coverage(selected: list[dict[str, Any]], queries: list[Any]) -> dict[str, Any]:
    targets = all_targets(queries)
    if not targets:
        return {"status": "unavailable", "policy": "legacy_technical_coverage"}
    eligible = [x for x in selected if x.get("missing_status") == "uncited"
                and x.get("relation_scope") == "same_domain"
                and (x.get("verification") or {}).get("relevance", {}).get("passed") is True]
    rows = []
    for target in targets:
        matches = []
        for item in eligible:
            source = f"{item.get('title', '')} {item.get('summary', '')}"
            source += " " + str((item.get("selection_evidence") or {}).get("abstract_excerpt") or "")
            if _supports_topic(target["topic"], source):
                matches.append({"title": item.get("title"), "tier": item.get("tier"), "basis": "accepted_same_domain_candidate_source_terms"})
        rows.append({**target, "status": "covered" if matches else "uncovered", "evidence": matches})
    primary = [x for x in rows if x["priority"] == "primary"] or rows
    dimensions = {dimension: {
        "expected": sum(x["dimension"] == dimension for x in rows),
        "covered": sum(x["dimension"] == dimension and x["status"] == "covered" for x in rows),
    } for dimension in dict.fromkeys(x["dimension"] for x in rows)}
    return {"status": "assessed", "policy": "balanced_rw_dimensions_v1", "targets": rows,
        "dimensions": dimensions, "verified_same_domain_uncited_count": len(eligible),
        "missing_target_ids": [x["id"] for x in rows if x["status"] == "uncovered"],
        "sufficient": all(x["status"] == "covered" for x in primary)
                      and all(x["covered"] > 0 for x in dimensions.values()),
        "limitation": "Source-term coverage is conservative and is not a usefulness score; role labels or query counts alone do not count as coverage."}


def balanced_feedback(*, original_queries: list[Any], selected: list[dict[str, Any]],
                      coverage: dict[str, Any], controls: list[dict[str, Any]],
                      review_as_of: str, llm_json: Callable, query_type: Callable,
                      maximum: int = 8) -> tuple[list[Any], dict[str, Any]]:
    targets = [x for x in coverage["targets"] if x["status"] == "uncovered"]
    ordered = balanced_targets(targets)
    if not targets:
        return [], {"status": "not_needed", "coverage_before_feedback": coverage, "query_count": 0}
    profile = attr(original_queries[0], "retrieval_profile", {}) if original_queries else {}
    prompt = f'''Return JSON only: {{"queries":[{{"target_id":"W1","search_terms":"one short query"}}]}}.
Plan at most {maximum} new scholarly keyword queries for the uncovered Related Work targets below.
First cover different supported directions, then spend remaining slots on important targets.
The source quotes establish what this manuscript discusses. Do not invent additional targets or
force a dataset/finding direction when the manuscript has no such target. Use one target_id per
query. Use one short topic and its task/domain, preferably at most {QUERY_TARGET_WORDS} content words in total.
The caller will deduplicate the domain qualifier. Do not stack method, dataset, metric and
application constraints into one query. Prefer a concise alternative terminology or a broader task-level phrase when an
original query already tried the obvious wording. Do not concatenate independent methods or
repeat an original query. Cited titles are neighborhood context only, never proposed omissions.
All work must be available on or before {review_as_of}. No more than two retrieval rounds are allowed.
Targets: {json.dumps(ordered, ensure_ascii=False)}
Original queries: {json.dumps([str(q) for q in original_queries], ensure_ascii=False)}
Accepted candidates: {json.dumps([{'title': x.get('title'), 'reason': str(x.get('selection_reason') or '')[:300]} for x in selected], ensure_ascii=False)}
Already-cited controls: {json.dumps([x.get('title') for x in controls[:12]], ensure_ascii=False)}'''
    failure = ""
    try:
        payload = llm_json("You plan a bounded, manuscript-grounded Related Work search.", prompt, max_tokens=1200)
    except Exception as exc:
        payload = {}; failure = type(exc).__name__
    model_plans = payload.get("queries", []) if isinstance(payload, dict) else []
    proposals = {}
    allowed = {x["id"] for x in targets}
    for row in model_plans if isinstance(model_plans, list) else []:
        if isinstance(row, dict) and row.get("target_id") in allowed:
            phrase = row.get("search_terms")
            if isinstance(phrase, list):
                phrase = " ".join(str(x) for x in phrase)
            if isinstance(phrase, str) and 2 <= len(words(phrase)) <= 12:
                proposals.setdefault(row["target_id"], []).append(phrase)
    original = {normalize(str(q)) for q in original_queries}
    cited = {normalize(str(x.get("title") or "")) for x in controls}
    plans, dispositions, seen = [], [], set()
    target_catalog = all_targets(original_queries)
    for target in ordered:
        status, reason = "deferred_budget", ""
        if len(plans) < maximum:
            suggestions = proposals.get(target["id"], []) or [target["topic"]]
            for index, phrase in enumerate(suggestions):
                reason = ""
                text = scoped_phrase(phrase, target["domain_context"], target["dimension"])
                if not text:
                    text = scoped_phrase(target["topic"], target["domain_context"], target["dimension"])
                    reason = "oversize_use_validated_topic"
                key = normalize(text)
                if not text:
                    status = "invalid_query"
                elif normalize(phrase) in cited:
                    status = "excluded_cited_title"
                elif key in original or key in seen:
                    status = "duplicate_query"
                else:
                    status = "queried"
                    if index and not reason:
                        reason = "later_model_alternative"
                    break
            if status == "queried":
                seen.add(key)
                plans.append(query_type(text, query_shape="keyword_phrase", fallback_phrase=text,
                    role=ROLES[target["dimension"]], pro_field="keyword", facet=target["dimension"],
                    feedback_origin="deterministic_target_completion" if reason == "oversize_use_validated_topic" else "model_targeted" if target["id"] in proposals else "deterministic_target_completion",
                    retrieval_profile=profile, target_id=target["id"], coverage_targets=target_catalog))
        dispositions.append({"target_id": target["id"], "dimension": target["dimension"], "topic": target["topic"], "status": status, "repair_basis": reason})
    counts = dict(Counter(x["status"] for x in dispositions))
    return plans, {"status": "planned" if plans else "no_new_query", "query_count": len(plans),
        "query_source": "balanced_rw_targets", "model_planner_status": failure or "completed",
        "coverage_before_feedback": coverage, "target_dispositions": dispositions,
        "query_targets": [{"query": str(q), "target_id": q.target_id, "dimension": q.facet, "origin": q.feedback_origin} for q in plans],
        "feedback_query_budget": {"policy": "round_robin_supported_dimensions_then_remaining_targets",
            "maximum_queries": maximum, "used_queries": len(plans), "remaining_queries": maximum-len(plans)},
        "target_conservation": {"total": len(targets), **counts, "conserved": sum(counts.values()) == len(targets)}}
