from __future__ import annotations

import ast
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from related_work_audit import __version__

from related_work_audit.aminer import (
    AMINER_EXTRACTED_FILES_URL,
    content_list_context,
    download_aminer_source,
)
from related_work_audit.aminer_search import (
    AMINER_PAPER_INFO_URL as SEARCH_PAPER_INFO_URL,
    AMINER_PAPER_LIST_BY_KEYWORDS_URL,
    AMINER_PAPER_QA_SEARCH_URL,
    AMINER_PAPER_QA_SEARCH_PRO_URL,
    AMINER_PAPER_SEARCH_PRO_URL,
    AMINER_PAPER_SEARCH_URL,
    resolve_title_to_pub_id,
    retrieve_aminer_records,
)
from related_work_audit.cli import build_parser, main
from related_work_audit.config import (
    ModelProfile,
    aminer_api_key,
    load_env_file,
    resolve_model_profile,
)
from related_work_audit.coverage import assess_related_work_coverage
from related_work_audit.grobid import prepare_uploaded_pdf_source
from related_work_audit.identity import detect_skill_runtime
from related_work_audit.model import llm_json
from related_work_audit.parsing import MarkdownParseResult
from related_work_audit.planning import (
    RoutedQuery,
    build_feedback_queries,
    build_related_work_queries,
    select_relevant_records,
)
from related_work_audit.pipeline import (
    _retrieval_reference_inputs,
    _structured_location_metadata,
    run_audit,
)
from related_work_audit.reference_pdf import build_reference_ledger_for_pdf
from related_work_audit.references import build_reference_ledger
from related_work_audit.relation import (
    AMINER_PAPER_INFO_URL,
    AMINER_PAPER_RELATION_URL,
    fetch_aminer_relation_context,
)
from related_work_audit.retrieval import (
    _exclude_known_control_matches,
    filter_retrieval_records,
    retrieve_related_work,
    technical_coverage_summary,
)


class StandaloneBoundaryTests(unittest.TestCase):
    def test_package_has_no_forbidden_imports(self) -> None:
        package = Path(__file__).resolve().parents[1] / "src" / "related_work_audit"
        forbidden = {"paper_review_core", "deepreview", "demo_evidence_reviewer"}
        for source in package.glob("*.py"):
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots = {alias.name.split(".", 1)[0] for alias in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module:
                    roots = {node.module.split(".", 1)[0]}
                else:
                    continue
                self.assertTrue(forbidden.isdisjoint(roots), f"forbidden import in {source}: {roots}")

    def test_cli_supports_exactly_one_pdf_or_pub_id_source(self) -> None:
        parser = build_parser()
        option_strings = {item for action in parser._actions for item in action.option_strings}
        self.assertIn("--pdf", option_strings)
        self.assertIn("--pub-id", option_strings)
        self.assertIn("--allow-pypdf-fallback", option_strings)
        self.assertIn("--relation-pub-id", option_strings)
        self.assertIn("--search-provider", option_strings)
        self.assertIn("--aminer-search-tier", option_strings)
        self.assertIn("--aminer-detail-limit", option_strings)
        defaults = parser.parse_args(["--pub-id", "a" * 24])
        self.assertEqual(defaults.search_provider, "aminer")
        self.assertEqual(defaults.model_profile, "glm53")
        self.assertTrue(defaults.semantic_preselection)
        self.assertEqual(defaults.publication_year_grace, 1)
        overrides = parser.parse_args([
            "--pub-id", "a" * 24, "--model-profile", "deepseek",
            "--no-semantic-preselection", "--publication-year-grace", "0",
        ])
        self.assertEqual(overrides.model_profile, "deepseek")
        self.assertFalse(overrides.semantic_preselection)
        self.assertEqual(overrides.publication_year_grace, 0)
        with self.assertRaises(SystemExit):
            parser.parse_args([])
        with self.assertRaises(SystemExit):
            parser.parse_args(
                ["--pdf", "/tmp/paper.pdf", "--pub-id", "a" * 24]
            )
        self.assertEqual(parser.parse_args(["--pub-id", "a" * 24]).pub_id, "a" * 24)
        self.assertFalse(any(action.__class__.__name__ == "_SubParsersAction" for action in parser._actions))

    def test_cli_turns_provider_http_error_into_clean_exit(self) -> None:
        stderr = io.StringIO()
        environment = {
            "GLM_API_KEY": "test-key",
            "GLM_BASE_URL": "https://example.invalid/v1",
        }
        with (
            patch.dict(os.environ, environment, clear=False),
            patch("related_work_audit.cli.run_audit", side_effect=httpx.ConnectError("provider offline")) as mock_run,
            redirect_stderr(stderr),
        ):
            status = main(["--pdf", "/tmp/not-used-because-run-is-mocked.pdf"])
        self.assertEqual(status, 2)
        self.assertEqual(mock_run.call_args.kwargs["profile"].name, "glm53")
        self.assertTrue(mock_run.call_args.kwargs["semantic_preselection"])
        self.assertEqual(mock_run.call_args.kwargs["publication_year_grace"], 1)
        self.assertIn("error: provider offline", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_cli_rejects_pub_id_with_pdf_only_fallback_before_configuration(self) -> None:
        stderr = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), redirect_stderr(stderr):
            status = main(
                ["--pub-id", "a" * 24, "--allow-pypdf-fallback"]
            )
        self.assertEqual(status, 2)
        self.assertIn("only valid with --pdf", stderr.getvalue())
        self.assertNotIn("not configured", stderr.getvalue())

    def test_cli_rejects_conflicting_relation_id_before_configuration(self) -> None:
        stderr = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), redirect_stderr(stderr):
            status = main(
                [
                    "--pub-id",
                    "a" * 24,
                    "--relation-pub-id",
                    "b" * 24,
                ]
            )
        self.assertEqual(status, 2)
        self.assertIn("must reuse the same ID", stderr.getvalue())
        self.assertNotIn("not configured", stderr.getvalue())


class ModelRoutingTests(unittest.TestCase):
    def test_invalid_json_retry_uses_complete_core_instruction(self) -> None:
        requests: list[dict[str, object]] = []
        responses = iter(("not json", '{"status": "ok"}'))

        class FakeCompletions:
            def create(self, **request):
                requests.append(request)
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content=next(responses))
                        )
                    ]
                )

        class FakeOpenAI:
            def __init__(self, **_kwargs):
                self.chat = SimpleNamespace(completions=FakeCompletions())

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        profile = ModelProfile(
            "deepseek",
            "DeepSeek",
            "test-model",
            "test-key",
            "https://example.invalid/v1",
        )
        with (
            patch("related_work_audit.model.proxy_url", return_value=None),
            patch("related_work_audit.model.OpenAI", FakeOpenAI),
        ):
            self.assertEqual(
                llm_json(profile, "system", "user", max_tokens=100),
                {"status": "ok"},
            )

        retry_instruction = (
            "\n\nYour previous response was not valid JSON. Return one complete JSON object only, "
            "with no Markdown fence, commentary, or trailing text. Keep every string concise. "
            "Do not omit required keys or close the response early."
        )
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0]["messages"][1]["content"], "user")
        self.assertEqual(
            requests[1]["messages"][1]["content"],
            "user" + retry_instruction,
        )

    def test_deepseek_ignores_cross_provider_credentials_and_uses_official_base(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "wrong-provider-key",
                "API_KEY": "generic-key",
                "LLM_API_KEY": "generic-llm-key",
                "OPENAI_BASE_URL": "https://wrong.example/v1",
                "BASE_URL": "https://generic.example/v1",
                "LLM_BASE_URL": "https://generic-llm.example/v1",
                "AGENT_MODEL": "wrong-model",
            },
            clear=True,
        ):
            with self.assertRaises(ValueError):
                resolve_model_profile("deepseek")

            os.environ["DEEPSEEK_API_KEY"] = "deepseek-test-key"
            profile = resolve_model_profile("deepseek")
            self.assertEqual(profile.api_key, "deepseek-test-key")
            self.assertEqual(profile.base_url, "https://api.deepseek.com")
            self.assertEqual(profile.model, "deepseek-chat")

    def test_glm_profiles_accept_only_glm_credentials(self) -> None:
        with patch.dict(
            os.environ,
            {
                "LLM_API_KEY": "wrong-provider-key",
                "LLM_BASE_URL": "https://wrong.example/v1",
            },
            clear=True,
        ):
            for name in ("glm", "glm53", "glm53flash"):
                with self.subTest(profile=name), self.assertRaises(ValueError):
                    resolve_model_profile(name)

            os.environ["GLM_API_KEY"] = "glm-test-key"
            for name in ("glm", "glm53", "glm53flash"):
                with self.subTest(profile=name):
                    profile = resolve_model_profile(name)
                    self.assertEqual(profile.api_key, "glm-test-key")
                    self.assertEqual(
                        profile.base_url,
                        "https://open.bigmodel.cn/api/paas/v4",
                    )

    def test_gpt_requires_both_gpt_key_and_explicit_base_url(self) -> None:
        with patch.dict(os.environ, {"GPT_API_KEY": "gpt-test-key"}, clear=True):
            with self.assertRaises(ValueError):
                resolve_model_profile("gpt")

            os.environ["GPT_BASE_URL"] = "https://trusted-gpt.example/v1"
            profile = resolve_model_profile("gpt")
            self.assertEqual(profile.api_key, "gpt-test-key")
            self.assertEqual(profile.base_url, "https://trusted-gpt.example/v1")

    def test_environment_values_take_priority_over_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "review.env"
            env_file.write_text(
                "DEEPSEEK_API_KEY=file-key\n"
                "DEEPSEEK_BASE_URL=https://file.example/v1\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "DEEPSEEK_API_KEY": "environment-key",
                    "DEEPSEEK_BASE_URL": "https://environment.example/v1",
                },
                clear=True,
            ):
                load_env_file(env_file)
                profile = resolve_model_profile("deepseek")
                self.assertEqual(profile.api_key, "environment-key")
                self.assertEqual(
                    profile.base_url,
                    "https://environment.example/v1",
                )

    def test_aminer_uses_only_its_dedicated_credential(self) -> None:
        with patch.dict(
            os.environ,
            {"API_KEY": "generic", "OPENAI_API_KEY": "model-key"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "AMINER_API_KEY"):
                aminer_api_key()
            os.environ["AMINER_API_KEY"] = "aminer-key"
            self.assertEqual(aminer_api_key(), "aminer-key")


class AMinerInputTests(unittest.TestCase):
    def test_content_list_preserves_pages_tables_and_inherited_coordinates(self) -> None:
        markdown, pages, locations, counts = content_list_context(
            [
                {
                    "type": "text",
                    "text": "Experimental Results",
                    "text_level": 1,
                    "page_idx": 0,
                    "bbox": [1, 2, 3, 4],
                },
                {
                    "type": "text",
                    "text": "We compare against strong prior systems.",
                    "page_idx": 0,
                    "bbox": [2, 3, 4, 5],
                },
                {
                    "type": "table",
                    "table_caption": ["Table 1: Main results"],
                    "table_body": "<table><tr><td>Ours</td></tr></table>",
                    "bbox": [3, 4, 5, 6],
                },
                {
                    "type": "text",
                    "text": "References",
                    "text_level": 1,
                    "page_idx": 1,
                },
            ],
            {"pdf_info": [{"page_size": [612, 792]}, {"page_size": [612, 792]}]},
        )
        self.assertEqual(len(pages), 2)
        self.assertIn("<table>", markdown)
        table = next(
            block
            for block in locations[0]["blocks"]
            if block["type"] == "table"
        )
        self.assertTrue(table["page_inherited"])
        self.assertEqual(locations[0]["location_unit"], "aminer_content_block")
        self.assertEqual(counts["table"], 1)

    def test_content_list_pages_feed_the_private_reference_ledger(self) -> None:
        _markdown, pages, _locations, _counts = content_list_context(
            [
                {
                    "type": "text",
                    "text": "References",
                    "text_level": 1,
                    "page_idx": 0,
                },
                {
                    "type": "text",
                    "text": "Alice Author. Structured Retrieval Systems. 2022.",
                    "page_idx": 0,
                },
                {
                    "type": "text",
                    "text": "Bob Builder. Evidence-Aware Search Methods. 2023.",
                    "page_idx": 0,
                },
                {
                    "type": "text",
                    "text": "Carol Researcher. Reliable Literature Discovery. 2024.",
                    "page_idx": 0,
                },
            ]
        )
        ledger = build_reference_ledger(
            pages, parser="aminer_interface_b_markdown"
        )
        self.assertEqual(ledger["status"], "ready_for_resolution")
        self.assertEqual(ledger["summary"]["records_segmented"], 3)

    def test_frozen_interface_b_http_normalizes_without_leaking_credentials(self) -> None:
        pub_id = "0123456789abcdef01234567"
        content_url = (
            "https://paper-bucket.oss-cn-beijing.aliyuncs.com/frozen_content_list.json"
            "?token=signed-token"
        )
        middle_url = (
            "https://paper-bucket.oss-cn-beijing.aliyuncs.com/frozen_middle.json"
            "?token=signed-token"
        )
        markdown_url = (
            "https://paper-bucket.oss-cn-beijing.aliyuncs.com/frozen.md"
            "?token=signed-token"
        )
        rows = [
            {
                "type": "text",
                "text": "A Structured Retrieval Manuscript",
                "text_level": 1,
                "page_idx": 0,
                "bbox": [0, 0, 10, 10],
            },
            {
                "type": "text",
                "text": "This paper studies reliable scientific retrieval.",
                "page_idx": 0,
                "bbox": [0, 10, 10, 20],
            },
            {
                "type": "text",
                "text": "References",
                "text_level": 1,
                "page_idx": 1,
                "bbox": [0, 0, 10, 10],
            },
            {
                "type": "text",
                "text": "A. Author. A useful retrieval method. 2024.",
                "page_idx": 1,
                "bbox": [0, 10, 10, 20],
            },
        ]
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "POST" and str(request.url) == AMINER_EXTRACTED_FILES_URL:
                self.assertEqual(request.headers["Authorization"], "frozen-test-key")
                self.assertEqual(request.headers["X-Platform"], detect_skill_runtime())
                self.assertEqual(request.headers["X-Skill-Name"], "related-work-audit")
                self.assertEqual(request.headers["X-Skill-Version"], __version__)
                payload = json.loads(request.content)
                if "file_types" in payload:
                    return httpx.Response(200, json={"data": [{"md": [markdown_url]}]})
                return httpx.Response(
                    200,
                    json={"data": [{"json": [content_url, middle_url]}]},
                )
            if str(request.url) == content_url:
                return httpx.Response(200, json=rows)
            if str(request.url) == middle_url:
                return httpx.Response(
                    200,
                    json={
                        "pdf_info": [
                            {"page_size": [612, 792]},
                            {"page_size": [612, 792]},
                        ]
                    },
                )
            if str(request.url) == markdown_url:
                return httpx.Response(200, text="# Provider copy\n\nProvider Markdown")
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)

        def client_factory(**kwargs):
            kwargs.pop("proxy", None)
            return httpx.Client(transport=transport, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run"
            result = download_aminer_source(
                pub_id,
                run_dir,
                api_key="frozen-test-key",
                proxy=None,
                client_factory=client_factory,
            )
            self.assertEqual(result["provider"], "aminer_interface_b_content_list")
            self.assertEqual(len(result["pages"]), 2)
            self.assertIn("References", result["pages"][1])
            self.assertTrue((run_dir / "source_content_list.json").is_file())
            self.assertTrue((run_dir / "source_locations.json").is_file())
            manifest_text = (run_dir / "source_manifest.json").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("frozen-test-key", manifest_text)
            self.assertNotIn("signed-token", manifest_text)
            manifest = json.loads(manifest_text)
            self.assertEqual(manifest["page_coordinate_count"], 4)
            self.assertEqual(manifest["explicit_page_coordinate_count"], 4)
            self.assertEqual(manifest["inherited_page_block_count"], 0)
            self.assertEqual(
                stat.S_IMODE((run_dir / "source_manifest.json").stat().st_mode),
                0o600,
            )
        self.assertEqual(sum(request.method == "POST" for request in requests), 2)

    def test_markdown_only_response_is_a_visible_logical_chunk_fallback(self) -> None:
        markdown_url = "https://paper-bucket.oss-cn-beijing.aliyuncs.com/fallback.md"

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                payload = json.loads(request.content)
                if "file_types" in payload:
                    return httpx.Response(
                        200, json={"data": [{"md": [markdown_url]}]}
                    )
                return httpx.Response(200, json={"data": [{"json": []}]})
            if str(request.url) == markdown_url:
                return httpx.Response(
                    200,
                    text="# Paper title\n\n## Introduction\n\n" + "review text " * 500,
                )
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)

        def client_factory(**kwargs):
            kwargs.pop("proxy", None)
            return httpx.Client(transport=transport, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            result = download_aminer_source(
                "b" * 24,
                Path(directory),
                api_key="test-key",
                proxy=None,
                client_factory=client_factory,
            )
        self.assertEqual(result["provider"], "aminer_interface_b_markdown_fallback")
        self.assertTrue(result["fallback_used"])
        self.assertEqual(
            result["source_locations"][0]["location_numbering"], "section_local"
        )
        self.assertIsNone(result["manifest"]["physical_page_count"])

    def test_content_list_without_page_indices_uses_visible_semantic_chunks(self) -> None:
        content_url = (
            "https://paper-bucket.oss-cn-beijing.aliyuncs.com/"
            "no_pages_content_list.json"
        )
        rows = [
            {"type": "text", "text": "A Page-Less Paper", "text_level": 1},
            {"type": "text", "text": "structured review evidence " * 300},
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                payload = json.loads(request.content)
                if "file_types" in payload:
                    return httpx.Response(200, json={"data": [{"md": []}]})
                return httpx.Response(
                    200, json={"data": [{"json": [content_url]}]}
                )
            if str(request.url) == content_url:
                return httpx.Response(200, json=rows)
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)

        def client_factory(**kwargs):
            kwargs.pop("proxy", None)
            return httpx.Client(transport=transport, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            result = download_aminer_source(
                "c" * 24,
                Path(directory),
                api_key="test-key",
                proxy=None,
                client_factory=client_factory,
            )
        self.assertEqual(
            result["provider"],
            "aminer_interface_b_content_list_semantic_chunks",
        )
        self.assertTrue(result["fallback_used"])
        self.assertTrue(result["manifest"]["structured_fallback_used"])
        self.assertEqual(
            result["manifest"]["structured_fallback_reason"],
            "structured blocks had no usable physical-page indices",
        )
        self.assertIsNone(result["manifest"]["physical_page_count"])
        self.assertGreater(result["manifest"]["logical_text_chunks"], 0)
        self.assertEqual(
            result["source_locations"][0]["location_numbering"],
            "section_local",
        )

    def test_artifact_redirect_to_untrusted_host_is_refused_before_following(self) -> None:
        content_url = (
            "https://paper-bucket.oss-cn-beijing.aliyuncs.com/"
            "redirect_content_list.json"
        )
        requested_hosts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested_hosts.append(request.url.host)
            if request.method == "POST":
                payload = json.loads(request.content)
                if "file_types" in payload:
                    return httpx.Response(200, json={"data": [{"md": []}]})
                return httpx.Response(
                    200, json={"data": [{"json": [content_url]}]}
                )
            if str(request.url) == content_url:
                return httpx.Response(
                    302,
                    headers={"location": "https://attacker.example/stolen.json"},
                )
            self.fail(f"untrusted redirect was followed: {request.url}")

        transport = httpx.MockTransport(handler)

        def client_factory(**kwargs):
            kwargs.pop("proxy", None)
            return httpx.Client(transport=transport, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "untrusted artifact"):
                download_aminer_source(
                    "d" * 24,
                    Path(directory),
                    api_key="test-key",
                    proxy=None,
                    client_factory=client_factory,
                )
        self.assertNotIn("attacker.example", requested_hosts)

    def test_untrusted_markdown_download_url_is_rejected(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            if "file_types" in payload:
                return httpx.Response(
                    200,
                    json={"data": [{"md": ["https://attacker.example/paper.md"]}]},
                )
            return httpx.Response(200, json={"data": [{"json": []}]})

        transport = httpx.MockTransport(handler)

        def client_factory(**kwargs):
            kwargs.pop("proxy", None)
            return httpx.Client(transport=transport, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "untrusted artifact"):
                download_aminer_source(
                    "a" * 24,
                    Path(directory),
                    api_key="test-key",
                    proxy=None,
                    client_factory=client_factory,
                )

    def test_artifact_http_error_does_not_expose_signed_url(self) -> None:
        content_url = (
            "https://paper-bucket.oss-cn-beijing.aliyuncs.com/"
            "paper_content_list.json?OSSAccessKeyId=sensitive-token"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                payload = json.loads(request.content)
                if "file_types" in payload:
                    return httpx.Response(200, json={"data": [{"md": []}]})
                return httpx.Response(
                    200,
                    json={"data": [{"json": [content_url]}]},
                )
            return httpx.Response(403, text="forbidden")

        transport = httpx.MockTransport(handler)

        def client_factory(**kwargs):
            kwargs.pop("proxy", None)
            return httpx.Client(transport=transport, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError) as captured:
                download_aminer_source(
                    "e" * 24,
                    Path(directory),
                    api_key="test-key",
                    proxy=None,
                    client_factory=client_factory,
                )
        self.assertIn("HTTP 403", str(captured.exception))
        self.assertNotIn("sensitive-token", str(captured.exception))


class AMinerRelationTests(unittest.TestCase):
    def test_public_relation_fetches_only_backward_references(self) -> None:
        target = "0123456789abcdef01234567"
        referenced = "1123456789abcdef01234567"
        same_year_reference = "2123456789abcdef01234567"
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            self.assertEqual(request.headers["Authorization"], "relation-key")
            self.assertEqual(request.headers["X-Skill-Version"], __version__)
            if request.method == "GET" and str(request.url).split("?", 1)[0] == AMINER_PAPER_RELATION_URL:
                self.assertEqual(request.url.params["id"], target)
                self.assertNotIn("ref", request.url.params)
                self.assertNotIn("cited", request.url.params)
                return httpx.Response(
                    200,
                    json={
                        "code": 200,
                        "success": True,
                        "data": {
                            "_id": target,
                            "cited": [
                                {"_id": referenced},
                                {"_id": same_year_reference},
                            ],
                        },
                    },
                )
            if request.method == "POST" and str(request.url) == AMINER_PAPER_INFO_URL:
                self.assertEqual(
                    set(json.loads(request.content)["ids"]),
                    {referenced, same_year_reference},
                )
                return httpx.Response(
                    200,
                    json={
                        "code": 200,
                        "success": True,
                        "data": [
                            {
                                "id": referenced,
                                "title": "A Referenced Prior Method",
                                "year": 2024,
                                "authors": [{"name": "A. Author"}],
                                "venue": {"raw": "Example Conference"},
                                "venue_id": "venue-1",
                            },
                            {
                                "id": same_year_reference,
                                "title": "A Same-Year Referenced Method",
                                "year": 2025,
                                "abstract_slice": "A directly related method.",
                            },
                        ],
                    },
                )
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)

        def client_factory(**kwargs):
            kwargs.pop("proxy", None)
            return httpx.Client(transport=transport, **kwargs)

        result = fetch_aminer_relation_context(
            target,
            review_as_of="2025-09-24",
            api_key="relation-key",
            proxy=None,
            client_factory=client_factory,
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(
            [item["aminer_pub_id"] for item in result["target_references"]],
            [referenced, same_year_reference],
        )
        self.assertEqual(result["candidate_records"], [])
        self.assertEqual(result["citing_target_records"], [])
        self.assertEqual(result["target_references"][0]["year"], 2024)
        self.assertEqual(
            result["target_references"][0]["venue"], "Example Conference"
        )
        self.assertEqual(result["summary"]["same_year_cutoff_uncertain"], 1)
        self.assertEqual(result["metadata_missing_ids"], [])
        self.assertEqual(result["summary"]["relation_api_calls_attempted"], 1)
        self.assertEqual(result["summary"]["paper_info_batches_completed"], 1)
        self.assertEqual(result["summary"]["total_http_request_count"], 2)
        self.assertEqual(result["supported_directions"], ["target_references"])
        self.assertEqual(result["unsupported_directions"], ["papers_citing_target"])
        self.assertEqual(result["cost"]["estimated_total_yuan"], 0.1)
        self.assertTrue(
            all(item["query_value"] == target for item in result["queries"])
        )
        self.assertEqual(sum(request.method == "GET" for request in requests), 1)
        self.assertEqual(sum(request.method == "POST" for request in requests), 1)

    def test_relation_http_failures_are_reported_and_non_fatal(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="temporarily unavailable")

        transport = httpx.MockTransport(handler)

        def client_factory(**kwargs):
            kwargs.pop("proxy", None)
            return httpx.Client(transport=transport, **kwargs)

        result = fetch_aminer_relation_context(
            "0123456789abcdef01234567",
            review_as_of="2025-09-24",
            api_key="relation-key",
            proxy=None,
            client_factory=client_factory,
        )
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["non_fatal"])
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["candidate_records"], [])

    def test_relation_paper_info_is_batched_at_one_hundred_ids(self) -> None:
        target = "f" * 24
        cited_ids = [f"{number:024x}" for number in range(1, 102)]
        batch_sizes: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url).split("?", 1)[0]
            if request.method == "GET" and url == AMINER_PAPER_RELATION_URL:
                return httpx.Response(
                    200,
                    json={
                        "code": 200,
                        "success": True,
                        "data": {"_id": target, "cited": cited_ids},
                    },
                )
            if request.method == "POST" and url == AMINER_PAPER_INFO_URL:
                ids = json.loads(request.content)["ids"]
                batch_sizes.append(len(ids))
                return httpx.Response(
                    200,
                    json={
                        "code": 200,
                        "success": True,
                        "data": [
                            {"id": paper_id, "title": f"Paper {paper_id}", "year": 2024}
                            for paper_id in ids
                        ],
                    },
                )
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)

        def client_factory(**kwargs):
            kwargs.pop("proxy", None)
            return httpx.Client(transport=transport, **kwargs)

        result = fetch_aminer_relation_context(
            target,
            review_as_of="2025-09-24",
            api_key="relation-key",
            proxy=None,
            client_factory=client_factory,
        )
        self.assertEqual(batch_sizes, [100, 1])
        self.assertEqual(result["summary"]["paper_info_batches_attempted"], 2)
        self.assertEqual(result["summary"]["paper_info_batches_completed"], 2)
        self.assertEqual(len(result["target_references"]), 101)


