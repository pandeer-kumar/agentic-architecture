"""LLM gateway (ARCHITECTURE §12): the only path to the Claude Messages API.

- role -> model / effort / max_tokens from contracts/policies/model_routing.yaml
- streaming for every call (long outputs stay within HTTP timeouts)
- structured outputs (output_config.format) + client-side JSON-Schema validation + one repair retry
- server-side refusal fallback (beta) when enabled; the served model is recorded
- every call is logged to llm_calls with usage and the payload snapshot id
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import anthropic
from jsonschema import Draft202012Validator

from . import db
from .config import get_settings

FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass
class LLMResult:
    content: list[dict]          # response content blocks as plain dicts (thinking blocks kept unchanged)
    stop_reason: str
    model: str
    usage: dict
    request_id: str | None

    @property
    def text(self) -> str:
        return "".join(b.get("text", "") for b in self.content if b.get("type") == "text")

    @property
    def tool_uses(self) -> list[dict]:
        return [b for b in self.content if b.get("type") == "tool_use"]


class StructuredOutputError(RuntimeError):
    pass


class RefusalError(RuntimeError):
    pass


class LLMGateway:
    def __init__(self, client: anthropic.Anthropic | None = None):
        s = get_settings()
        kwargs: dict[str, Any] = {"max_retries": 4}
        if s.anthropic_base_url:
            kwargs["base_url"] = s.anthropic_base_url
        if s.anthropic_api_key:
            kwargs["api_key"] = s.anthropic_api_key
        self.client = client or anthropic.Anthropic(**kwargs)
        self.routing = s.model_routing
        self.fallback = s.server_side_fallback

    def role_params(self, role: str) -> dict:
        r = self.routing["roles"][role]
        return {"model": r["model"], "max_tokens": r["max_tokens"],
                "thinking": {"type": "adaptive"}, "output_config": {"effort": r["effort"]}}

    def call(self, role: str, *, system: list[dict], messages: list[dict], tools: list[dict] | None = None,
             output_schema: dict | None = None, job_id: str | None = None, snapshot_id: str | None = None,
             step_id: str | None = None) -> LLMResult:
        params = self.role_params(role)
        params.update(system=system, messages=messages, cache_control={"type": "ephemeral"})
        if tools:
            params["tools"] = tools
        if output_schema is not None:
            params["output_config"] = {**params["output_config"],
                                       "format": {"type": "json_schema", "schema": output_schema}}
        extra: dict[str, Any] = {}
        if self.fallback:
            extra = {"betas": [FALLBACK_BETA], "fallbacks": "default"}
        t0 = time.monotonic()
        with self.client.beta.messages.stream(**params, **extra) as stream:
            msg = stream.get_final_message()
        latency = int((time.monotonic() - t0) * 1000)
        content = [b.model_dump(mode="json", exclude_none=True) for b in msg.content]
        usage = msg.usage.model_dump(mode="json", exclude_none=True) if msg.usage else {}
        result = LLMResult(content=content, stop_reason=msg.stop_reason or "", model=msg.model,
                           usage=usage, request_id=getattr(msg, "_request_id", None))
        db.q("""INSERT INTO llm_calls (job_id, step_id, agent_role, model, api_request_id, input_tokens, output_tokens,
                                      cache_read_tokens, cache_write_tokens, stop_reason, latency_ms, snapshot_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
             (job_id, step_id, _db_role(role), result.model, result.request_id, usage.get("input_tokens", 0),
              usage.get("output_tokens", 0), usage.get("cache_read_input_tokens", 0) or 0,
              usage.get("cache_creation_input_tokens", 0) or 0, result.stop_reason, latency, snapshot_id))
        if result.stop_reason == "refusal":
            raise RefusalError(f"{role} request refused by all models in the fallback chain")
        return result

    def call_json(self, role: str, *, system: list[dict], user_content: list[dict] | str, schema: dict,
                  job_id: str | None = None, snapshot_id: str | None = None) -> Any:
        """Structured output with client-side validation and one repair retry (docs/02 §9)."""
        messages = [{"role": "user", "content": user_content}]
        validator = Draft202012Validator(schema)
        last_error = ""
        for attempt in range(2):
            res = self.call(role, system=system, messages=messages, output_schema=schema, job_id=job_id,
                            snapshot_id=snapshot_id)
            if res.stop_reason == "max_tokens":
                raise StructuredOutputError("max_tokens")
            try:
                data = json.loads(res.text)
                errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
                if not errors:
                    return data
                last_error = "; ".join(e.message for e in errors[:5])
            except json.JSONDecodeError as e:
                last_error = f"invalid JSON: {e}"
            messages = messages + [{"role": "assistant", "content": res.content},
                                   {"role": "user", "content": f"Your output did not validate: {last_error}. Return corrected JSON only."}]
        raise StructuredOutputError(last_error)


_DB_ROLES = {"orchestrator", "map_worker", "verifier", "explorer", "adjudicator", "judge", "summariser"}


def _db_role(role: str) -> str:
    return role if role in _DB_ROLES else {"second_opinion": "map_worker", "compressor": "summariser"}.get(role, "judge")


_gateway: LLMGateway | None = None


def gateway() -> LLMGateway:
    global _gateway
    if _gateway is None:
        _gateway = LLMGateway()
    return _gateway


def reset_gateway() -> None:
    global _gateway
    _gateway = None
