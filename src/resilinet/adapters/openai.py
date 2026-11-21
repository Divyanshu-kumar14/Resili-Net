"""OpenAI structured extraction and bounded adversarial critique.

The email is untrusted data inside ``<email>`` delimiters. The model receives no
tools, cannot change the schema, and its output is validated as a Pydantic model
before it can influence any domain state. A schema-invalid answer gets exactly
one repair attempt; transient provider failures become retryable
``ProviderUnavailableError`` values that the worker schedules.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import openai
from pydantic import BaseModel, ValidationError

from ..ports import CritiqueError, ExtractionError, ProviderUnavailableError, RawMessage
from ..schemas import CriticContext, CriticVerdict, ParsedEmail
from .llm_common import (
    CRITIQUE_INSTRUCTIONS,
    EXTRACTION_INSTRUCTIONS,
    MAX_REPAIR_ATTEMPTS,
    critique_payload,
    email_block,
)

logger = logging.getLogger("resilinet.adapters.openai")

SchemaT = TypeVar("SchemaT", bound=BaseModel)

StructuredParse = Callable[..., Awaitable[Any]]


class OpenAIIntelligenceProvider:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        timeout_seconds: float = 20.0,
        temperature: float | None = 0.0,
        parse: StructuredParse | None = None,
    ) -> None:
        self._model = model
        self._temperature = temperature
        if parse is None:
            client = openai.AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=timeout_seconds,
            )
            self._client: openai.AsyncOpenAI | None = client
            self._parse: StructuredParse = client.responses.parse
        else:
            self._client = None
            self._parse = parse

    async def extract(self, message: RawMessage) -> ParsedEmail:
        return await self._parse_with_repair(
            schema=ParsedEmail,
            instructions=EXTRACTION_INSTRUCTIONS,
            payload=email_block(message),
            error_type=ExtractionError,
        )

    async def critique(self, proposal: CriticContext) -> CriticVerdict:
        return await self._parse_with_repair(
            schema=CriticVerdict,
            instructions=CRITIQUE_INSTRUCTIONS,
            payload=critique_payload(proposal),
            error_type=CritiqueError,
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()

    async def _parse_with_repair(
        self,
        *,
        schema: type[SchemaT],
        instructions: str,
        payload: str,
        error_type: type[Exception],
    ) -> SchemaT:
        feedback: str | None = None
        last_error = "the model returned no parsed output"
        for _attempt in range(MAX_REPAIR_ATTEMPTS + 1):
            content = payload
            if feedback is not None:
                content = (
                    f"{payload}\n\n"
                    "<validation_feedback>The previous answer did not satisfy the "
                    f"required schema: {feedback}. Return a corrected answer that "
                    "validates.</validation_feedback>"
                )
            kwargs: dict[str, Any] = {
                "model": self._model,
                "input": [
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": content},
                ],
                "text_format": schema,
            }
            if self._temperature is not None:
                kwargs["temperature"] = self._temperature
            try:
                response = await self._parse(**kwargs)
            except ProviderUnavailableError:
                raise
            except ValidationError as exc:
                last_error = str(exc)[:800]
                feedback = last_error
                continue
            except (
                openai.APIConnectionError,
                openai.RateLimitError,
                openai.InternalServerError,
            ) as exc:
                raise ProviderUnavailableError(
                    f"openai request failed: {type(exc).__name__}", retryable=True
                ) from exc
            except openai.APIStatusError as exc:
                raise ProviderUnavailableError(
                    f"openai rejected the request with status {exc.status_code}",
                    retryable=exc.status_code >= 500 or exc.status_code == 429,
                ) from exc
            except openai.OpenAIError as exc:
                raise ProviderUnavailableError(
                    f"openai error: {type(exc).__name__}", retryable=False
                ) from exc

            parsed = getattr(response, "output_parsed", None)
            if parsed is None:
                last_error = "the model returned no parsed output (refusal or empty answer)"
                feedback = last_error
                continue
            if isinstance(parsed, schema):
                return parsed
            try:
                return schema.model_validate(parsed)
            except ValidationError as exc:
                last_error = str(exc)[:800]
                feedback = last_error
                continue
        logger.warning(
            "structured_output_failed",
            extra={"schema": schema.__name__, "attempts": MAX_REPAIR_ATTEMPTS + 1},
        )
        raise error_type(
            f"{schema.__name__} was not produced after "
            f"{MAX_REPAIR_ATTEMPTS + 1} attempts: {last_error}"
        )


__all__ = [
    "MAX_REPAIR_ATTEMPTS",
    "OpenAIIntelligenceProvider",
    "StructuredParse",
]