class AMinerSearchTests(unittest.TestCase):
    def _client_factory(self, handler):
        transport = httpx.MockTransport(handler)

        def factory(**kwargs):
            kwargs.pop("proxy", None)
            return httpx.Client(transport=transport, **kwargs)

        return factory

    def test_default_free_search_uses_paper_info_and_reports_zero_cost(self) -> None:
        paper_id = "1123456789abcdef01234567"
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            self.assertEqual(request.headers["X-Platform"], detect_skill_runtime())
            self.assertEqual(request.headers["X-Skill-Name"], "related-work-audit")
            self.assertEqual(request.headers["X-Skill-Version"], __version__)
            url = str(request.url).split("?", 1)[0]
            if request.method == "GET" and url == AMINER_PAPER_SEARCH_URL:
                self.assertEqual(request.url.params["query"], "retrieval grounded profiles")
                self.assertEqual(request.url.params["size"], "20")
                self.assertEqual(request.url.params["year_to"], "2026")
                self.assertNotIn("title", request.url.params)
                return httpx.Response(
                    200,
                    json={
                        "code": 200,
                        "success": True,
                        "data": [
                            {
                                "id": paper_id,
                                "title": "Retrieval Grounded User Profiles",
                                "first_author": "A. Author",
                                "year": 2025,
                                "venue_name": "ExampleConf",
                            }
                        ],
                        "total": 1,
                    },
                )
            if request.method == "POST" and url == SEARCH_PAPER_INFO_URL:
                self.assertEqual(json.loads(request.content), {"ids": [paper_id]})
                return httpx.Response(
                    200,
                    json={
                        "code": 200,
                        "success": True,
                        "data": [
                            {
                                "id": paper_id,
                                "title": "Retrieval Grounded User Profiles",
                                "authors": [{"name": "Alice Author"}],
                                "year": 2025,
                                "abstract_slice": "A grounded profile method.",
                            }
                        ],
                    },
                )
            return httpx.Response(404)

        rows, diagnostics = retrieve_aminer_records(
            ["retrieval grounded profiles"],
            review_as_of="2026-10-01",
            api_key="aminer-key",
            proxy=None,
            client_factory=self._client_factory(handler),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["aminer_pub_id"], paper_id)
        self.assertEqual(rows[0]["authors"], "Alice Author")
        self.assertEqual(rows[0]["summary"], "A grounded profile method.")
        self.assertEqual(
            [item["api"] for item in diagnostics["api_calls"]],
            ["paper_search", "paper_info"],
        )
        self.assertEqual(diagnostics["api_calls"][0]["response"]["total"], 1)
        self.assertEqual(diagnostics["cost"]["estimated_total_yuan"], 0.0)
        self.assertFalse(diagnostics["external_fallback_used"])
        self.assertEqual(len(requests), 2)

    def test_qa_pro_permission_failure_downgrades_only_inside_aminer(self) -> None:
        paper_id = "2123456789abcdef01234567"

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url).split("?", 1)[0]
            if url in {
                AMINER_PAPER_QA_SEARCH_PRO_URL,
                AMINER_PAPER_QA_SEARCH_URL,
                AMINER_PAPER_SEARCH_PRO_URL,
            }:
                return httpx.Response(
                    403,
                    json={
                        "code": 403,
                        "msg": "no permission",
                        "log_id": f"log-{url.rsplit('/', 1)[-1]}",
                    },
                )
            if url == AMINER_PAPER_SEARCH_URL:
                return httpx.Response(
                    200,
                    json={
                        "code": 200,
                        "success": True,
                        "data": [{"id": paper_id, "title": "Complex Semantic Question Methods", "year": 2024}],
                    },
                )
            if url == SEARCH_PAPER_INFO_URL:
                return httpx.Response(200, json={"code": 200, "success": True, "data": []})
            return httpx.Response(404)

        rows, diagnostics = retrieve_aminer_records(
            ["complex semantic question"],
            review_as_of="2026-01-01",
            api_key="aminer-key",
            proxy=None,
            search_tier="qa-pro",
            client_factory=self._client_factory(handler),
        )
        self.assertEqual(
            [row["title"] for row in rows],
            ["Complex Semantic Question Methods"],
        )
        self.assertEqual(
            [(item["from"], item["to"]) for item in diagnostics["downgrades"]],
            [("qa-pro", "qa"), ("qa", "pro"), ("pro", "free")],
        )
        self.assertEqual(diagnostics["provider_errors"], [])
        self.assertFalse(diagnostics["external_fallback_used"])
        self.assertEqual(diagnostics["cost"]["estimated_total_yuan"], 0.36)
        self.assertTrue(
            diagnostics["api_calls"][0]["response"]["log_id"].startswith("log-")
        )

    def test_auto_routes_by_shape_with_only_documented_parameters(self) -> None:
        identifiers = [f"{index:024x}" for index in range(1, 9)]
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url).split("?", 1)[0]
            if url == AMINER_PAPER_SEARCH_URL:
                seen.append("free")
                self.assertEqual(
                    set(request.url.params),
                    {"query", "year_to", "page", "size"},
                )
                self.assertEqual(request.url.params["query"], "user profile recommendation")
                self.assertEqual(request.url.params["year_to"], "2026")
                return httpx.Response(200, json={"code": 200, "success": True, "data": [{"id": identifiers[0], "title": "User Profile Recommendation", "year": 2025}]})
            if url == AMINER_PAPER_SEARCH_PRO_URL:
                seen.append("pro")
                self.assertEqual(request.url.params["query"], "retrieval grounded profiles")
                self.assertEqual(request.url.params["year_to"], "2026")
                self.assertNotIn("title", request.url.params)
                return httpx.Response(200, json={"code": 200, "success": True, "data": [{"id": identifiers[1], "title": "Retrieval Grounded Profiles", "year": 2024}]})
            if url == AMINER_PAPER_QA_SEARCH_URL:
                seen.append("qa")
                self.assertEqual(
                    json.loads(request.content),
                    {"use_topic": True, "query": "Which methods learn profiles with retrieval feedback?", "size": 20, "offset": 0},
                )
                return httpx.Response(200, json={"code": 200, "success": True, "data": [{"id": identifiers[2], "title": "Learning Profiles with Feedback", "year": 2023}]})
            if url == AMINER_PAPER_LIST_BY_KEYWORDS_URL:
                seen.append("keyword-batch")
                keywords = json.loads(request.url.params["keywords"])
                self.assertEqual(len(keywords), 3)
                self.assertTrue(all(" " not in keyword for keyword in keywords))
                return httpx.Response(200, json={"code": 200, "success": True, "data": [{"id": identifiers[index], "title": f"Thematic Candidate {index}", "year": 2022} for index in range(3, 8)]})
            if url == SEARCH_PAPER_INFO_URL:
                seen.append("info")
                return httpx.Response(200, json={"code": 200, "success": True, "data": []})
            return httpx.Response(404)

        rows, diagnostics = retrieve_aminer_records(
            [
                {"query": "user profile recommendation", "query_shape": "title_phrase", "fallback_phrase": "user profile recommendation"},
                {"query": "retrieval grounded profiles", "query_shape": "keyword_phrase", "fallback_phrase": "retrieval grounded profiles"},
                {"query": "Which methods learn profiles with retrieval feedback?", "query_shape": "natural_language", "fallback_phrase": "profiles retrieval feedback"},
            ],
            review_as_of="2026-10-01",
            api_key="aminer-key",
            proxy=None,
            search_tier="auto",
            detail_limit=0,
            client_factory=self._client_factory(handler),
        )
        self.assertEqual(seen, ["free", "pro", "qa", "keyword-batch", "info"])
        self.assertEqual(len(rows), 8)
        self.assertNotIn("paper_qa_search_pro", [call["api"] for call in diagnostics["api_calls"]])
        self.assertEqual(diagnostics["cost"]["estimated_total_yuan"], 0.16)
        for call in diagnostics["api_calls"]:
            self.assertIn("request_method", call)
            self.assertIn("parameter_shape", call)

    def test_pro_uses_query_and_cutoff_but_exact_title_keeps_title_field(self) -> None:
        identifiers = ["1123456789abcdef01234567", "2123456789abcdef01234567"]
        requests: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url).split("?", 1)[0]
            if url == AMINER_PAPER_SEARCH_PRO_URL:
                requests.append(dict(request.url.params))
                paper_id = identifiers[len(requests) - 1]
                title = (
                    "Sparse Memory Sequential Recommendation"
                    if "query" in request.url.params
                    else "A Complete Exact Prior Paper Title"
                )
                return httpx.Response(
                    200,
                    json={
                        "code": 200,
                        "success": True,
                        "data": [{"id": paper_id, "title": title, "year": 2025}],
                    },
                )
            if url == SEARCH_PAPER_INFO_URL:
                return httpx.Response(
                    200, json={"code": 200, "success": True, "data": []}
                )
            return httpx.Response(404)

        rows, diagnostics = retrieve_aminer_records(
            [
                {
                    "query": "sparse memory sequential recommendation",
                    "query_shape": "keyword_phrase",
                    "fallback_phrase": "sparse memory sequential recommendation",
                },
                {
                    "query": "A Complete Exact Prior Paper Title",
                    "query_shape": "exact_title",
                    "fallback_phrase": "A Complete Exact Prior Paper Title",
                },
            ],
            review_as_of="2026-10-01",
            api_key="aminer-key",
            proxy=None,
            search_tier="pro",
            allow_keyword_expansion=False,
            client_factory=self._client_factory(handler),
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual(requests[0]["query"], "sparse memory sequential recommendation")
        self.assertNotIn("title", requests[0])
        self.assertEqual(requests[0]["year_to"], "2026")
        self.assertEqual(requests[0]["page"], "0")
        self.assertEqual(requests[1]["title"], "A Complete Exact Prior Paper Title")
        self.assertNotIn("query", requests[1])
        self.assertEqual(requests[1]["year_to"], "2026")
        self.assertFalse(diagnostics["external_fallback_used"])

    def test_pro_fields_only_controlled_mechanism_domain_feedback_as_title(self) -> None:
        identifiers = ["3123456789abcdef01234567", "4123456789abcdef01234567"]
        requests: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url).split("?", 1)[0]
            if url == AMINER_PAPER_SEARCH_PRO_URL:
                requests.append(dict(request.url.params))
                paper_id = identifiers[len(requests) - 1]
                return httpx.Response(
                    200,
                    json={
                        "code": 200,
                        "success": True,
                        "data": [
                            {
                                "id": paper_id,
                                "title": request.url.params.get("title")
                                or request.url.params.get("query"),
                                "year": 2025,
                            }
                        ],
                    },
                )
            if url == SEARCH_PAPER_INFO_URL:
                return httpx.Response(
                    200, json={"code": 200, "success": True, "data": []}
                )
            return httpx.Response(404)

        rows, diagnostics = retrieve_aminer_records(
            [
                {
                    "query": "diffusion denoising sequence prediction",
                    "query_shape": "keyword_phrase",
                    "query_subtype": "controlled_mechanism_domain",
                    "mechanism_branch": "diffusion denoising",
                    "feedback_origin": "deterministic_mechanism_completion",
                    "fallback_phrase": "diffusion denoising sequence prediction",
                    "pro_field": "title",
                },
                {
                    "query": "memory compression sequence prediction",
                    "query_shape": "keyword_phrase",
                    "fallback_phrase": "memory compression sequence prediction",
                    "pro_field": "title",
                },
            ],
            review_as_of="2026-10-29",
            api_key="aminer-key",
            proxy=None,
            search_tier="pro",
            allow_keyword_expansion=False,
            client_factory=self._client_factory(handler),
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual(
            requests[0]["title"], "diffusion denoising sequence prediction"
        )
        self.assertNotIn("query", requests[0])
        self.assertEqual(
            requests[1]["query"], "memory compression sequence prediction"
        )
        self.assertNotIn("title", requests[1])
        self.assertEqual(
            diagnostics["api_calls"][0]["query_subtype"],
            "controlled_mechanism_domain",
        )
        self.assertEqual(
            diagnostics["query_plan"][0]["query_subtype"],
            "controlled_mechanism_domain",
        )
        self.assertEqual(
            diagnostics["query_plan"][0]["mechanism_branch"],
            "diffusion denoising",
        )
        self.assertEqual(
            diagnostics["api_calls"][0]["feedback_origin"],
            "deterministic_mechanism_completion",
        )
        self.assertEqual(rows[0]["query_subtype"], "controlled_mechanism_domain")
        self.assertEqual(rows[0]["mechanism_branch"], "diffusion denoising")
        self.assertEqual(diagnostics["query_plan"][1]["query_subtype"], "")
        self.assertIn(
            "mechanism+domain",
            diagnostics["api_calls"][0]["route_reason"],
        )

    def test_low_overlap_route_preserves_candidates_while_trying_next_route(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url).split("?", 1)[0]
            if url == AMINER_PAPER_SEARCH_URL:
                seen.append("free")
                return httpx.Response(
                    200,
                    json={
                        "code": 200,
                        "success": True,
                        "data": [
                            {
                                "id": f"{index:024x}",
                                "title": f"Unrelated Vision Survey Number {index}",
                                "year": 2024,
                            }
                            for index in range(1, 11)
                        ],
                    },
                )
            if url == AMINER_PAPER_SEARCH_PRO_URL:
                seen.append("pro")
                return httpx.Response(
                    200,
                    json={
                        "code": 200,
                        "success": True,
                        "data": [
                            {
                                "id": f"{101:024x}",
                                "title": "Grounded Retrieval Profile Learning",
                                "year": 2024,
                            }
                        ],
                    },
                )
            if url == SEARCH_PAPER_INFO_URL:
                seen.append("info")
                return httpx.Response(200, json={"code": 200, "success": True, "data": []})
            return httpx.Response(404)

        rows, diagnostics = retrieve_aminer_records(
            [
                {
                    "query": "Grounded Retrieval Profile",
                    "query_shape": "title_phrase",
                    "fallback_phrase": "Grounded Retrieval Profile",
                    "role": "method_mechanism",
                }
            ],
            review_as_of="2026-10-01",
            api_key="aminer-key",
            proxy=None,
            search_tier="auto",
            detail_limit=0,
            client_factory=self._client_factory(handler),
        )

        self.assertEqual(seen, ["free", "pro", "info"])
        self.assertEqual(len(rows), 11)
        self.assertEqual(rows[0]["title"], "Grounded Retrieval Profile Learning")
        self.assertEqual({r["retrieval_tier"] for r in rows[1:]}, {"free"})
        self.assertTrue(all(not r["query_title_overlap_hint"] for r in rows[1:]))
        self.assertEqual(sum(s["retained_record_count"] for s in diagnostics["search_responses"]), 11)
        self.assertEqual(diagnostics["downgrades"][0]["trigger"], "low_quality_result")

    def test_low_overlap_discovery_rows_remain_for_semantic_selection(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url).split("?", 1)[0]
            if url in {AMINER_PAPER_SEARCH_URL, AMINER_PAPER_SEARCH_PRO_URL}:
                return httpx.Response(
                    200,
                    json={
                        "code": 200,
                        "success": True,
                        "data": [
                            {
                                "id": "1123456789abcdef01234567",
                                "title": "Unrelated Medical Imaging Survey",
                                "year": 2024,
                            }
                        ],
                    },
                )
            if url == SEARCH_PAPER_INFO_URL:
                return httpx.Response(200, json={"code": 200, "data": []})
            return httpx.Response(404)

        rows, diagnostics = retrieve_aminer_records(
            [
                {
                    "query": "Grounded Retrieval Profiles",
                    "query_shape": "title_phrase",
                    "fallback_phrase": "Grounded Retrieval Profiles",
                    "role": "method_mechanism",
                }
            ],
            review_as_of="2026-10-01",
            api_key="aminer-key",
            proxy=None,
            search_tier="auto",
            allow_keyword_expansion=False,
            client_factory=self._client_factory(handler),
        )
        self.assertEqual(len(rows), 2)  # provenance retained; downstream deduplicates
        self.assertTrue(all(not row["query_title_overlap_hint"] for row in rows))
        self.assertTrue(all("tier" not in row for row in rows))  # retrieval is not admission
        self.assertEqual(
            [call["status"] for call in diagnostics["api_calls"] if call["api"] != "paper_info"],
            ["completed_low_quality", "completed_low_quality"],
        )

    def test_short_declared_exact_title_matches_expanded_method_title(self) -> None:
        paper_id = "2123456789abcdef01234567"

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url).split("?", 1)[0]
            if url == AMINER_PAPER_SEARCH_URL:
                return httpx.Response(
                    200,
                    json={
                        "code": 200,
                        "success": True,
                        "data": [
                            {
                                "id": paper_id,
                                "title": "Mamba4Rec: State Space Models for Sequential Recommendation",
                                "year": 2025,
                            }
                        ],
                    },
                )
            if url == SEARCH_PAPER_INFO_URL:
                return httpx.Response(
                    200, json={"code": 200, "success": True, "data": []}
                )
            return httpx.Response(404)

        rows, diagnostics = retrieve_aminer_records(
            [
                {
                    "query": "Mamba4Rec",
                    "query_shape": "exact_title",
                    "fallback_phrase": "Mamba4Rec",
                    "role": "named_prior_work",
                }
            ],
            review_as_of="2026-10-01",
            api_key="aminer-key",
            proxy=None,
            client_factory=self._client_factory(handler),
        )
        self.assertEqual([row["title"] for row in rows], [
            "Mamba4Rec: State Space Models for Sequential Recommendation"
        ])
        self.assertEqual(
            diagnostics["query_plan"][0]["query_shape"], "title_phrase"
        )

    def test_keyword_expansion_uses_prequalified_pool_not_raw_count(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url).split("?", 1)[0]
            if url in {AMINER_PAPER_SEARCH_URL, AMINER_PAPER_SEARCH_PRO_URL}:
                api = "free" if url == AMINER_PAPER_SEARCH_URL else "pro"
                seen.append(api)
                query = request.url.params.get("title", "")
                base = 1 if "profile" in query.lower() else 20
                if api == "pro":
                    base += 100
                return httpx.Response(
                    200,
                    json={
                        "code": 200,
                        "success": True,
                        "data": [
                            {
                                "id": f"{base + index:024x}",
                                "title": f"Unrelated Rendering Survey {base + index}",
                                "year": 2024,
                            }
                            for index in range(5)
                        ],
                    },
                )
            if url == AMINER_PAPER_LIST_BY_KEYWORDS_URL:
                seen.append("keyword")
                keywords = json.loads(request.url.params["keywords"])
                self.assertGreaterEqual(len(keywords), 2)
                self.assertLessEqual(len(keywords), 3)
                return httpx.Response(
                    200,
                    json={
                        "code": 200,
                        "success": True,
                        "data": [
                            {
                                "id": f"{300 + index:024x}",
                                "title": f"Grounded Profile Evaluation {index}",
                                "year": 2023,
                            }
                            for index in range(2)
                        ],
                    },
                )
            if url == SEARCH_PAPER_INFO_URL:
                seen.append("info")
                return httpx.Response(200, json={"code": 200, "success": True, "data": []})
            return httpx.Response(404)

        rows, diagnostics = retrieve_aminer_records(
            [
                {
                    "query": "grounded user profile",
                    "query_shape": "title_phrase",
                    "fallback_phrase": "grounded user profile",
                    "role": "problem_setting",
                },
                {
                    "query": "retrieval feedback evaluation",
                    "query_shape": "title_phrase",
                    "fallback_phrase": "retrieval feedback evaluation",
                    "role": "benchmark_or_evaluation",
                },
            ],
            review_as_of="2026-10-01",
            api_key="aminer-key",
            proxy=None,
            search_tier="auto",
            detail_limit=0,
            client_factory=self._client_factory(handler),
        )

        self.assertEqual(len(rows), 22)
        self.assertEqual(seen.count("keyword"), 1)
        expansion = diagnostics["keyword_expansion"]
        self.assertTrue(expansion["attempted"])
        self.assertEqual(expansion["raw_unique_candidate_count"], 10)
        self.assertEqual(expansion["prequalified_candidate_count"], 0)
        self.assertIn("prequalified_candidates_below_8", expansion["trigger"])

    def test_title_resolution_requires_unique_corroborated_identity(self) -> None:
        paper_id = "3123456789abcdef01234567"
        title = "A Safe Exact Manuscript Match"

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url).split("?", 1)[0]
            if url == AMINER_PAPER_SEARCH_URL:
                self.assertEqual(request.url.params["query"], title)
                self.assertEqual(request.url.params["year_to"], "2026")
                self.assertNotIn("title", request.url.params)
                return httpx.Response(
                    200,
                    json={
                        "code": 200,
                        "success": True,
                        "data": [
                            {
                                "id": paper_id,
                                "title": title,
                                "first_author": "Alice Smith",
                                "year": 2025,
                            }
                        ],
                    },
                )
            if url == SEARCH_PAPER_INFO_URL:
                return httpx.Response(
                    200,
                    json={
                        "code": 200,
                        "success": True,
                        "data": [
                            {
                                "id": paper_id,
                                "title": title,
                                "authors": [{"name": "Alice Smith"}],
                                "year": 2025,
                            }
                        ],
                    },
                )
            return httpx.Response(404)

        result = resolve_title_to_pub_id(
            title,
            authors=["Alice Smith"],
            manuscript_year=2025,
            review_as_of="2026-10-01",
            api_key="aminer-key",
            proxy=None,
            client_factory=self._client_factory(handler),
        )
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["resolved_pub_id"], paper_id)

    def test_title_only_candidate_is_never_claimed(self) -> None:
        paper_id = "4123456789abcdef01234567"
        title = "Anonymous Submission Title"

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url).split("?", 1)[0]
            if url == AMINER_PAPER_SEARCH_URL:
                return httpx.Response(
                    200,
                    json={
                        "code": 200,
                        "success": True,
                        "data": [{"id": paper_id, "title": title, "year": 2026}],
                    },
                )
            if url == SEARCH_PAPER_INFO_URL:
                return httpx.Response(
                    200,
                    json={
                        "code": 200,
                        "success": True,
                        "data": [{"id": paper_id, "title": title, "year": 2026}],
                    },
                )
            return httpx.Response(404)

        result = resolve_title_to_pub_id(
            title,
            authors=[],
            manuscript_year=None,
            review_as_of="2026-10-01",
            api_key="aminer-key",
            proxy=None,
            client_factory=self._client_factory(handler),
        )
        self.assertEqual(result["status"], "insufficient_identity_evidence")
        self.assertIsNone(result["resolved_pub_id"])

    def test_multiple_verified_title_matches_are_ambiguous(self) -> None:
        paper_ids = [
            "5123456789abcdef01234567",
            "6123456789abcdef01234567",
        ]
        title = "A Duplicated Exact Manuscript Title"

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url).split("?", 1)[0]
            rows = [
                {
                    "id": paper_id,
                    "title": title,
                    "authors": [{"name": "Alice Smith"}],
                    "first_author": "Alice Smith",
                    "year": 2025,
                }
                for paper_id in paper_ids
            ]
            if url == AMINER_PAPER_SEARCH_URL:
                return httpx.Response(
                    200,
                    json={"code": 200, "success": True, "data": rows},
                )
            if url == SEARCH_PAPER_INFO_URL:
                return httpx.Response(
                    200,
                    json={"code": 200, "success": True, "data": rows},
                )
            return httpx.Response(404)

        result = resolve_title_to_pub_id(
            title,
            authors=["Alice Smith"],
            manuscript_year=2025,
            review_as_of="2026-10-01",
            api_key="aminer-key",
            proxy=None,
            client_factory=self._client_factory(handler),
        )
        self.assertEqual(result["status"], "ambiguous")
        self.assertIsNone(result["resolved_pub_id"])


class CoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = {
            "status": "ready_for_resolution",
            "records": [{
                "id": "R1",
                "candidate_title": "A Precise Method for Structured Scientific Retrieval",
                "doi": "10.1234/example.1",
                "arxiv_id": None,
                "anchor": {"page_start": 9, "page_end": 9, "quote": "reference"},
                "resolution_eligible": True,
            }],
        }

    def test_exact_identifier_is_covered(self) -> None:
        audit = assess_related_work_coverage([{
            "title": "Different metadata title",
            "url": "https://doi.org/10.1234/example.1",
        }], self.ledger)
        self.assertEqual(audit["records"][0]["status"], "covered")
        self.assertEqual(audit["records"][0]["match"]["match_type"], "doi")
        self.assertNotIn("aminer_pub_id", audit["records"][0])
        self.assertNotIn("relation_evidence", audit["records"][0])

    def test_unmatched_candidate_is_not_an_omission_finding(self) -> None:
        audit = assess_related_work_coverage([{
            "title": "An Unrelated Candidate With No Bibliography Match",
            "url": "https://arxiv.org/abs/2401.12345",
        }], self.ledger)
        record = audit["records"][0]
        self.assertEqual(record["status"], "candidate_to_check")
        self.assertIn("not an omission finding", record["reason"])

    def test_two_tier_metadata_and_missing_status_survive_coverage(self) -> None:
        audit = assess_related_work_coverage(
            [
                {
                    "title": "A Precise Method for Structured Scientific Retrieval",
                    "url": "https://example.test/already-cited",
                    "tier": "core",
                    "confidence": "high",
                    "candidate_label": "candidate_to_check",
                    "missing_status": "unknown",
                    "selection_source": "llm_ranked_shortlist",
                    "fill_source": "llm_ranked_shortlist",
                    "selection_evidence": {"retrieval_role": "method_mechanism"},
                    "verification": {"bibliography": "pending_coverage_check"},
                },
                {
                    "title": "A Distinct Exploratory Retrieval Candidate",
                    "url": "https://example.test/to-check",
                    "tier": "extended",
                    "confidence": "medium",
                    "candidate_label": "candidate_to_check",
                    "missing_status": "unknown",
                    "selection_source": "deterministic_role_balanced_medium_fill",
                    "fill_source": "deterministic_role_balanced_medium_fill",
                    "selection_evidence": {"retrieval_role": "problem_setting"},
                    "verification": {"bibliography": "pending_coverage_check"},
                },
            ],
            self.ledger,
        )

        [exploratory] = audit["records"]
        [covered] = audit["already_cited_controls"]
        self.assertEqual(covered["missing_status"], "already_cited")
        self.assertFalse(covered["final_output_eligible"])
        self.assertEqual(exploratory["missing_status"], "uncited")
        self.assertEqual(exploratory["candidate_label"], "candidate_to_check")
        self.assertEqual(exploratory["tier"], "extended")
        self.assertEqual(exploratory["confidence"], "medium")
        self.assertEqual(
            exploratory["verification"]["bibliography"]["coverage_status"],
            "candidate_to_check",
        )
        self.assertEqual(audit["summary"]["candidate_tiers"], {"core": 0, "extended": 1})
        self.assertEqual(audit["summary"]["already_cited_control_count"], 1)
        self.assertTrue(audit["summary"]["partition_conservation"]["conserved"])
        self.assertEqual(audit["summary"]["evaluation_view_status"], "insufficient_candidates")

    def test_core_requires_confirmed_uncited_but_extended_may_be_unknown(self) -> None:
        audit = assess_related_work_coverage(
            [
                {
                    "title": "Unknown Core Candidate",
                    "tier": "core",
                    "confidence": "high",
                    "candidate_label": "candidate_to_check",
                },
                {
                    "title": "Unknown Extended Candidate",
                    "tier": "extended",
                    "confidence": "medium",
                    "candidate_label": "candidate_to_check",
                },
            ],
            {"status": "parse_quality_insufficient", "records": []},
        )

        self.assertEqual(
            [item["title"] for item in audit["records"]],
            ["Unknown Extended Candidate"],
        )
        self.assertEqual(audit["records"][0]["missing_status"], "unknown")
        self.assertEqual(
            audit["final_selection_exclusions"][0]["reason"],
            "core_requires_bibliography_confirmed_uncited",
        )
        self.assertTrue(audit["summary"]["partition_conservation"]["conserved"])

    def test_formal_view_excludes_unresolved_same_year_candidate(self) -> None:
        audit = assess_related_work_coverage(
            [
                {
                    "title": "Same-Year Date-Unresolved Candidate",
                    "published": "2026",
                    "date_precision": "year",
                    "cutoff_status": "uncertain_same_year",
                    "time_admissibility": "uncertain",
                    "tier": "extended",
                    "confidence": "medium",
                    "candidate_label": "candidate_to_check",
                }
            ],
            self.ledger,
        )
        self.assertEqual(audit["records"], [])
        self.assertEqual(
            audit["final_selection_exclusions"][0]["reason"],
            "cutoff_date_unresolved",
        )
        self.assertTrue(audit["summary"]["partition_conservation"]["conserved"])

    def test_bad_ledger_preserves_not_assessed(self) -> None:
        audit = assess_related_work_coverage([{"title": "Some Candidate Work"}], {
            "status": "parse_quality_insufficient", "records": []
        })
        self.assertEqual(audit["status"], "not_assessed")
        self.assertEqual(audit["records"][0]["status"], "not_assessed")

    def test_outgoing_aminer_relation_is_a_hint_with_unavailable_local_ledger(self) -> None:
        related_id = "1123456789abcdef01234567"
        audit = assess_related_work_coverage(
            [
                {
                    "title": "A Relation-Covered Prior Method",
                    "aminer_pub_id": related_id,
                    "source": "AMiner Open Platform paper/relation + paper_info",
                }
            ],
            {"status": "parse_quality_insufficient", "records": []},
            relation_references=[
                {
                    "aminer_pub_id": related_id,
                    "title": "A Relation-Covered Prior Method",
                    "relation_evidence": {
                        "direction": "target_cites_related_paper"
                    },
                }
            ],
        )
        record = audit["records"][0]
        self.assertEqual(audit["status"], "partial")
        self.assertEqual(record["status"], "not_assessed")
        self.assertIsNone(record["match"])
        self.assertEqual(record["external_reference_hint"]["match_type"], "aminer_paper_relation_id")
        self.assertEqual(record["aminer_pub_id"], related_id)

    def test_relation_evidence_does_not_replace_an_unavailable_ledger(self) -> None:
        related_id = "1123456789abcdef01234567"
        audit = assess_related_work_coverage(
            [
                {
                    "title": "A Relation-Covered Prior Method",
                    "aminer_pub_id": related_id,
                },
                {
                    "title": "An Unmatched Candidate Outside The Relation Page",
                    "aminer_pub_id": "2123456789abcdef01234567",
                },
            ],
            {"status": "parse_quality_insufficient", "records": []},
            relation_references=[
                {
                    "aminer_pub_id": related_id,
                    "title": "A Relation-Covered Prior Method",
                    "relation_evidence": {
                        "direction": "target_cites_related_paper"
                    },
                }
            ],
        )
        self.assertEqual(audit["status"], "partial")
        self.assertEqual(audit["records"][0]["status"], "not_assessed")
        self.assertEqual(audit["records"][1]["status"], "not_assessed")
        self.assertIn("cannot establish citations", audit["records"][1]["reason"])


