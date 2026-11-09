"""Structured-output model calls used by query planning and candidate selection."""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

import httpx
from openai import OpenAI

from .config import ModelProfile, openai_max_retries, openai_timeout_seconds, proxy_url


def close_json_containers(text: str) -> str:
    closing = {"{": "}", "[": "]"}
    result: list[str] = []
    stack: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            result.append(char)
        elif char in closing:
            stack.append(char)
            result.append(char)
        elif char in "}]":
            while stack and closing[stack[-1]] != char:
                result.append(closing[stack.pop()])
            if stack:
                stack.pop()
            result.append(char)
        else:
            result.append(char)
    if not in_string:
        while stack:
            result.append(closing[stack.pop()])
    return "".join(result)


def json_from_text(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.S)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    candidates = [cleaned]
    if start >= 0:
        candidates.append(cleaned[start : end + 1] if end > start else cleaned[start:])
    last_error: json.JSONDecodeError | None = None
    for candidate in dict.fromkeys(candidates):
        for value in (candidate, close_json_containers(candidate)):
            try:
                payload = json.loads(value)
                if isinstance(payload, dict):
                    return payload
            except json.JSONDecodeError as exc:
                last_error = exc
    if start < 0:
        raise ValueError("The model did not return a JSON object.")
    raise last_error or ValueError("The model did not return a JSON object.")


def llm_json(
    profile: ModelProfile,
    system: str,
    user: str,
    *,
    max_tokens: int,
    audit: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    last_error: ValueError | None = None
    for attempt in range(2):
        retry_instruction = ""
        if attempt:
            retry_instruction = (
                "\n\nYour previous response was not valid JSON. Return one complete JSON object only, "
                "with no Markdown fence, commentary, or trailing text. Keep every string concise. "
                "Do not omit required keys or close the response early."
            )
        request_tokens = min(max_tokens * 2, 12000) if profile.name in {"gpt", "glm53", "glm53flash"} else max_tokens
        request: dict[str, Any] = {
            "model": profile.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user + retry_instruction},
            ],
        }
        if profile.name != "gpt":
            request["temperature"] = 0.15
        if profile.name == "gpt":
            request["max_completion_tokens"] = request_tokens
            request["response_format"] = {"type": "json_object"}
        else:
            request["max_tokens"] = request_tokens
            if profile.name in {"glm", "glm53", "glm53flash"}:
                request["response_format"] = {"type": "json_object"}
        if profile.name == "glm":
            request["extra_body"] = {"thinking": {"type": "disabled"}}
        elif profile.name == "glm53flash":
            request["extra_body"] = {
                "thinking": {"type": "enabled"},
                "reasoning_effort": profile.thinking_effort or "low",
            }
        elif profile.name == "glm53" and profile.thinking_effort:
            request["reasoning_effort"] = profile.thinking_effort
        proxy = proxy_url()
        http_client = httpx.Client(proxy=proxy) if proxy else None
        started = time.monotonic()
        try:
            with OpenAI(
                api_key=profile.api_key,
                base_url=profile.base_url,
                timeout=openai_timeout_seconds(),
                max_retries=openai_max_retries(),
                http_client=http_client,
            ) as client:
                response = client.chat.completions.create(**request)
                response_text = response.choices[0].message.content or ""
                finish_reason = getattr(response.choices[0], "finish_reason", None)
                usage = getattr(response, "usage", None)
                if audit:
                    audit({"event": "model_response", "attempt": attempt + 1,
                           "request": request, "response_text": response_text,
                           "finish_reason": finish_reason,
                           "usage": usage.model_dump() if hasattr(usage, "model_dump") else usage,
                           "elapsed_seconds": round(time.monotonic() - started, 3)})
        except Exception as exc:
            if audit:
                audit({"event": "model_error", "attempt": attempt + 1,
                       "request": request, "error_type": type(exc).__name__})
            raise
        finally:
            if http_client is not None:
                http_client.close()
        try:
            if finish_reason == "length":
                raise ValueError("Model output reached its token limit; partial rankings are not accepted.")
            return json_from_text(response_text)
        except ValueError as exc:
            if audit:
                audit({"event": "model_output_rejected", "attempt": attempt + 1,
                       "reason": "token_limit" if finish_reason == "length" else "invalid_json"})
            last_error = ValueError(
                f"Invalid structured model output (model={profile.model}, chars={len(response_text)}): {exc}"
            )
    raise last_error or ValueError("The model did not return valid JSON.")
