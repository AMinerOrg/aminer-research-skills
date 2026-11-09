"""Command-line interface for the standalone Related Work Skill."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from openai import APIError

from . import __version__
from .config import default_work_root, load_env_file, resolve_model_profile
from .pipeline import run_audit


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="related-work-audit",
        description=(
            "Audit time-bounded related-work coverage for one PDF or AMiner manuscript."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--pdf",
        help="Path to a local PDF manuscript (maximum 25 MiB).",
    )
    source.add_argument(
        "--pub-id",
        help="AMiner 24-character hexadecimal publication identifier.",
    )
    parser.add_argument(
        "--relation-pub-id",
        help=(
            "Optional verified AMiner publication ID for backward-reference "
            "expansion. PDF input otherwise attempts conservative title + "
            "author/year resolution; --pub-id input reuses its source ID."
        ),
    )
    parser.add_argument(
        "--search-provider",
        default="aminer",
        choices=("aminer", "arxiv", "openalex", "arxiv-openalex"),
        help=(
            "Candidate-search provider (default: AMiner). External providers "
            "are used only when explicitly selected; arxiv-openalex means an "
            "explicit arXiv-first/OpenAlex fallback chain."
        ),
    )
    parser.add_argument(
        "--aminer-search-tier",
        default="auto",
        choices=("auto", "free", "pro", "qa", "qa-pro"),
        help=(
            "AMiner search routing. auto selects free title search, Search Pro, "
            "or available legacy QA by query shape; qa-pro explicitly probes "
            "QA Pro and degrades only inside AMiner."
        ),
    )
    parser.add_argument(
        "--aminer-detail-limit",
        type=int,
        default=5,
        help=(
            "Maximum candidates to enrich with paid paper_detail when the free "
            "paper_info abstract slice is absent (default: 5)."
        ),
    )
    parser.add_argument(
        "--allow-pypdf-fallback",
        action="store_true",
        help=(
            "PDF only: explicitly allow lower-quality pypdf extraction when "
            "GROBID is unavailable or unusable."
        ),
    )
    parser.add_argument(
        "--review-year", "--review-as-of", dest="review_as_of",
        default=str(datetime.now(timezone.utc).year),
        help="Base review year, YYYY (default: current year); the year allowance may extend it. The legacy --review-as-of YYYY-MM-DD form also selects the entire year.",
    )
    parser.add_argument(
        "--publication-year-grace", type=int, choices=(0, 1), default=1,
        help="Allow the following indexed publication year (default: 1). Use 0 for a strict stated-year boundary; grace-admitted records are labeled in the report.",
    )
    parser.add_argument(
        "--model-profile",
        default="glm53",
        choices=("deepseek", "glm", "glm53", "glm53flash", "gpt"),
        help="Model provider profile (default: GLM-5.3).",
    )
    parser.add_argument("--env-file", action="append", default=[], help="Optional KEY=VALUE file; may be repeated.")
    parser.add_argument("--work-dir", help="Private run root; defaults to RELATED_WORK_AUDIT_WORK_DIR.")
    parser.add_argument("--output", help="Optional path for a copy of the complete JSON report.")
    parser.add_argument("--force", action="store_true", help="Allow replacing --output if it already exists.")
    parser.add_argument('--reference-seed-limit', type=int, choices=range(4), default=None,
                        help='One-step reference expansion from 0-3 verified cited seeds '
                             '(default: 3 with AMiner, 0 with external providers).')
    parser.add_argument('--selector-batch-size', type=int, choices=(0, 20), default=0,
                        help='Experimental balanced selector batches of at most 20; 0 keeps one request.')
    parser.add_argument('--selector-workers', type=int, choices=(1, 2, 3), default=1,
                        help='Concurrent selector batches; use 1 for the same-batch serial control.')
    parser.add_argument('--semantic-preselection', action=argparse.BooleanOptionalAction, default=True,
                        help='Use a semantic shortlist of up to 500 retrieved records before final 60-candidate selection (default: on).')
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.pub_id and args.allow_pypdf_fallback:
            raise ValueError("--allow-pypdf-fallback is only valid with --pdf.")
        if (
            args.pub_id
            and args.relation_pub_id
            and args.pub_id.strip().lower() != args.relation_pub_id.strip().lower()
        ):
            raise ValueError(
                "--pub-id input must reuse the same ID for citation relations; "
                "do not supply a different --relation-pub-id."
            )
        if args.aminer_detail_limit < 0 or args.aminer_detail_limit > 20:
            raise ValueError("--aminer-detail-limit must be between 0 and 20.")
        if args.search_provider != "aminer" and (
            args.aminer_search_tier != "auto" or args.aminer_detail_limit != 5
        ):
            raise ValueError(
                "AMiner tier/detail options require --search-provider aminer."
            )
        for env_file in args.env_file:
            load_env_file(Path(env_file))
        profile = resolve_model_profile(args.model_profile)
        work_root = Path(args.work_dir).expanduser().resolve() if args.work_dir else default_work_root()

        def progress(phase: str, message: str, metrics: dict[str, Any] | None, _artifacts: Any) -> None:
            rendered = f"[{phase}] {message}"
            if metrics:
                rendered += " " + json.dumps(metrics, ensure_ascii=False, sort_keys=True)
            print(rendered, file=sys.stderr, flush=True)

        manifest = run_audit(
            pdf=Path(args.pdf) if args.pdf else None,
            pub_id=args.pub_id,
            relation_pub_id=args.relation_pub_id,
            review_as_of=args.review_as_of,
            publication_year_grace=args.publication_year_grace,
            profile=profile,
            work_root=work_root,
            output=Path(args.output) if args.output else None,
            force=args.force,
            allow_pypdf_fallback=args.allow_pypdf_fallback,
            search_provider=args.search_provider,
            aminer_search_tier=args.aminer_search_tier,
            aminer_detail_limit=args.aminer_detail_limit,
            reference_seed_limit=args.reference_seed_limit,
            selector_batch_size=args.selector_batch_size,
            semantic_preselection=args.semantic_preselection,
            selector_workers=args.selector_workers,
            progress=progress,
        )
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, json.JSONDecodeError, httpx.HTTPError, APIError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