class RetrievalPolicyTests(unittest.TestCase):
    def test_query_planning_splits_methods_and_routes_by_content_not_position(self) -> None:
        def model(_system, _user, *, max_tokens):
            self.assertGreater(max_tokens, 0)
            return {
                "retrieval_profile": {
                    "domain_terms": ["personalized recommendation systems"],
                    "task_terms": ["grounded user profiling"],
                    "mechanism_terms": ["retrieval feedback"],
                    "data_protocol_terms": ["predictive recommendation"],
                },
                "queries": [
                    {
                        "query": "Which approaches learn grounded user profiles from retrieval feedback?",
                        "role": "problem_setting",
                    },
                    {
                        "query": "MethodAlpha, MethodBeta",
                        "role": "direct_baseline",
                        "methods": ["MethodAlpha", "MethodBeta"],
                    },
                    {
                        "query": "grounded profile retrieval benchmark",
                        "role": "benchmark_or_evaluation",
                    },
                ],
                "exact_titles": ["A Precise Prior Paper Title"],
            }

        plans = build_related_work_queries(
            "# Manuscript\nA grounded profiling method.",
            review_as_of="2026-10-01",
            llm_json=model,
        )

        self.assertEqual(
            [str(item) for item in plans],
            [
                "A Precise Prior Paper Title",
                "Which approaches learn grounded user profiles from retrieval feedback?",
                "MethodAlpha",
                "MethodBeta",
                "grounded profile retrieval benchmark",
            ],
        )
        self.assertEqual(plans[0].query_shape, "exact_title")
        self.assertEqual(plans[1].query_shape, "natural_language")
        self.assertEqual(plans[2].query_shape, "title_phrase")
        self.assertEqual(plans[3].query_shape, "title_phrase")
        self.assertEqual(plans[4].query_shape, "title_phrase")
        self.assertNotIn("MethodAlpha, MethodBeta", [str(item) for item in plans])
        self.assertEqual(
            plans[0].retrieval_profile["mechanism_terms"],
            ["retrieval feedback"],
        )
        self.assertEqual(
            plans[0].retrieval_profile["domain_terms"],
            ["personalized recommendation systems"],
        )

    def test_query_planning_keeps_component_search_phrases_short(self) -> None:
        plans = build_related_work_queries(
            "# Manuscript\nA task with two distinct technical components.",
            review_as_of="2026-10-01",
            llm_json=lambda *_args, **_kwargs: {
                "retrieval_profile": {
                    "task_terms": ["sequential event prediction"],
                    "mechanism_terms": [
                        "sparse key value memory",
                        "diffusion denoising compression",
                    ],
                    "data_protocol_terms": ["long history evaluation"],
                },
                "queries": [
                    {
                        "query": "a very long prose description of sparse memory that should not be sent literally",
                        "role": "method_mechanism",
                        "query_shape": "keyword_phrase",
                        "search_terms": ["sparse", "key-value", "memory"],
                    },
                    {
                        "query": "NamedOne, NamedTwo",
                        "role": "named_prior_work",
                        "methods": ["NamedOne", "NamedTwo"],
                    },
                ],
            },
        )
        self.assertEqual(plans[0].fallback_phrase, "sparse key-value memory")
        self.assertEqual(
            [item.fallback_phrase for item in plans[1:]],
            ["NamedOne", "NamedTwo"],
        )

    def test_feedback_query_plan_is_dynamic_bounded_and_deduplicated(self) -> None:
        def model(_system, user, *, max_tokens):
            self.assertIn("Roles already represented", user)
            self.assertIn("missing component", user)
            self.assertLessEqual(max_tokens, 700)
            return {
                "missing_categories": ["evaluation protocol", "alternative mechanism"],
                "queries": [
                    {
                        "query": "FocusedEval",
                        "role": "benchmark_or_evaluation",
                    },
                    {
                        "query": "AlternativeOne, AlternativeTwo",
                        "role": "alternative_formulation",
                        "methods": ["AlternativeOne", "AlternativeTwo"],
                    },
                    {
                        "query": "already searched",
                        "role": "problem_setting",
                    },
                    {
                        "query": "Fourth focused query",
                        "role": "method_mechanism",
                    },
                    {
                        "query": "Fifth query over budget",
                        "role": "method_mechanism",
                    },
                ],
            }

        plans, diagnostics = build_feedback_queries(
            [{"title": "Initial Candidate", "retrieval_role": "problem_setting"}],
            [{"title": "Selected Candidate", "retrieval_role": "problem_setting"}],
            original_queries=["already searched"],
            review_as_of="2026-10-01",
            llm_json=model,
            technical_coverage={
                "missing_mechanism_terms": ["missing component"],
                "sufficient": False,
            },
        )

        self.assertEqual(len(plans), 4)
        self.assertNotIn("already searched", [str(item) for item in plans])
        self.assertTrue(any("AlternativeOne" in str(item) for item in plans))
        self.assertTrue(any("AlternativeTwo" in str(item) for item in plans))
        self.assertTrue(all(2 <= len(str(item).split()) <= 5 for item in plans))
        self.assertEqual(diagnostics["status"], "planned")
        self.assertEqual(diagnostics["query_count"], 4)
        self.assertEqual(
            diagnostics["targeted_missing_mechanisms"], ["missing component"]
        )

    def test_feedback_rejects_cited_exact_title_but_keeps_neighbor_keywords(self) -> None:
        captured: dict[str, str] = {}

        def model(_system, user, *, max_tokens):
            captured["user"] = user
            return {
                "missing_categories": ["ranking mechanism"],
                "queries": [
                    {
                        "query": "Deep Reinforcement Learning for Information Retrieval",
                        "role": "named_prior_work",
                        "query_shape": "exact_title",
                    },
                    {
                        "query": "reinforcement retrieval",
                        "role": "method_mechanism",
                        "query_shape": "keyword_phrase",
                    },
                ],
            }

        plans, diagnostics = build_feedback_queries(
            [],
            [],
            original_queries=[
                RoutedQuery(
                    "information retrieval ranking",
                    query_shape="keyword_phrase",
                    fallback_phrase="information retrieval ranking",
                    role="problem_setting",
                    pro_field="keyword",
                    retrieval_profile={
                        "task_terms": ["information retrieval"],
                        "mechanism_terms": ["reinforcement ranking"],
                        "data_protocol_terms": [],
                    },
                )
            ],
            review_as_of="2026-10-01",
            llm_json=model,
            technical_coverage={
                "missing_mechanism_terms": ["reinforcement ranking"],
                "sufficient": False,
            },
            already_cited_controls=[
                {
                    "title": "Deep Reinforcement Learning for Information Retrieval",
                    "summary": "A reinforcement learning approach to retrieval ranking.",
                    "facet_alignment": {
                        "mechanism_aligned": True,
                        "task_aligned": True,
                    },
                    "bibliography_match": {
                        "reference_id": "R4",
                        "title_similarity": 1.0,
                    },
                }
            ],
        )

        self.assertEqual(
            [str(item) for item in plans],
            ["reinforcement ranking information retrieval"],
        )
        self.assertEqual(plans[0].query_subtype, "controlled_mechanism_domain")
        self.assertEqual(plans[0].mechanism_branch, "reinforcement ranking")
        self.assertEqual(plans[0].pro_field, "title")
        self.assertEqual(diagnostics["already_cited_query_rejections"], 1)
        self.assertEqual(diagnostics["query_source"], "model")
        self.assertIn("reinforcement learning approach", captured["user"])

    def test_feedback_compresses_non_title_queries_to_mechanism_plus_domain(self) -> None:
        original = RoutedQuery(
            "long history sequential recommendation",
            query_shape="keyword_phrase",
            fallback_phrase="long history sequential recommendation",
            role="problem_setting",
            pro_field="keyword",
            retrieval_profile={
                "domain_terms": ["personalized recommender systems"],
                "task_terms": ["long sequence recommendation"],
                "mechanism_terms": ["sparse denoising memory"],
                "data_protocol_terms": ["long user histories"],
            },
        )

        plans, diagnostics = build_feedback_queries(
            [],
            [],
            original_queries=[original],
            review_as_of="2026-10-01",
            llm_json=lambda *_args, **_kwargs: {
                "missing_categories": ["sparse denoising memory"],
                "queries": [
                    {
                        "query": (
                            "Which prior methods combine plug in sparse memory and "
                            "denoising compression for very long recommendation sequences?"
                        ),
                        "role": "method_mechanism",
                        "query_shape": "natural_language",
                    },
                    {
                        "query": "CandidateMethod",
                        "role": "named_prior_work",
                        "query_shape": "title_phrase",
                    },
                    {
                        "query": "A Complete Exact Prior Paper Title",
                        "role": "named_prior_work",
                        "query_shape": "exact_title",
                    },
                ],
            },
            technical_coverage={
                "missing_mechanism_terms": ["sparse denoising memory"],
                "sufficient": False,
            },
        )

        self.assertEqual(diagnostics["status"], "planned")
        exact = next(
            plan for plan in plans if getattr(plan, "query_shape", "") == "exact_title"
        )
        self.assertEqual(str(exact), "A Complete Exact Prior Paper Title")
        non_titles = [plan for plan in plans if plan is not exact]
        self.assertGreaterEqual(len(non_titles), 2)
        for plan in non_titles:
            words = str(plan).split()
            self.assertGreaterEqual(len(words), 2)
            self.assertLessEqual(len(words), 5)
            self.assertEqual(plan.query_shape, "keyword_phrase")
            self.assertEqual(plan.fallback_phrase, str(plan))
        long_plan = non_titles[0]
        self.assertIn("denoising", str(long_plan).casefold())
        self.assertIn("personalized recommender", str(long_plan).casefold())
        self.assertEqual(
            long_plan.query_subtype, "controlled_mechanism_domain"
        )
        self.assertEqual(long_plan.pro_field, "title")
        named_plan = next(plan for plan in non_titles if "CandidateMethod" in str(plan))
        self.assertIn("recommendation", str(named_plan).casefold())
        self.assertEqual(named_plan.query_subtype, "")

    def test_feedback_mechanism_query_uses_missing_mechanism_and_shortest_domain(self) -> None:
        original = RoutedQuery(
            "long click history prediction",
            query_shape="keyword_phrase",
            fallback_phrase="long click history prediction",
            role="problem_setting",
            pro_field="keyword",
            retrieval_profile={
                "domain_terms": [
                    "interactive personalized recommendation systems",
                    "sequential recommendation",
                ],
                "task_terms": ["long click history"],
                "mechanism_terms": ["diffusion denoising"],
                "data_protocol_terms": ["long behavior sequences"],
            },
        )

        plans, diagnostics = build_feedback_queries(
            [],
            [],
            original_queries=[original],
            review_as_of="2026-10-29",
            llm_json=lambda *_args, **_kwargs: {
                "missing_categories": ["diffusion denoising"],
                "queries": [
                    {
                        "query": "diffusion models long click history",
                        "role": "method_mechanism",
                        "facet": "mechanism",
                        "query_shape": "keyword_phrase",
                    }
                ],
            },
            technical_coverage={
                "missing_mechanism_terms": ["diffusion denoising"],
                "sufficient": False,
            },
        )

        self.assertEqual(diagnostics["status"], "planned")
        self.assertEqual([str(plan) for plan in plans], [
            "diffusion denoising sequential recommendation"
        ])
        self.assertEqual(plans[0].query_shape, "keyword_phrase")
        self.assertEqual(plans[0].query_subtype, "controlled_mechanism_domain")
        self.assertEqual(plans[0].pro_field, "title")
        self.assertNotIn("long click history", str(plans[0]).casefold())

    def test_feedback_deterministically_completes_every_missing_mechanism_branch(self) -> None:
        profile = {
            "domain_terms": [
                "news recommendation",
                "text-rich sequential recommendation",
                "long-sequence recommendation",
            ],
            "task_terms": [
                "user preference modeling",
                "long-term interest memory",
                "noisy history compression",
            ],
            "mechanism_terms": [
                "key-value memory network",
                "learnable latent tokens",
                "diffusion denoising objective",
                "semantic topic profiles",
                "profile bucket hashing",
            ],
            "data_protocol_terms": ["large news benchmark"],
        }

        def original(query: str, role: str, facet: str) -> RoutedQuery:
            return RoutedQuery(
                query,
                query_shape="keyword_phrase",
                fallback_phrase=query,
                role=role,
                pro_field="keyword",
                facet=facet,
                retrieval_profile=profile,
            )

        missing = list(profile["mechanism_terms"])
        plans, diagnostics = build_feedback_queries(
            [],
            [],
            original_queries=[
                original(
                    "long-sequence text-rich news recommendation",
                    "problem_setting",
                    "task",
                ),
                original(
                    "key-value memory user preference prototypes",
                    "method_mechanism",
                    "mechanism",
                ),
                original(
                    "diffusion denoising recommendation latent states",
                    "method_mechanism",
                    "mechanism",
                ),
            ],
            review_as_of="2026-10-29",
            llm_json=lambda *_args, **_kwargs: {
                "missing_categories": ["incomplete mechanism coverage"],
                "queries": [
                    {
                        "query": "key-value memory user preference modeling",
                        "role": "method_mechanism",
                        "facet": "mechanism",
                        "query_shape": "keyword_phrase",
                    },
                    {
                        "query": "learnable latent long-term interest memory",
                        "role": "method_mechanism",
                        "facet": "mechanism",
                        "query_shape": "keyword_phrase",
                    },
                    {
                        "query": "semantic topic news recommendation",
                        "role": "method_mechanism",
                        "facet": "mechanism",
                        "query_shape": "keyword_phrase",
                    },
                    {
                        "query": "large news benchmark protocol",
                        "role": "benchmark_or_evaluation",
                        "facet": "data_protocol",
                        "query_shape": "keyword_phrase",
                    },
                ],
            },
            technical_coverage={
                "missing_mechanism_terms": missing,
                "sufficient": False,
            },
        )

        controlled = {
            plan.mechanism_branch: str(plan)
            for plan in plans
            if plan.query_subtype == "controlled_mechanism_domain"
        }
        self.assertEqual(set(controlled), set(missing))
        self.assertEqual(
            controlled["diffusion denoising objective"],
            "diffusion denoising sequential recommendation",
        )
        self.assertNotIn(
            "diffusion denoising objective",
            diagnostics["unplanned_missing_mechanisms"],
        )
        self.assertEqual(
            diagnostics["feedback_query_budget"]["maximum_queries"], 8
        )
        self.assertEqual(
            diagnostics["feedback_query_budget"]["planned_mechanism_branches"],
            5,
        )
        self.assertEqual(diagnostics["mechanism_completion_added_count"], 2)
        self.assertLessEqual(len(plans), 8)

    def test_feedback_budget_reports_deferred_mechanism_without_silent_loss(self) -> None:
        mechanisms = [
            f"{name} routing"
            for name in (
                "adaptive", "contrastive", "diffusive", "hierarchical",
                "iterative", "modular", "probabilistic", "recursive",
                "spectral",
            )
        ]
        original = RoutedQuery(
            "structured document analysis",
            query_shape="keyword_phrase",
            fallback_phrase="structured document analysis",
            role="problem_setting",
            pro_field="keyword",
            retrieval_profile={
                "domain_terms": ["structured document analysis"],
                "task_terms": ["long document reasoning"],
                "mechanism_terms": mechanisms,
                "data_protocol_terms": [],
            },
        )

        plans, diagnostics = build_feedback_queries(
            [],
            [],
            original_queries=[original],
            review_as_of="2026-10-29",
            llm_json=lambda *_args, **_kwargs: {
                "missing_categories": [],
                "queries": [],
            },
            technical_coverage={
                "missing_mechanism_terms": mechanisms,
                "sufficient": False,
            },
        )

        self.assertEqual(len(plans), 8)
        self.assertTrue(
            all(
                plan.query_subtype == "controlled_mechanism_domain"
                for plan in plans
            )
        )
        self.assertEqual(
            diagnostics["deferred_missing_mechanisms"], [mechanisms[-1]]
        )
        self.assertEqual(
            diagnostics["target_conservation"],
            {
                "total": 9,
                "queried": 8,
                "deferred_budget": 1,
                "unqueryable": 0,
                "conserved": True,
            },
        )

    def test_core_technical_coverage_tracks_each_method_component(self) -> None:
        profile = {
            "task_terms": ["event recommendation"],
            "mechanism_terms": ["sparse memory", "denoising compression"],
            "data_protocol_terms": ["long history"],
        }
        selected = [
            {
                "tier": "core",
                "missing_status": "uncited",
                "verification": {"relevance": {"passed": True}},
                "facet_alignment": {
                    "task_aligned": True,
                    "mechanism_aligned": True,
                    "matched_profile_terms": {
                        "mechanism_terms": ["sparse memory"]
                    },
                },
            }
        ]
        coverage = technical_coverage_summary(selected, profile)
        self.assertEqual(coverage["covered_mechanism_terms"], ["sparse memory"])
        self.assertEqual(
            coverage["missing_mechanism_terms"], ["denoising compression"]
        )
        self.assertFalse(coverage["sufficient"])

    def test_unknown_core_does_not_satisfy_technical_coverage(self) -> None:
        profile = {
            "task_terms": ["information retrieval"],
            "mechanism_terms": ["reinforcement ranking"],
            "data_protocol_terms": [],
        }
        coverage = technical_coverage_summary(
            [
                {
                    "tier": "core",
                    "missing_status": "unknown",
                    "verification": {"relevance": {"passed": True}},
                    "facet_alignment": {
                        "task_aligned": True,
                        "mechanism_aligned": True,
                        "matched_profile_terms": {
                            "mechanism_terms": ["reinforcement ranking"]
                        },
                    },
                }
            ],
            profile,
        )
        self.assertEqual(coverage["verified_core_count"], 0)
        self.assertEqual(
            coverage["missing_mechanism_terms"], ["reinforcement ranking"]
        )
        self.assertFalse(coverage["sufficient"])

    def test_year_only_cutoff_year_is_retained_as_uncertain(self) -> None:
        kept, diagnostics = filter_retrieval_records(
            [
                {
                    "title": "Earlier Related Work",
                    "published": "2024",
                    "date_precision": "year",
                    "relation_role": "cites_target",
                    "summary": "relevant retrieval method",
                    "query": "retrieval method",
                },
                {
                    "title": "Same Year Date Unknown",
                    "published": "2025",
                    "date_precision": "year",
                    "relation_role": "cites_target",
                    "summary": "relevant retrieval method",
                    "query": "retrieval method",
                },
            ],
            local_title="Local Retrieval Manuscript",
            review_as_of="2025-09-24",
        )
        self.assertEqual(
            [item["title"] for item in kept],
            ["Earlier Related Work", "Same Year Date Unknown"],
        )
        self.assertEqual(kept[1]["time_admissibility"], "uncertain")
        self.assertEqual(kept[1]["cutoff_status"], "uncertain_same_year")
        self.assertEqual(diagnostics["same_year_date_uncertain_records"], 1)

    def test_local_bibliography_can_verify_year_only_cutoff_candidate(self) -> None:
        kept, diagnostics = filter_retrieval_records(
            [
                {
                    "title": "Exact Named Method for Event Prediction",
                    "published": "2026",
                    "summary": "A method for event prediction.",
                    "query": "Exact Named Method for Event Prediction",
                    "query_role": "named_prior_work",
                    "query_shape": "exact_title",
                    "source": "AMiner Open Platform",
                    "url": "https://www.aminer.cn/pub/000000000000000000000001",
                }
            ],
            local_title="A New Event Prediction Manuscript",
            review_as_of="2026-10-01",
            manuscript_profile={
                "task_terms": ["event prediction"],
                "mechanism_terms": [],
                "data_protocol_terms": [],
            },
            local_references=[
                {
                    "reference_id": "R7",
                    "candidate_title": "Exact Named Method for Event Prediction",
                }
            ],
            bibliography_ready=True,
        )
        self.assertEqual(len(kept), 1)
        self.assertEqual(
            kept[0]["cutoff_status"], "admissible_via_local_citation"
        )
        self.assertEqual(
            kept[0]["local_citation_match"]["reference_id"], "R7"
        )
        self.assertEqual(diagnostics["local_citation_supported_records"], 1)

    def test_ambiguous_local_title_is_unknown_not_already_cited_control(self) -> None:
        kept, diagnostics = filter_retrieval_records(
            [
                {
                    "title": "A Precise Method for Structured Scientific Retrieval",
                    "published": "2025-01-01",
                    "summary": "A structured scientific retrieval method.",
                    "query": "structured scientific retrieval",
                    "query_role": "method_mechanism",
                    "source": "AMiner Open Platform",
                    "url": "https://www.aminer.cn/pub/000000000000000000000009",
                }
            ],
            local_title="A New Scientific Retrieval Manuscript",
            review_as_of="2026-10-01",
            local_references=[
                {
                    "reference_id": "R9",
                    "candidate_title": (
                        "A Precise Framework for Structured Scientific Retrieval"
                    ),
                }
            ],
            bibliography_ready=True,
        )
        self.assertEqual(len(kept), 1)
        self.assertNotIn("local_citation_match", kept[0])
        self.assertEqual(
            kept[0]["local_citation_ambiguity"]["coverage_status"],
            "ambiguous_match",
        )
        self.assertEqual(diagnostics["local_citation_supported_records"], 0)
        self.assertEqual(diagnostics["local_citation_ambiguous_records"], 1)

    def test_unready_ledger_exact_match_is_only_a_provisional_seed(self) -> None:
        kept, diagnostics = filter_retrieval_records(
            [
                {
                    "title": "Exact Cited Retrieval Method",
                    "published": "2024-01-01",
                    "summary": "A retrieval method.",
                    "query": "exact cited retrieval method",
                    "query_role": "named_prior_work",
                    "source": "AMiner Open Platform",
                    "url": "https://www.aminer.cn/pub/000000000000000000000031",
                }
            ],
            local_title="A New Retrieval Manuscript",
            review_as_of="2026-10-01",
            local_references=[
                {
                    "reference_id": "R31",
                    "candidate_title": "Exact Cited Retrieval Method",
                    "resolution_eligible": True,
                }
            ],
            bibliography_ready=False,
        )
        self.assertEqual(len(kept), 1)
        self.assertNotIn("local_citation_match", kept[0])
        self.assertEqual(
            kept[0]["local_citation_provisional_seed"]["coverage_status"],
            "provisional_untrusted_ledger",
        )
        self.assertEqual(diagnostics["local_citation_supported_records"], 0)
        self.assertEqual(diagnostics["local_citation_provisional_seed_records"], 1)

    def test_pipeline_reference_gate_requires_ready_ledger_and_eligible_entry(self) -> None:
        eligible = {
            "id": "R1",
            "candidate_title": "Eligible Reference",
            "resolution_eligible": True,
        }
        ineligible = {
            "id": "R2",
            "candidate_title": "Ineligible Reference",
            "resolution_eligible": False,
        }
        references, ready = _retrieval_reference_inputs(
            {
                "status": "parse_quality_insufficient",
                "records": [eligible],
            }
        )
        self.assertEqual(references, [eligible])
        self.assertFalse(ready)

        references, ready = _retrieval_reference_inputs(
            {
                "status": "ready_for_resolution",
                "records": [ineligible],
            }
        )
        self.assertEqual(references, [])
        self.assertFalse(ready)

        references, ready = _retrieval_reference_inputs(
            {
                "status": "ready_for_resolution",
                "records": [eligible, ineligible],
            }
        )
        self.assertEqual(references, [eligible])
        self.assertTrue(ready)

    def test_facet_alignment_marks_mechanism_only_cross_domain_candidate(self) -> None:
        kept, diagnostics = filter_retrieval_records(
            [
                {
                    "title": "Key Value Memory for Medical Video Segmentation",
                    "published": "2025-01-01",
                    "summary": "A gated sparse memory for echocardiography masks.",
                    "query": "sparse key value memory",
                    "query_role": "method_mechanism",
                    "query_shape": "keyword_phrase",
                    "source": "AMiner Open Platform",
                    "url": "https://www.aminer.cn/pub/000000000000000000000001",
                }
            ],
            local_title="Long History Recommendation with Sparse Memory",
            review_as_of="2026-10-01",
            manuscript_profile={
                "task_terms": ["sequential recommendation"],
                "mechanism_terms": ["sparse key value memory"],
                "data_protocol_terms": ["long user history"],
            },
        )
        self.assertEqual(len(kept), 1)
        alignment = kept[0]["facet_alignment"]
        self.assertTrue(alignment["mechanism_only_cross_domain"])
        self.assertFalse(alignment["publication_fit"])
        self.assertEqual(diagnostics["mechanism_only_cross_domain_records"], 1)

    def test_filter_reserves_one_qualified_hit_per_named_query(self) -> None:
        broad = [
            {
                "title": f"Grounded Event Prediction Architecture {index}",
                "published": "2025-01-01",
                "summary": "Grounded event prediction with retrieval feedback.",
                "query": "grounded event prediction",
                "query_role": "problem_setting",
                "query_shape": "keyword_phrase",
            }
            for index in range(20)
        ]
        named = [
            {
                "title": "NamedAlpha: An Expanded Technical Title",
                "published": "2025-01-01",
                "summary": "",
                "query": "NamedAlpha",
                "query_role": "named_prior_work",
                "query_shape": "title_phrase",
            },
            {
                "title": "NamedBeta: Another Expanded Technical Title",
                "published": "2025-01-01",
                "summary": "",
                "query": "NamedBeta",
                "query_role": "direct_baseline",
                "query_shape": "title_phrase",
            },
        ]
        kept, diagnostics = filter_retrieval_records(
            broad + named,
            local_title="A Grounded Event Prediction Manuscript",
            review_as_of="2026-10-01",
            max_records=10,
            manuscript_profile={
                "task_terms": ["grounded event prediction"],
                "mechanism_terms": ["retrieval feedback"],
                "data_protocol_terms": [],
            },
        )
        titles = [item["title"] for item in kept]
        self.assertIn("NamedAlpha: An Expanded Technical Title", titles)
        self.assertIn("NamedBeta: Another Expanded Technical Title", titles)
        self.assertEqual(diagnostics["named_query_representative_count"], 2)

    def test_year_only_before_cutoff_is_admissible_for_any_provider(self) -> None:
        kept, diagnostics = filter_retrieval_records(
            [
                {
                    "title": "Ordinary Search Result With Year Only",
                    "published": "2024",
                    "summary": "relevant retrieval method",
                    "query": "retrieval method",
                    "source": "OpenAlex fallback",
                }
            ],
            local_title="Local Retrieval Manuscript",
            review_as_of="2025-09-24",
        )
        self.assertEqual(
            [item["title"] for item in kept],
            ["Ordinary Search Result With Year Only"],
        )
        self.assertEqual(kept[0]["time_admissibility"], "admissible")
        self.assertEqual(diagnostics["undated_records_removed"], 0)
        self.assertNotIn("same_year_date_uncertain_records", diagnostics)

    def test_exact_same_year_date_is_hard_filtered_against_cutoff(self) -> None:
        kept, diagnostics = filter_retrieval_records(
            [
                {
                    "title": "Available Before Exact Review Cutoff",
                    "published": "2026-09-30",
                    "summary": "grounded retrieval method",
                    "query": "grounded retrieval",
                    "source": "AMiner Open Platform",
                },
                {
                    "title": "Published After Exact Review Cutoff",
                    "published": "2026-10-02",
                    "summary": "grounded retrieval method",
                    "query": "grounded retrieval",
                    "source": "AMiner Open Platform",
                },
            ],
            local_title="Local Grounded Retrieval Manuscript",
            review_as_of="2026-10-01",
        )

        self.assertEqual(
            [item["title"] for item in kept],
            ["Available Before Exact Review Cutoff"],
        )
        self.assertEqual(kept[0]["date_precision"], "day")
        self.assertEqual(kept[0]["cutoff_status"], "admissible_exact_date")
        self.assertEqual(diagnostics["future_records_removed"], 1)

    def test_cutoff_self_match_and_duplicate_filters(self) -> None:
        rows = [
            {"title": "Local Paper Exact Title", "published": "2025-01-01", "summary": "x", "query": "local"},
            {"title": "Useful Distinct Retrieval Result", "published": "2024-03-02", "summary": "local query", "query": "retrieval"},
            {"title": "Useful Distinct Retrieval Result", "published": "2024-03-02", "summary": "duplicate", "query": "retrieval"},
            {"title": "Future Result for Retrieval", "published": "2027-01-01", "summary": "future", "query": "retrieval"},
            {"title": "Undated Retrieval Result", "published": "", "summary": "undated", "query": "retrieval"},
        ]
        kept, diagnostics = filter_retrieval_records(
            rows, local_title="Local Paper Exact Title", review_as_of="2026-10-15"
        )
        self.assertEqual([row["title"] for row in kept], ["Useful Distinct Retrieval Result"])
        self.assertEqual(diagnostics["self_records_removed"], 1)
        self.assertEqual(diagnostics["duplicate_records_removed"], 1)
        self.assertEqual(diagnostics["future_records_removed"], 1)
        self.assertEqual(diagnostics["undated_records_removed"], 1)

    def test_openalex_is_only_used_after_empty_arxiv_selection(self) -> None:
        class Client:
            def __init__(self, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        arxiv_calls: list[str] = []
        openalex_calls: list[str] = []
        selection_calls = 0

        def arxiv(query, _client):
            arxiv_calls.append(query)
            return [{
                "title": "A Candidate From Arxiv Search",
                "published": "2024-01-01",
                "summary": "distinct topic",
                "query": query,
                "source": "arXiv API",
                "url": "https://arxiv.org/abs/2401.00001",
            }]

        def openalex(query, _client):
            openalex_calls.append(query)
            return [{
                "title": "A Candidate From Openalex Fallback",
                "published": "2024-02-01",
                "summary": "distinct topic",
                "query": query,
                "source": "OpenAlex fallback",
                "url": "https://openalex.org/W1",
            }]

        def select(candidates, **_kwargs):
            nonlocal selection_calls
            selection_calls += 1
            return ([], {"selection_status": "completed", "selected_record_count": 0}) if selection_calls == 1 else ([candidates[-1]], {"selection_status": "completed", "selected_record_count": 1})

        selected, diagnostics = retrieve_related_work(
            ["specific retrieval query"],
            local_title="A Local Manuscript",
            review_as_of="2026-01-01",
            proxy=None,
            select_relevant=select,
            provider="arxiv-openalex",
            client_factory=Client,
            sleep=lambda _seconds: None,
            arxiv_search_fn=arxiv,
            openalex_search_fn=openalex,
        )
        self.assertEqual(arxiv_calls, ["specific retrieval query"])
        self.assertEqual(openalex_calls, ["specific retrieval query"])
        self.assertEqual(selection_calls, 2)
        self.assertEqual(selected[0]["source"], "OpenAlex fallback")
        self.assertEqual(diagnostics["status"], "passed")

    def test_aminer_feedback_search_is_bounded_to_two_rounds_and_one_keyword_call(self) -> None:
        search_calls: list[dict[str, object]] = []
        selection_calls = 0

        def candidate(index: int, query: str, role: str) -> dict[str, str]:
            return {
                "aminer_pub_id": f"{index:024x}",
                "title": f"Grounded Retrieval Candidate {index}",
                "published": "2024-01-01",
                "summary": "A grounded retrieval method with evaluation evidence.",
                "query": query,
                "query_role": role,
                "source": "AMiner Open Platform",
                "url": f"https://www.aminer.cn/pub/{index:024x}",
            }

        def aminer_search(queries, *, allow_keyword_expansion, **_kwargs):
            round_number = len(search_calls) + 1
            search_calls.append(
                {
                    "queries": [str(item) for item in queries],
                    "allow_keyword_expansion": allow_keyword_expansion,
                }
            )
            if round_number == 1:
                rows = [candidate(1, str(queries[0]), "method_mechanism")]
                api_calls = [
                    {"api": "paper_search_pro", "status": "completed"},
                    {"api": "paper_list_by_keywords", "status": "completed"},
                ]
            else:
                rows = [
                    candidate(2, str(queries[0]), "benchmark_or_evaluation"),
                    candidate(3, str(queries[0]), "alternative_formulation"),
                ]
                api_calls = [{"api": "paper_search_pro", "status": "completed"}]
            return rows, {
                "provider": "aminer_open_platform",
                "provider_errors": [],
                "provider_warnings": [],
                "downgrades": [],
                "api_calls": api_calls,
                "cost": {
                    "currency": "CNY",
                    "estimated_total_yuan": 0.1,
                    "calls": len(api_calls),
                },
                "raw_record_count": len(rows),
            }

        def select(candidates, **kwargs):
            nonlocal selection_calls
            selection_calls += 1
            roles = ["method_precedent", "benchmark_or_evaluation", "alternative_formulation"]
            return select_relevant_records(candidates, **kwargs, llm_json=lambda *a, **k: {
                "selected": [{"candidate_id": f"R{i}", "relevance": "high",
                              "comparison_role": roles[min(i-1, len(roles)-1)],
                              "reason": "The abstract describes grounded retrieval and evaluation."}
                             for i in range(1, len(candidates)+1)]})

        feedback_plan_calls = 0

        def feedback(_candidates, _selected, **_kwargs):
            nonlocal feedback_plan_calls
            feedback_plan_calls += 1
            return [
                RoutedQuery(
                    "grounded retrieval evaluation protocol",
                    query_shape="keyword_phrase",
                    fallback_phrase="grounded retrieval evaluation protocol",
                    role="benchmark_or_evaluation",
                    pro_field="keyword",
                )
            ], {"status": "planned", "missing_categories": ["evaluation protocol"]}

        selected, diagnostics = retrieve_related_work(
            [
                RoutedQuery(
                    "grounded retrieval method",
                    query_shape="keyword_phrase",
                    fallback_phrase="grounded retrieval method",
                    retrieval_profile={"domain_terms": ["grounded retrieval"],
                                       "task_terms": ["grounded retrieval"],
                                       "mechanism_terms": ["grounded retrieval"]},
                    role="method_mechanism",
                    pro_field="keyword",
                )
            ],
            local_title="A Local Grounded Retrieval Manuscript",
            review_as_of="2026-01-01",
            proxy=None,
            select_relevant=select,
            plan_feedback=feedback,
            provider="aminer",
            aminer_api_key="aminer-key",
            aminer_search_fn=aminer_search,
        )

        self.assertEqual(len(search_calls), 2)
        self.assertEqual(
            [call["allow_keyword_expansion"] for call in search_calls],
            [True, False],
        )
        self.assertEqual(feedback_plan_calls, 1)
        self.assertEqual(selection_calls, 2)
        self.assertEqual(len(selected), 3)
        self.assertEqual(diagnostics["feedback_retrieval"]["rounds_completed"], 2)
        self.assertEqual(
            diagnostics["feedback_retrieval"]["stop_reason"],
            "maximum_rounds_reached_after_verified_union",
        )
        self.assertEqual(
            sum(
                call.get("api") == "paper_list_by_keywords"
                for call in diagnostics["provider_diagnostics"]["api_calls"]
            ),
            1,
        )

    def test_initial_raw_hit_without_core_still_gets_mechanism_feedback_query(self) -> None:
        profile = {
            "domain_terms": ["event sequence prediction"],
            "task_terms": ["long event history"],
            "mechanism_terms": ["sparse memory", "diffusion denoising"],
            "data_protocol_terms": ["long sequence evaluation"],
        }
        original_queries = [
            RoutedQuery(
                "sparse memory long event history",
                query_shape="keyword_phrase",
                fallback_phrase="sparse memory long event history",
                role="method_mechanism",
                pro_field="keyword",
                facet="mechanism",
                retrieval_profile=profile,
            ),
            RoutedQuery(
                "diffusion denoising event sequence states",
                query_shape="keyword_phrase",
                fallback_phrase="diffusion denoising event sequence states",
                role="method_mechanism",
                pro_field="keyword",
                facet="mechanism",
                retrieval_profile=profile,
            ),
        ]
        search_rounds: list[list[object]] = []

        def aminer_search(queries, *, allow_keyword_expansion, **_kwargs):
            search_rounds.append(list(queries))
            rows = []
            if len(search_rounds) == 1:
                rows = [
                    {
                        "aminer_pub_id": "1123456789abcdef01234567",
                        "title": "Diffusion Compression for Event Sequences",
                        "published": "2025-01-01",
                        "summary": "Diffusion denoising for long event sequence states.",
                        "query": str(queries[1]),
                        "query_role": "method_mechanism",
                        "source": "AMiner Open Platform",
                        "url": "https://www.aminer.cn/pub/1123456789abcdef01234567",
                    }
                ]
            return rows, {
                "provider": "aminer_open_platform",
                "provider_errors": [],
                "provider_warnings": [],
                "downgrades": [],
                "api_calls": [],
                "cost": {
                    "currency": "CNY",
                    "estimated_total_yuan": 0,
                    "calls": 0,
                },
                "raw_record_count": len(rows),
            }

        def feedback(candidates, selected, **kwargs):
            return build_feedback_queries(
                candidates,
                selected,
                llm_json=lambda *_args, **_model_kwargs: {
                    "missing_categories": ["mechanism coverage"],
                    "queries": [
                        {
                            "query": "sparse memory long event history",
                            "role": "method_mechanism",
                            "facet": "mechanism",
                            "query_shape": "keyword_phrase",
                        }
                    ],
                },
                **kwargs,
            )

        _selected, diagnostics = retrieve_related_work(
            original_queries,
            local_title="A Different Event Prediction Manuscript",
            review_as_of="2026-10-29",
            proxy=None,
            select_relevant=lambda *_args, **_kwargs: (
                [],
                {
                    "selection_status": "completed",
                    "selected_record_count": 0,
                    "core_record_count": 0,
                    "selected_roles": {},
                },
            ),
            plan_feedback=feedback,
            provider="aminer",
            aminer_api_key="aminer-key",
            aminer_search_fn=aminer_search,
        )

        self.assertEqual(len(search_rounds), 2)
        second_round = {
            item.mechanism_branch: str(item)
            for item in search_rounds[1]
            if item.query_subtype == "controlled_mechanism_domain"
        }
        self.assertEqual(
            second_round["diffusion denoising"],
            "diffusion denoising event sequence",
        )
        feedback_diagnostics = diagnostics["feedback_retrieval"]
        self.assertEqual(
            feedback_diagnostics["target_conservation"]["queried"], 2
        )
        self.assertTrue(
            feedback_diagnostics["target_conservation"]["conserved"]
        )

    def test_already_cited_controls_trigger_feedback_but_never_enter_output(self) -> None:
        search_calls = 0
        feedback_calls = 0
        query = RoutedQuery(
            "grounded retrieval feedback",
            query_shape="keyword_phrase",
            fallback_phrase="grounded retrieval feedback",
            role="method_mechanism",
            pro_field="keyword",
            retrieval_profile={
                "domain_terms": ["grounded retrieval"],
                "task_terms": ["grounded retrieval"],
                "mechanism_terms": ["retrieval feedback"],
                "data_protocol_terms": [],
            },
        )

        def aminer_search(queries, **_kwargs):
            nonlocal search_calls
            search_calls += 1
            if search_calls == 1:
                rows = [
                    {
                        "title": "Cited Grounded Retrieval Feedback Method",
                        "published": "2024-01-01",
                        "summary": "Grounded retrieval with feedback.",
                        "query": str(queries[0]),
                        "query_role": "method_mechanism",
                        "source": "AMiner Open Platform",
                        "url": "https://www.aminer.cn/pub/000000000000000000000011",
                    }
                ]
            else:
                rows = [
                    {
                        "title": "Policy-Guided Ranking with Iterative User Evidence",
                        "published": "2025-01-01",
                        "summary": "A grounded retrieval task using iterative feedback.",
                        "query": str(queries[0]),
                        "query_role": "method_mechanism",
                        "source": "AMiner Open Platform",
                        "url": "https://www.aminer.cn/pub/000000000000000000000012",
                    }
                ]
            return rows, {
                "provider": "aminer_open_platform",
                "provider_errors": [],
                "provider_warnings": [],
                "downgrades": [],
                "api_calls": [],
                "cost": {"currency": "CNY", "estimated_total_yuan": 0.0, "calls": 0},
                "raw_record_count": len(rows),
            }

        def select(candidates, **kwargs):
            return select_relevant_records(candidates, **kwargs, llm_json=lambda *a, **k: {
                "selected": [{"candidate_id": f"R{i}", "relevance": "high",
                              "comparison_role": "method_precedent", "relation_scope": "same_domain",
                              "domain_evidence": {"primary_domain": "grounded retrieval",
                                  "manuscript_quote": kwargs["local_title"],
                                  "candidate_quote": candidates[i-1]["summary"],
                                  "explanation": "Both address grounded retrieval with user feedback."},
                              "reason": "The abstract supports an adjacent retrieval method."}
                             for i in range(1, len(candidates)+1)]})

        # Deliberately omit **kwargs to verify compatibility with the legacy
        # feedback callback contract.
        def feedback(
            candidates,
            selected,
            *,
            original_queries,
            review_as_of,
            technical_coverage,
        ):
            nonlocal feedback_calls
            feedback_calls += 1
            self.assertEqual(selected, [])
            self.assertTrue(any(item.get("local_citation_match") for item in candidates))
            self.assertFalse(technical_coverage["sufficient"])
            return [
                RoutedQuery(
                    "adjacent grounded retrieval feedback",
                    query_shape="keyword_phrase",
                    fallback_phrase="adjacent grounded retrieval feedback",
                    role="method_mechanism",
                    pro_field="keyword",
                )
            ], {"status": "planned", "missing_categories": ["retrieval feedback"]}

        selected, diagnostics = retrieve_related_work(
            [query],
            local_title="A New Grounded Retrieval Manuscript",
            review_as_of="2026-10-01",
            proxy=None,
            select_relevant=select,
            plan_feedback=feedback,
            known_cited_controls=[
                {
                    "title": "Relation-Controlled Prior Work",
                    "published": "2023-01-01",
                    "summary": "A cited work from the target reference graph.",
                    "query": str(query),
                    "query_role": "named_prior_work",
                    "relation_role": "target_references",
                    "relation_evidence": {"relation": "target_cites_candidate"},
                    "source": "AMiner Open Platform paper/relation",
                    "url": "https://www.aminer.cn/pub/000000000000000000000013",
                }
            ],
            local_references=[
                {
                    "reference_id": "R11",
                    "candidate_title": "Cited Grounded Retrieval Feedback Method",
                },
                {"reference_id": "R13", "candidate_title": "Relation-Controlled Prior Work"}
            ],
            bibliography_ready=True,
            provider="aminer",
            aminer_api_key="aminer-key",
            aminer_search_fn=aminer_search,
        )

        self.assertEqual(search_calls, 2)
        self.assertEqual(feedback_calls, 1)
        self.assertEqual(
            [item["title"] for item in selected],
            ["Policy-Guided Ranking with Iterative User Evidence"],
        )
        self.assertEqual(selected[0]["missing_status"], "uncited")
        self.assertEqual(diagnostics["already_cited_control_count"], 2)
        self.assertTrue(
            all(
                item["title"] not in diagnostics["already_cited_control_titles"]
                for item in selected
            )
        )

    def test_four_known_pub_ids_are_removed_before_top40_and_force_feedback(self) -> None:
        controls = [
            {
                "title": f"Known Cited Control {index}",
                "aminer_pub_id": f"{index:024x}",
                "relation_role": "target_references",
                "summary": "A known cited retrieval method.",
            }
            for index in range(1, 5)
        ]
        search_calls = 0
        selection_inputs: list[list[str]] = []

        def aminer_search(queries, **_kwargs):
            nonlocal search_calls
            search_calls += 1
            if search_calls == 1:
                rows = [
                    {
                        "title": control["title"],
                        "aminer_pub_id": control["aminer_pub_id"],
                        "published": "2024-01-01",
                        "summary": "A duplicate search hit for known cited work.",
                        "query": str(queries[0]),
                        "query_role": "method_mechanism",
                        "source": "AMiner Open Platform",
                        "url": f"https://www.aminer.cn/pub/{control['aminer_pub_id']}",
                    }
                    for control in controls
                ]
            else:
                rows = [
                    {
                        "title": "New Adjacent Retrieval Policy",
                        "aminer_pub_id": "000000000000000000000099",
                        "published": "2025-01-01",
                        "summary": "A task-aligned adjacent retrieval policy.",
                        "query": str(queries[0]),
                        "query_role": "method_mechanism",
                        "source": "AMiner Open Platform",
                        "url": "https://www.aminer.cn/pub/000000000000000000000099",
                    }
                ]
            return rows, {
                "provider": "aminer_open_platform",
                "provider_errors": [],
                "provider_warnings": [],
                "downgrades": [],
                "api_calls": [],
                "cost": {"currency": "CNY", "estimated_total_yuan": 0.0, "calls": 0},
                "raw_record_count": len(rows),
            }

        def select(candidates, **kwargs):
            selection_inputs.append([str(item.get("title")) for item in candidates])
            return select_relevant_records(candidates, **kwargs, llm_json=lambda *a, **k: {
                "selected": [{"candidate_id": f"R{i}", "relevance": "high",
                              "comparison_role": "method_precedent",
                              "reason": "The abstract supports an adjacent retrieval method."}
                             for i in range(1, len(candidates)+1)]})

        selected, diagnostics = retrieve_related_work(
            [
                RoutedQuery(
                    "adjacent retrieval policy",
                    query_shape="keyword_phrase",
                    fallback_phrase="adjacent retrieval policy",
                    role="method_mechanism",
                    pro_field="keyword",
                    retrieval_profile={
                        "task_terms": ["retrieval policy"],
                        "mechanism_terms": ["adjacent retrieval"],
                        "data_protocol_terms": [],
                    },
                )
            ],
            local_title="A New Retrieval Policy Manuscript",
            review_as_of="2026-10-01",
            proxy=None,
            select_relevant=select,
            plan_feedback=lambda *_args, **_kwargs: (
                [
                    RoutedQuery(
                        "neighbor retrieval policy",
                        query_shape="keyword_phrase",
                        fallback_phrase="neighbor retrieval policy",
                        role="method_mechanism",
                        pro_field="keyword",
                    )
                ],
                {"status": "planned", "missing_categories": ["adjacent retrieval"]},
            ),
            known_cited_controls=controls,
            local_references=[
                {
                    "reference_id": "R99",
                    "candidate_title": "An Unrelated Eligible Reference",
                    "resolution_eligible": True,
                }
            ] + [{"reference_id": f"B{i}", "candidate_title": c["title"]} for i, c in enumerate(controls)],
            bibliography_ready=True,
            provider="aminer",
            aminer_api_key="aminer-key",
            aminer_search_fn=aminer_search,
        )

        self.assertEqual(search_calls, 2)
        self.assertEqual(selection_inputs[0], [])
        self.assertEqual(selection_inputs[1], ["New Adjacent Retrieval Policy"])
        self.assertEqual([item["title"] for item in selected], ["New Adjacent Retrieval Policy"])
        self.assertEqual(diagnostics["known_control_discovery_match_count"], 4)
        self.assertEqual(diagnostics["already_cited_control_count"], 4)
        self.assertEqual(diagnostics["raw_record_count"], 1)

    def test_known_control_title_fallback_requires_no_shared_stable_id(self) -> None:
        remaining, matches = _exclude_known_control_matches(
            [
                {
                    "title": "A Precise Cited Retrieval Architecture",
                    "published": "2024-01-01",
                }
            ],
            [{"title": "A Precise Cited Retrieval Architecture"}],
        )
        self.assertEqual(remaining, [])
        self.assertEqual(matches[0]["match_type"], "title_high_similarity_without_shared_id")
        self.assertEqual(matches[0]["title_similarity"], 1.0)

        remaining, matches = _exclude_known_control_matches(
            [
                {
                    "title": "A Precise Cited Retrieval Architecture",
                    "aminer_pub_id": "1" * 24,
                }
            ],
            [
                {
                    "title": "A Precise Cited Retrieval Architecture",
                    "aminer_pub_id": "2" * 24,
                }
            ],
        )
        self.assertEqual(len(remaining), 1)
        self.assertEqual(matches, [])

    def test_selector_keeps_the_focused_role_diversity_instruction(self) -> None:
        captured: dict[str, str] = {}

        def model(system, user, *, max_tokens):
            captured.update(system=system, user=user, max_tokens=str(max_tokens))
            return {"selected": []}

        selected, diagnostics = select_relevant_records(
            [{
                "title": "A Useful Retrieval Candidate",
                "published": "2024-01-01",
                "summary": "A task-specific method and evaluation.",
                "query": "specific retrieval query",
                "retrieval_role": "method_mechanism",
                "relevance_score": "4",
            }],
            local_title="A Local Manuscript",
            queries=["specific retrieval query"],
            review_as_of="2026-01-01",
            llm_json=model,
        )

        self.assertEqual(selected, [])
        self.assertEqual(diagnostics["selection_status"], "completed")
        self.assertIn("retain at least one method precedent", captured["user"])
        self.assertIn("one direct baseline/task precedent", captured["user"])
        self.assertIn("one benchmark/evaluation or alternative-formulation record", captured["user"])

    def test_selector_preserves_confirmed_uncited_status_for_core(self) -> None:
        selected, diagnostics = select_relevant_records(
            [
                {
                    "title": "Policy-Guided Retrieval Ranking",
                    "published": "2024-01-01",
                    "summary": "A policy-guided retrieval ranking method.",
                    "query": "policy retrieval ranking",
                    "retrieval_role": "method_mechanism",
                    "relevance_score": "7",
                    "has_distinctive_title_overlap": "true",
                    "source": "AMiner Open Platform",
                    "url": "https://www.aminer.cn/pub/000000000000000000000021",
                    "missing_status": "uncited",
                    "facet_alignment": {
                        "profile_available": True,
                        "task_domain_fit": True,
                        "task_aligned": True,
                        "mechanism_aligned": True,
                        "data_protocol_aligned": False,
                        "publication_fit": True,
                        "mechanism_only_cross_domain": False,
                    },
                }
            ],
            local_title="A New Retrieval Ranking Manuscript",
            queries=["policy retrieval ranking"],
            review_as_of="2026-10-01",
            llm_json=lambda *_args, **_kwargs: {
                "selected": [
                    {
                        "candidate_id": "R1",
                        "relevance": "high",
                        "comparison_role": "method_precedent",
                        "reason": "The title and abstract directly match the ranking mechanism.",
                    }
                ]
            },
        )
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["tier"], "core")
        self.assertEqual(selected[0]["missing_status"], "uncited")
        self.assertEqual(diagnostics["core_record_count"], 1)

    def test_selector_builds_core_and_hard_gated_extended_view(self) -> None:
        roles = (
            "problem_setting",
            "method_mechanism",
            "named_prior_work",
            "direct_baseline",
            "benchmark_or_evaluation",
            "alternative_formulation",
        )
        candidates = [
            {
                "title": f"Verified Retrieval Architecture Candidate Number {index}",
                "published": "2024-03-01",
                "summary": "A task-specific retrieval architecture with comparative evaluation evidence.",
                "query": f"retrieval architecture query {index}",
                "retrieval_role": roles[(index - 1) % len(roles)],
                "relevance_score": "7",
                "has_distinctive_title_overlap": "true",
                "source": "AMiner Open Platform paper_search_pro",
                "url": f"https://www.aminer.cn/pub/{index:024x}",
                "aminer_pub_id": f"{index:024x}",
                "missing_status": "uncited",
                "facet_alignment": {
                    "profile_available": True,
                    "task_domain_fit": True,
                    "task_aligned": True,
                    "mechanism_aligned": True,
                    "data_protocol_aligned": False,
                    "publication_fit": True,
                    "mechanism_only_cross_domain": False,
                },
            }
            for index in range(1, 25)
        ]
        captured: dict[str, str] = {}

        def model(system, user, *, max_tokens):
            captured.update(system=system, user=user, max_tokens=str(max_tokens))
            return {
                "selected": [
                    {
                        "candidate_id": f"R{index}",
                        "relevance": "high",
                        "comparison_role": "method_precedent",
                        "reason": "The supplied abstract directly studies the same retrieval architecture.",
                    }
                    for index in range(1, 4)
                ]
            }

        selected, diagnostics = select_relevant_records(
            candidates,
            local_title="A Local Manuscript About Grounded Retrieval",
            queries=["grounded retrieval architecture"],
            review_as_of="2026-01-01",
            llm_json=model,
        )

        self.assertEqual(len(selected), 3)
        self.assertEqual([item["tier"] for item in selected], ["core"] * 3)
        self.assertTrue(all(item["confidence"] in {"high", "medium"} for item in selected))
        self.assertTrue(all(item["candidate_label"] == "candidate_to_check" for item in selected))
        self.assertTrue(all(item["missing_status"] == "uncited" for item in selected))
        self.assertTrue(all(item["selection_evidence"]["source"] for item in selected))
        self.assertEqual(diagnostics["core_record_count"], 3)
        self.assertEqual(diagnostics["extended_record_count"], 0)
        self.assertEqual(diagnostics["deterministic_fill_count"], 0)
        self.assertEqual(diagnostics["evaluation_view_status"], "insufficient_candidates")
        self.assertEqual(diagnostics["eligible_medium_not_returned_count"], 21)
        self.assertIn("Rank the supplied shortlist", captured["user"])
        self.assertIn('"candidate_id": "R24"', captured["user"])

    def test_selector_does_not_pad_with_low_confidence_future_or_self_records(self) -> None:
        candidates = [
            {
                "title": "Directly Relevant Grounded Retrieval Precedent",
                "published": "2024-03-01",
                "summary": "A direct method precedent.",
                "query": "grounded retrieval",
                "retrieval_role": "method_mechanism",
                "relevance_score": "7",
                "has_distinctive_title_overlap": "true",
                "source": "AMiner Open Platform paper_search_pro",
                "url": "https://www.aminer.cn/pub/000000000000000000000001",
            },
            {
                "title": "Broad Artificial Intelligence Overview",
                "published": "2024-03-01",
                "summary": "A broad unrelated overview.",
                "query": "grounded retrieval",
                "retrieval_role": "background",
                "relevance_score": "0",
                "has_distinctive_title_overlap": "false",
                "source": "AMiner Open Platform paper_search_pro",
                "url": "https://www.aminer.cn/pub/000000000000000000000002",
            },
            {
                "title": "A Local Manuscript About Grounded Retrieval",
                "published": "2024-03-01",
                "summary": "The manuscript itself.",
                "query": "grounded retrieval",
                "retrieval_role": "method_mechanism",
                "relevance_score": "9",
                "has_distinctive_title_overlap": "true",
                "source": "AMiner Open Platform paper_search_pro",
                "url": "https://www.aminer.cn/pub/000000000000000000000003",
            },
            {
                "title": "Future Grounded Retrieval Architecture",
                "published": "2027-03-01",
                "summary": "A future method.",
                "query": "grounded retrieval",
                "retrieval_role": "method_mechanism",
                "relevance_score": "9",
                "has_distinctive_title_overlap": "true",
                "source": "AMiner Open Platform paper_search_pro",
                "url": "https://www.aminer.cn/pub/000000000000000000000004",
            },
        ]

        selected, diagnostics = select_relevant_records(
            candidates,
            local_title="A Local Manuscript About Grounded Retrieval",
            queries=["grounded retrieval"],
            review_as_of="2026-01-01",
            llm_json=lambda *_args, **_kwargs: {
                "selected": [
                    {
                        "candidate_id": "R1",
                        "relevance": "high",
                        "comparison_role": "method_precedent",
                        "reason": "The title and abstract directly match the manuscript method.",
                    }
                ]
            },
        )

        self.assertEqual([item["title"] for item in selected], [candidates[0]["title"]])
        self.assertEqual(diagnostics["evaluation_view_status"], "insufficient_candidates")
        self.assertEqual(diagnostics["deterministic_fill_count"], 0)
        self.assertEqual(diagnostics["low_confidence_excluded_count"], 0)
        self.assertEqual(diagnostics["unselected_metadata_failed_count"], 3)
        self.assertEqual(diagnostics["decision_counts"]["not_selected_by_model"], 3)
        self.assertLess(len(selected), diagnostics["evaluation_view_limit"])

    def test_selector_hard_gates_mechanism_only_cross_domain_result(self) -> None:
        query = RoutedQuery(
            "sparse key value memory",
            query_shape="keyword_phrase",
            fallback_phrase="sparse key value memory",
            role="method_mechanism",
            pro_field="keyword",
            facet="mechanism",
            retrieval_profile={
                "task_terms": ["sequential recommendation"],
                "mechanism_terms": ["sparse key value memory"],
                "data_protocol_terms": ["long user history"],
            },
        )
        candidates, _diagnostics = filter_retrieval_records(
            [
                {
                    "title": "Sparse Key Value Memory for Medical Segmentation",
                    "published": "2025-01-01",
                    "summary": "A sparse memory for medical video segmentation.",
                    "query": str(query),
                    "query_role": "method_mechanism",
                    "query_shape": "keyword_phrase",
                    "source": "AMiner Open Platform paper_search_pro",
                    "url": "https://www.aminer.cn/pub/000000000000000000000001",
                }
            ],
            local_title="Long History Sequential Recommendation",
            review_as_of="2026-10-01",
            manuscript_profile=query.retrieval_profile,
        )
        selected, diagnostics = select_relevant_records(
            candidates,
            local_title="Long History Sequential Recommendation",
            queries=[query],
            review_as_of="2026-10-01",
            llm_json=lambda *_args, **_kwargs: {
                "selected": [
                    {
                        "candidate_id": "R1",
                        "relevance": "high",
                        "comparison_role": "method_precedent",
                        "reason": "The abstract contains sparse key value memory.",
                    }
                ]
            },
        )
        self.assertEqual(selected, [])
        self.assertGreaterEqual(diagnostics["low_confidence_excluded_count"], 1)

    def test_selector_rejects_generic_task_overlap_from_another_domain(self) -> None:
        query = RoutedQuery(
            "long sequence recommendation key value memory",
            query_shape="keyword_phrase",
            fallback_phrase="sequence recommendation memory",
            role="method_mechanism",
            pro_field="keyword",
            retrieval_profile={
                "domain_terms": ["domain alpha interactive systems"],
                "task_terms": ["long sequence recommendation"],
                "mechanism_terms": ["key value memory"],
                "data_protocol_terms": [],
            },
        )
        candidates, _diagnostics = filter_retrieval_records(
            [
                {
                    "title": "Long Sequence Recommendation with Key Value Memory for Domain Beta",
                    "published": "2025-01-01",
                    "summary": (
                        "A long sequence recommendation task in domain beta using "
                        "a key value memory mechanism."
                    ),
                    "query": str(query),
                    "query_role": "method_mechanism",
                    "query_shape": "keyword_phrase",
                    "source": "AMiner Open Platform paper_search_pro",
                    "url": "https://www.aminer.cn/pub/000000000000000000000041",
                }
            ],
            local_title="A Manuscript for Domain Alpha Personalization",
            review_as_of="2026-10-01",
            manuscript_profile=query.retrieval_profile,
        )
        selected, diagnostics = select_relevant_records(
            candidates,
            local_title="A Manuscript for Domain Alpha Personalization",
            queries=[query],
            review_as_of="2026-10-01",
            llm_json=lambda *_args, **_kwargs: {
                "selected": [
                    {
                        "candidate_id": "R1",
                        "relevance": "high",
                        "comparison_role": "method_precedent",
                        "reason": "The candidate shares the task words and memory mechanism.",
                    }
                ]
            },
        )
        self.assertEqual(selected, [])
        checks = diagnostics["low_confidence_excluded"][0]["verification"]["checks"]
        self.assertIn("task_domain_fit", checks)
        self.assertFalse(checks["task_domain_fit"])

    def test_selector_excludes_same_year_candidate_with_unresolved_date(self) -> None:
        candidate = {
            "title": "Potentially Later Same-Year Retrieval Method",
            "published": "2026",
            "date_precision": "year",
            "cutoff_status": "uncertain_same_year",
            "time_admissibility": "uncertain",
            "summary": "A directly relevant retrieval method.",
            "query": "relevant retrieval method",
            "retrieval_role": "method_mechanism",
            "relevance_score": "8",
            "has_distinctive_title_overlap": "true",
            "source": "AMiner Open Platform paper_search_pro",
            "url": "https://www.aminer.cn/pub/000000000000000000000042",
        }
        selected, diagnostics = select_relevant_records(
            [candidate],
            local_title="A Relevant Retrieval Manuscript",
            queries=["relevant retrieval method"],
            review_as_of="2026-10-01",
            llm_json=lambda *_args, **_kwargs: {
                "selected": [
                    {
                        "candidate_id": "R1",
                        "relevance": "high",
                        "comparison_role": "method_precedent",
                        "reason": "The supplied evidence is directly relevant.",
                    }
                ]
            },
        )
        self.assertEqual(selected, [])
        checks = diagnostics["low_confidence_excluded"][0]["verification"]["checks"]
        self.assertFalse(checks["cutoff_date_resolved"])


