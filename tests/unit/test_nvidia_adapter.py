from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import openai
import pytest

from resilinet.adapters.nvidia import NvidiaIntelligenceProvider
from resilinet.ports import ExtractionError, ProviderUnavailableError, RawMessage
from resilinet.schemas import CriticContext, CriticVerdict, ParsedEmail

INCIDENT_ID = UUID("00000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 11, 22, 9, 0, tzinfo=UTC)

NOW_TEXT = RawMessage(
    provider_message_id="msg-1",
    mailbox_id="mbx-1",
    sender="volunteer@example.org",
    subject="Shelter 4 water",
    text="Shelter 4 has 40 people and zero potable water near Gandhi Park.",
    received_at=NOW,
)

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "openai"
    / "extraction_need.contract.json"
)


def parsed_payload() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["payload"]


@dataclass
class _Function:
    arguments: str | None


@dataclass
class _ToolCall:
    function: _Function


@dataclass
class _Message:
    content: str | None = None
    tool_calls: list[_ToolCall] | None = None


@dataclass
class _Choice:
    message: _Message


@dataclass
class _Response:
    choices: list[_Choice]


def tool_response(payload: Any) -> _Response:
    return _Response(
        [
            _Choice(
                _Message(
                    tool_calls=[_ToolCall(_Function(json.dumps(payload)))],
                )
            )
        ]
    )


class StubCreate:
    def __init__(self, outcomes: list[Any]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def provider(
    create: StubCreate, *, fallback_models: tuple[str, ...] = ()
) -> NvidiaIntelligenceProvider:
    return NvidiaIntelligenceProvider(
        api_key="test-key",
        model="meta/llama-3.3-70b-instruct",
        fallback_models=fallback_models,
        create=create,
    )


def retired_model_error(model: str) -> openai.APIStatusError:
    request = httpx.Request(
        "POST", "https://integrate.api.nvidia.com/v1/chat/completions"
    )
    response = httpx.Response(
        410,
        request=request,
        json={"error": {"message": f"{model} is retired"}},
    )
    return openai.APIStatusError(
        f"410 Gone: {model}",
        response=response,
        body={"error": {"message": f"{model} is retired"}},
    )


async def test_extract_calls_one_function_tool_and_validates_arguments() -> None:
    payload = parsed_payload()
    create = StubCreate([tool_response(payload)])

    result = await provider(create).extract(NOW_TEXT)

    assert isinstance(result, ParsedEmail)
    assert result.context.entity_name == "Shelter 4"
    call = create.calls[0]
    assert call["model"] == "meta/llama-3.3-70b-instruct"
    assert call["temperature"] == 0.0
    # NIM's hosted schema documents tools but not tool_choice.
    assert "tool_choice" not in call
    assert call["tools"][0]["function"]["name"] == "emit_result"
    assert call["tools"][0]["function"]["parameters"]["type"] == "object"
    assert "emit_result" in call["messages"][0]["content"]
    assert "<email>" in call["messages"][1]["content"]
    assert NOW_TEXT.text in call["messages"][1]["content"]


async def test_json_content_is_accepted_when_no_tool_call_is_returned() -> None:
    payload = parsed_payload()
    create = StubCreate(
        [_Response([_Choice(_Message(content=json.dumps(payload)))])]
    )

    result = await provider(create).extract(NOW_TEXT)

    assert result.context.entity_name == "Shelter 4"


async def test_missing_tool_call_is_repaired_once() -> None:
    payload = parsed_payload()
    create = StubCreate(
        [
            _Response([_Choice(_Message())]),
            tool_response(payload),
        ]
    )

    result = await provider(create).extract(NOW_TEXT)

    assert result.context.entity_name == "Shelter 4"
    assert len(create.calls) == 2
    assert "validation_feedback" in create.calls[1]["messages"][1]["content"]


async def test_malformed_arguments_fail_after_one_repair() -> None:
    malformed = _Response(
        [_Choice(_Message(tool_calls=[_ToolCall(_Function("not-json"))]))]
    )
    create = StubCreate([malformed, malformed])

    with pytest.raises(ExtractionError):
        await provider(create).extract(NOW_TEXT)

    assert len(create.calls) == 2


async def test_schema_validation_failure_is_repaired_once() -> None:
    create = StubCreate(
        [
            tool_response({"reports": "not-a-list"}),
            tool_response(parsed_payload()),
        ]
    )

    result = await provider(create).extract(NOW_TEXT)

    assert result.context.entity_name == "Shelter 4"
    assert len(create.calls) == 2
    assert "validation_feedback" in create.calls[1]["messages"][1]["content"]


async def test_critique_returns_a_structured_verdict() -> None:
    create = StubCreate([tool_response({"verdict": "approve", "objections": []})])

    result = await provider(create).critique(
        CriticContext(incident_id=INCIDENT_ID, need_ref="RN-100001")
    )

    assert result == CriticVerdict(verdict="approve", objections=[])
    assert create.calls[0]["tools"][0]["function"]["name"] == "emit_result"


async def test_provider_unavailable_is_not_swallowed() -> None:
    create = StubCreate([ProviderUnavailableError("outage", retryable=True)])

    with pytest.raises(ProviderUnavailableError):
        await provider(create).extract(NOW_TEXT)

    assert len(create.calls) == 1


async def test_generic_openai_error_is_non_retryable() -> None:
    create = StubCreate([openai.OpenAIError("boom")])

    with pytest.raises(ProviderUnavailableError) as excinfo:
        await provider(create).extract(NOW_TEXT)

    assert excinfo.value.retryable is False


async def test_retired_model_falls_back_without_a_repair_attempt() -> None:
    create = StubCreate(
        [
            retired_model_error("meta/llama-3.3-70b-instruct"),
            tool_response(parsed_payload()),
        ]
    )

    result = await provider(
        create, fallback_models=("nvidia/llama-3.3-nemotron-super-49b-v1.5",)
    ).extract(NOW_TEXT)

    assert result.context.entity_name == "Shelter 4"
    assert len(create.calls) == 2
    assert create.calls[0]["model"] == "meta/llama-3.3-70b-instruct"
    assert create.calls[1]["model"] == "nvidia/llama-3.3-nemotron-super-49b-v1.5"


async def test_all_retired_models_raise_a_non_retryable_error() -> None:
    create = StubCreate(
        [
            retired_model_error("primary"),
            retired_model_error("fallback"),
        ]
    )

    with pytest.raises(ProviderUnavailableError) as excinfo:
        await provider(create, fallback_models=("fallback",)).extract(NOW_TEXT)

    assert excinfo.value.retryable is False
    assert len(create.calls) == 2
