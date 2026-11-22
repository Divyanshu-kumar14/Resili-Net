"""Plain-language safety checks for notification text."""

from __future__ import annotations

from decimal import Decimal

from resilinet.services.notifications import (
    NotificationService,
    RecommendationText,
    recommendation_body,
    reply_subject,
)


def test_reply_subject_collapses_header_control_characters() -> None:
    subject = reply_subject("Water\r\nBcc: attacker@example.org")
    assert subject == "Re: Water Bcc: attacker@example.org"
    assert "\r" not in subject and "\n" not in subject


def test_recommendation_body_does_not_claim_no_stock_when_depot_name_is_missing() -> None:
    body = recommendation_body(
        [
            RecommendationText(
                need_ref="RN-100001",
                entity_name="Shelter 4",
                resource_label="potable water",
                quantity_label="10 l",
                allocation_ref="AL-100001",
                supply_name=None,
                distance_km=Decimal("1.2"),
            )
        ],
        unverified=False,
    )
    assert "The supplying depot is recommended" in body
    assert "No stock is currently reserved" not in body
    assert "proposal, not a shipment" in body


def test_medical_corroboration_does_not_claim_an_empty_reservation() -> None:
    text = NotificationService().medical_corroboration_text()
    assert "No stock has been reserved" not in text
    assert "Existing reservations are unchanged" in text
    assert "must review" in text