class ParserAndPipelineTests(unittest.TestCase):
    def test_logical_markdown_location_metadata_reports_chunk_count(self) -> None:
        metadata = _structured_location_metadata(
            ["chunk one", "chunk two"],
            [
                {
                    "section": "Introduction",
                    "location_unit": "markdown_block",
                    "location_numbering": "section_local",
                }
            ],
        )
        self.assertIsNone(metadata["physical_page_count"])
        self.assertEqual(metadata["logical_text_chunks"], 2)

    def test_grobid_failure_is_fatal_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "paper.pdf"
            pdf.write_bytes(b"not-used-by-injected-parser")

            def unavailable(_path):
                raise httpx.ConnectError("GROBID unavailable")

            with self.assertRaisesRegex(ValueError, "--allow-pypdf-fallback"):
                prepare_uploaded_pdf_source(
                    pdf,
                    Path(directory) / "run",
                    progress=None,
                    request_fulltext=unavailable,
                    parse_local=lambda _raw: MarkdownParseResult(
                        markdown="local text", pages=["local text"]
                    ),
                )

    def test_explicit_pypdf_fallback_is_visible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "paper.pdf"
            pdf.write_bytes(b"not-used-by-injected-parser")
            events: list[tuple[str, str, dict[str, object] | None]] = []

            def unavailable(_path):
                raise httpx.ConnectError("GROBID unavailable")

            result = prepare_uploaded_pdf_source(
                pdf,
                Path(directory) / "run",
                progress=lambda phase, message, metrics, _artifacts: events.append(
                    (phase, message, metrics)
                ),
                request_fulltext=unavailable,
                allow_pypdf_fallback=True,
                parse_local=lambda _raw: MarkdownParseResult(
                    markdown="local text", pages=["local text"]
                ),
            )
            self.assertTrue(result["fallback_used"])
            self.assertEqual(result["provider"], "local_pypdf")
            self.assertEqual(result["diagnostics"][-1]["status"], "fallback")
            self.assertTrue(
                any(
                    "explicitly allowed lower-quality pypdf fallback" in message
                    and metrics == {"parser": "local_pypdf", "fallback_used": True}
                    for _phase, message, metrics in events
                )
            )

    def test_reference_builder_reuses_precomputed_tei_without_second_request(self) -> None:
        calls = 0

        def request(_path):
            nonlocal calls
            calls += 1
            return "unused"

        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "paper.pdf"
            pdf.write_bytes(b"placeholder")
            ledger = build_reference_ledger_for_pdf(
                pdf,
                grobid_tei="",
                local_pages=["No bibliography here"],
                request_fulltext=request,
            )
        self.assertEqual(calls, 0)
        self.assertEqual(ledger["fallback_from"], "grobid_tei")

    def test_pipeline_returns_manifest_and_writes_private_full_report(self) -> None:
        long_page = " ".join(f"Scientific sentence number {index} explains the retrieval method." for index in range(180))
        parsed = {
            "pages": [long_page],
            "markdown": long_page,
            "source_locations": [],
            "provider": "local_pypdf",
            "local_title": "Local Retrieval Manuscript",
            "grobid_tei": "",
            "grobid_tei_file": None,
            "fallback_used": True,
            "diagnostics": [{"parser": "local_grobid_tei_hybrid", "status": "fallback"}],
            "physical_pages": [long_page],
        }
        ledger = {
            "status": "ready_for_resolution",
            "records": [{
                "id": "R1",
                "candidate_title": "Useful Related Retrieval Method",
                "doi": None,
                "arxiv_id": "2401.00001",
                "anchor": {"page_start": 1, "page_end": 1, "quote": "reference"},
                "resolution_eligible": True,
            }],
        }
        candidate = {
            "title": "Useful Related Retrieval Method",
            "published": "2024-01-01",
            "url": "https://arxiv.org/abs/2401.00001",
            "source": "arXiv API",
        }
        profile = ModelProfile("deepseek", "DeepSeek", "mock-model", "key", "https://example.invalid")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work_root = root / "work"
            work_root.mkdir(mode=0o755)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"pdf-placeholder")
            output = root / "report-copy.json"
            with (
                patch("related_work_audit.pipeline.prepare_uploaded_pdf_source", return_value=parsed),
                patch("related_work_audit.pipeline.build_related_work_queries", return_value=["specific query"]),
                patch(
                    "related_work_audit.pipeline.retrieve_related_work",
                    return_value=([candidate], {"status": "passed"}),
                ) as retriever,
                patch("related_work_audit.pipeline.build_reference_ledger_for_pdf", return_value=ledger),
            ):
                manifest = run_audit(
                    pdf=pdf,
                    review_as_of="2026-01-01",
                    profile=profile,
                    work_root=work_root,
                    output=output,
                    force=False,
                    search_provider="arxiv",
                )
            self.assertEqual(manifest["mode"], "standalone-related-work")
            report_path = Path(manifest["report"])
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["capability"], "related-work")
            self.assertNotIn("artifacts", report)
            self.assertIsNone(report["source"]["pub_id"])
            self.assertEqual(
                report["run_metadata"]["source_parse_file"], "source_parse.json"
            )
            self.assertNotIn("relation_status", report["run_metadata"])
            self.assertNotIn("relation_graph", manifest)
            self.assertFalse((report_path.parent / "relation_graph.json").exists())
            self.assertNotIn("seed_records", retriever.call_args.kwargs)
            self.assertNotIn(
                "relation_graph", report["results"]["related_work_coverage"]
            )
            source_parse = report_path.parent / "source_parse.json"
            self.assertTrue(source_parse.is_file())
            self.assertEqual(stat.S_IMODE(source_parse.stat().st_mode), 0o600)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), report)
            self.assertEqual(stat.S_IMODE(work_root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(report_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

    def test_pub_id_pipeline_records_aminer_provenance_and_uses_private_ledger(self) -> None:
        pub_id = "0123456789abcdef01234567"
        long_page = " ".join(
            f"Scientific sentence number {index} explains structured retrieval evidence."
            for index in range(180)
        )
        parsed = {
            "pages": [long_page],
            "markdown": long_page,
            "source_locations": [
                {
                    "physical_page": 1,
                    "page_index": 0,
                    "section": "Document",
                    "location_unit": "aminer_content_block",
                    "location_numbering": "physical_page_and_section_block",
                    "blocks": [{"type": "text", "markdown": long_page}],
                }
            ],
            "provider": "aminer_interface_b_content_list",
            "fallback_used": False,
            "diagnostics": [
                {
                    "parser": "aminer_interface_b_content_list",
                    "status": "completed",
                }
            ],
            "manifest": {
                "input_source": "aminer_interface_b",
                "aminer_pub_id": pub_id,
                "retrieval_interface": "AMiner interface B extracted files",
                "source_provider": "aminer_interface_b_content_list",
                "content_list_file": "source_content_list.json",
                "physical_page_count": 1,
                "logical_text_chunks": None,
                "content_block_count": 1,
                "content_block_type_counts": {"text": 1},
            },
        }
        ledger = {
            "status": "ready_for_resolution",
            "records": [
                {
                    "id": "R1",
                    "candidate_title": "Useful Related Retrieval Method",
                    "doi": None,
                    "arxiv_id": "2401.00001",
                    "anchor": {
                        "page_start": 1,
                        "page_end": 1,
                        "quote": "reference",
                    },
                    "resolution_eligible": True,
                },
                {
                    "id": "R2",
                    "candidate_title": "Untrusted Embedded Bibliography Record",
                    "doi": None,
                    "arxiv_id": None,
                    "anchor": None,
                    "resolution_eligible": False,
                }
            ],
        }
        candidate = {
            "title": "Useful Related Retrieval Method",
            "published": "2024-01-01",
            "url": "https://arxiv.org/abs/2401.00001",
            "source": "arXiv API",
        }
        relation_reference = {
            "title": "Verified Cited Relation-Control Work",
            "published": "2023-01-01",
            "summary": "A known cited work from the public relation graph.",
            "aminer_pub_id": "1123456789abcdef01234567",
            "relation_role": "target_references",
            "relation_evidence": {"relation": "target_cites_candidate"},
            "source": "AMiner Open Platform paper/relation + paper_info",
            "url": "https://www.aminer.cn/pub/1123456789abcdef01234567",
        }
        profile = ModelProfile(
            "deepseek", "DeepSeek", "mock-model", "key", "https://example.invalid"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch(
                    "related_work_audit.pipeline.download_aminer_source",
                    return_value=parsed,
                ),
                patch(
                    "related_work_audit.pipeline.aminer_api_key",
                    return_value="private-test-key",
                ),
                patch(
                    "related_work_audit.pipeline.build_related_work_queries",
                    return_value=["specific query"],
                ),
                patch(
                    "related_work_audit.pipeline.fetch_aminer_relation_context",
                    return_value={
                        "schema_version": "0.1",
                        "provider": "AMiner Open Platform paper/relation + paper_info",
                        "target_pub_id": pub_id,
                        "review_as_of": "2026-01-01",
                        "status": "completed",
                        "non_fatal": True,
                        "queries": [],
                        "metadata_status": "not_needed",
                        "summary": {
                            "target_reference_ids": 1,
                            "citing_target_ids": 0,
                            "metadata_records": 0,
                            "candidate_records": 0,
                            "same_year_cutoff_uncertain": 0,
                        },
                        "target_references": [relation_reference],
                        "citing_target_records": [],
                        "candidate_records": [],
                        "errors": [],
                        "limitations": [],
                    },
                ) as relation_fetch,
                patch(
                    "related_work_audit.pipeline.retrieve_related_work",
                    return_value=([candidate], {"status": "passed"}),
                ) as retriever,
                patch(
                    "related_work_audit.pipeline.build_reference_ledger",
                    return_value=ledger,
                ) as ledger_builder,
            ):
                manifest = run_audit(
                    pub_id=pub_id.upper(),
                    review_as_of="2026-01-01",
                    profile=profile,
                    work_root=root / "work",
                    output=None,
                    force=False,
                )
            ledger_builder.assert_called_once_with(
                [long_page], parser="aminer_interface_b_markdown"
            )
            self.assertTrue(retriever.call_args.kwargs["bibliography_ready"])
            self.assertEqual(
                retriever.call_args.kwargs["local_references"],
                [ledger["records"][0]],
            )
            self.assertEqual(
                retriever.call_args.kwargs["known_cited_controls"],
                [relation_reference],
            )
            self.assertNotIn("seed_records", retriever.call_args.kwargs)
            report = json.loads(
                Path(manifest["report"]).read_text(encoding="utf-8")
            )
            self.assertEqual(report["source"]["input_type"], "aminer_pub_id")
            self.assertEqual(report["source"]["pub_id"], pub_id)
            self.assertEqual(report["run_metadata"]["relation_pub_id"], pub_id)
            self.assertEqual(report["run_metadata"]["relation_status"], "completed")
            self.assertEqual(
                report["results"]["related_work_coverage"]["relation_graph"]["status"],
                "completed",
            )
            relation_fetch.assert_called_once()
            self.assertTrue(
                report["source"]["provenance"]["structured_content_list_used"]
            )
            self.assertEqual(
                report["run_metadata"]["source_provider"],
                "aminer_interface_b_content_list",
            )
            self.assertNotIn("private-test-key", json.dumps(report))

    def test_source_parse_is_retained_before_later_model_failure(self) -> None:
        long_page = " ".join(
            f"Scientific sentence number {index} explains the retrieval method."
            for index in range(180)
        )
        parsed = {
            "pages": [long_page],
            "markdown": long_page,
            "source_locations": [],
            "provider": "local_grobid_tei_hybrid",
            "local_title": "Local Retrieval Manuscript",
            "grobid_tei": "<TEI/>",
            "grobid_tei_file": "grobid_fulltext.tei.xml",
            "fallback_used": False,
            "diagnostics": [
                {"parser": "local_grobid_tei_hybrid", "status": "completed"}
            ],
            "physical_pages": [long_page],
        }
        profile = ModelProfile(
            "deepseek", "DeepSeek", "mock-model", "key", "https://example.invalid"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"pdf-placeholder")
            work_root = root / "work"
            with (
                patch(
                    "related_work_audit.pipeline.prepare_uploaded_pdf_source",
                    return_value=parsed,
                ),
                patch(
                    "related_work_audit.pipeline.build_related_work_queries",
                    side_effect=RuntimeError("model failed after parsing"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "model failed"):
                    run_audit(
                        pdf=pdf,
                        review_as_of="2026-01-01",
                        profile=profile,
                        work_root=work_root,
                        output=None,
                        force=False,
                        search_provider="arxiv",
                    )
            run_dirs = list((work_root / "runs").iterdir())
            self.assertEqual(len(run_dirs), 1)
            source_parse_path = run_dirs[0] / "source_parse.json"
            self.assertTrue(source_parse_path.is_file())
            source_parse = json.loads(source_parse_path.read_text(encoding="utf-8"))
            self.assertEqual(source_parse["provider"], "local_grobid_tei_hybrid")
            self.assertFalse(source_parse["fallback_used"])
            self.assertEqual(stat.S_IMODE(source_parse_path.stat().st_mode), 0o600)

    def test_pub_id_rejects_pdf_only_fallback_flag_before_network_access(self) -> None:
        profile = ModelProfile(
            "deepseek", "DeepSeek", "mock-model", "key", "https://example.invalid"
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "only valid with --pdf"):
                run_audit(
                    pub_id="a" * 24,
                    review_as_of="2026-01-01",
                    profile=profile,
                    work_root=Path(directory),
                    output=None,
                    force=False,
                    allow_pypdf_fallback=True,
                )

    def test_pub_id_rejects_a_different_relation_pub_id(self) -> None:
        profile = ModelProfile(
            "deepseek", "DeepSeek", "mock-model", "key", "https://example.invalid"
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "must reuse the same ID"):
                run_audit(
                    pub_id="a" * 24,
                    relation_pub_id="b" * 24,
                    review_as_of="2026-01-01",
                    profile=profile,
                    work_root=Path(directory),
                    output=None,
                    force=False,
                )

    def test_empty_or_failed_retrieval_is_not_assessed_and_partial(self) -> None:
        long_page = " ".join(
            f"Scientific sentence number {index} explains the retrieval method."
            for index in range(180)
        )
        parsed = {
            "pages": [long_page],
            "markdown": long_page,
            "source_locations": [],
            "provider": "local_pypdf",
            "local_title": "Local Retrieval Manuscript",
            "grobid_tei": "",
            "grobid_tei_file": None,
            "fallback_used": True,
            "diagnostics": [],
            "physical_pages": [long_page],
        }
        ledger = {
            "status": "ready_for_resolution",
            "records": [{
                "id": "R1",
                "candidate_title": "Useful Related Retrieval Method",
                "resolution_eligible": True,
            }],
        }
        profile = ModelProfile(
            "deepseek", "DeepSeek", "mock-model", "key", "https://example.invalid"
        )
        scenarios = {
            "empty": {
                "status": "no_semantically_relevant_records",
                "provider_errors": [],
            },
            "provider_failure": {
                "status": "no_semantically_relevant_records",
                "provider_errors": ["arXiv: ConnectError", "OpenAlex: ConnectError"],
            },
            "partial_with_valid_candidate": {
                "status": "partial",
                "provider_errors": [],
                "candidate_pool_round_statuses": ["completed", "partial_invalid_ids"],
            },
            "provider_error_with_valid_candidate": {
                "status": "partial",
                "provider_errors": ["AMiner: ConnectError"],
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"pdf-placeholder")
            for label, diagnostics in scenarios.items():
                candidates = [{
                    "title": "Another Useful Retrieval Method",
                    "published": "2025",
                    "source": "AMiner",
                    "url": "https://example.invalid/paper",
                }] if label.endswith("with_valid_candidate") else []
                with self.subTest(label=label):
                    with (
                        patch(
                            "related_work_audit.pipeline.prepare_uploaded_pdf_source",
                            return_value=parsed,
                        ),
                        patch(
                            "related_work_audit.pipeline.build_related_work_queries",
                            return_value=["specific query"],
                        ),
                        patch(
                            "related_work_audit.pipeline.retrieve_related_work",
                            return_value=(candidates, diagnostics),
                        ),
                        patch(
                            "related_work_audit.pipeline.build_reference_ledger_for_pdf",
                            return_value=ledger,
                        ),
                    ):
                        manifest = run_audit(
                            pdf=pdf,
                            review_as_of="2026-01-01",
                            profile=profile,
                            work_root=root / f"work-{label}",
                            output=None,
                            force=False,
                            search_provider="arxiv",
                        )
                report = json.loads(Path(manifest["report"]).read_text(encoding="utf-8"))
                audit = report["results"]["related_work_coverage"]
                self.assertEqual(manifest["status"], "partial")
                self.assertEqual(report["status"], "partial")
                self.assertEqual(audit["status"], "partial" if candidates else "not_assessed")
                self.assertEqual(audit["retrieval_diagnostics"], diagnostics)
                self.assertEqual(len(audit["records"]), len(candidates))
                if candidates:
                    self.assertEqual(audit["records"][0]["status"], "candidate_to_check")
                    self.assertTrue(any("were retained" in item for item in audit["limitations"]))
                    self.assertFalse(any("no semantically relevant candidates" in item for item in audit["limitations"]))
                else:
                    self.assertTrue(any("not assessed" in item for item in audit["limitations"]))


if __name__ == "__main__":
    unittest.main()
