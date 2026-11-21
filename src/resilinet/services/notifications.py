"""Plain-language email composition.

Every notification is truthful about state: a proposal is recommended, not
dispatched; an estimate is labelled; and a missing match reserves nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..domain.quantities import Quantity, format_quantity
from ..ports import OutboundDraft, RawMessage
from ..schemas import Resource


@dataclass(frozen=True, slots=True)
class StockLine:
    resource: Resource
    quantity: Quantity


@dataclass(frozen=True, slots=True)
class RecommendationLine:
    need_ref: str
    entity_name: str
    resource: Resource
    quantity: Quantity
    headcount: int | None
    allocation_ref: str | None = None
    supply_name: str | None = None
    distance_km: Decimal | None = None
    requested_quantity: Quantity | None = None


class NotificationService:
    def compose_recommendation(
        self,
        message: RawMessage,
        lines: list[RecommendationLine],
        *,
        unverified: bool,
    ) -> OutboundDraft:
        body: list[str] = []
        for line in lines:
            resource_label = _resource_label(line.resource)
            quantity_label = format_quantity(line.quantity)
            body.append(f"We received your report for {line.entity_name}.")
            body.append("")
            if line.allocation_ref and line.supply_name:
                distance = (
                    f" (approx. {line.distance_km.quantize(Decimal('0.1'))} km away)"
                    if line.distance_km is not None
                    else ""
                )
                body.append(f"Reference: {line.need_ref} (allocation {line.allocation_ref})")
                body.append(
                    f"Recommended: {line.supply_name} is recommended to supply "
                    f"{quantity_label} of {resource_label}{distance}."
                )
                if (
                    line.requested_quantity is not None
                    and line.requested_quantity.amount > line.quantity.amount
                ):
                    body.append(
                        f"This is a partial match for the "
                        f"{format_quantity(line.requested_quantity)} requested; "
                        "the remainder stays open."
                    )
                body.append(
                    "This is a proposal, not a shipment or a dispatch. A depot "
                    "coordinator must confirm it before stock is committed."
                )
            else:
                body.append(f"Reference: {line.need_ref}")
                body.append(
                    f"No stock is currently reserved for {quantity_label} of "
                    f"{resource_label}. The need remains open for matching."
                )
            if line.quantity.source.value == "estimated":
                body.append(
                    f"Quantity: {quantity_label} of {resource_label}, estimated from "
                    f"{line.headcount or 0} people using planning rates for one day."
                )
            body.append(
                f'Reply "QUERY STATUS {line.need_ref}" for an update.'
            )
            body.append("")
        if unverified:
            body.append(
                "Your sender address is not verified yet. Verification upgrades "
                "trust but does not change this recommendation."
            )
        return _reply_to(message, "\n".join(body).strip())

    def compose_stock_update(
        self,
        message: RawMessage,
        entity_name: str,
        lines: list[StockLine],
    ) -> OutboundDraft:
        body = [f"Stock recorded for {entity_name}."]
        for line in lines:
            body.append(
                f"- {format_quantity(line.quantity)} of {_resource_label(line.resource)} on hand"
            )
        body.append("")
        body.append(
            "This is an absolute on-hand snapshot. Reservations are tracked "
            "separately and do not reduce the reported on-hand figure."
        )
        return _reply_to(message, "\n".join(body))

    def compose_clarification(
        self,
        message: RawMessage,
        fields: list[str],
    ) -> OutboundDraft:
        labels = ", ".join(field.replace("_", " ") for field in fields)
        body = (
            "We received your message but need one more detail before we can match it: "
            f"{labels}. Please reply with the {labels}. We will not guess a location, "
            "quantity, or resource."
        )
        return _reply_to(message, body)

    def compose_snapshot_rejected(
        self,
        message: RawMessage,
        entity_name: str,
    ) -> OutboundDraft:
        body = (
            f"The stock snapshot for {entity_name} was not applied because it would "
            "leave existing reservations unsupported. The previous stock figure is "
            "unchanged. A coordinator must review and reconcile it."
        )
        return _reply_to(message, body)


def _resource_label(resource: Resource) -> str:
    return resource.value.replace("_", " ")


def _reply_to(message: RawMessage, body: str) -> OutboundDraft:
    subject = message.subject.strip() or "Your ResiliNet report"
    if not subject.casefold().startswith("re:"):
        subject = f"Re: {subject}"
    return OutboundDraft(
        mailbox_id=message.mailbox_id,
        to=message.sender,
        subject=subject,
        text=body,
        in_reply_to=message.provider_message_id,
    )
