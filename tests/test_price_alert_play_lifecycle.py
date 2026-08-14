from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from price_alerts.contracts import ContractError  # noqa: E402
from price_alerts.play import _parse_subscriptions_v2_response  # noqa: E402


NOW = datetime(2026, 8, 14, 12, tzinfo=timezone.utc)
PACKAGE = "com.northstack.stackwatch"
PRODUCT = "stackwatch_pro"
BASE_PLAN = "monthly"


class PlaySubscriptionLifecycleTests(unittest.TestCase):
    def test_active_acknowledged_subscription_grants_until_expiry(self) -> None:
        result = _parse(_response("SUBSCRIPTION_STATE_ACTIVE"))

        self.assertEqual(result.status, "active")
        self.assertEqual(result.raw_state, "SUBSCRIPTION_STATE_ACTIVE")
        self.assertEqual(result.verified_until_utc, NOW + timedelta(hours=12))

    def test_grace_period_remains_entitled(self) -> None:
        result = _parse(_response("SUBSCRIPTION_STATE_IN_GRACE_PERIOD"))

        self.assertEqual(result.status, "grace")

    def test_cancelled_subscription_remains_active_until_expiry(self) -> None:
        result = _parse(_response("SUBSCRIPTION_STATE_CANCELED"))

        self.assertEqual(result.status, "active")
        self.assertEqual(result.raw_state, "SUBSCRIPTION_STATE_CANCELED")

    def test_expired_cancelled_paused_on_hold_and_revoked_fail_closed(self) -> None:
        for state in [
            "SUBSCRIPTION_STATE_EXPIRED",
            "SUBSCRIPTION_STATE_PAUSED",
            "SUBSCRIPTION_STATE_ON_HOLD",
            "SUBSCRIPTION_STATE_PENDING_PURCHASE_CANCELED",
            "SUBSCRIPTION_STATE_REVOKED",
        ]:
            with self.subTest(state=state):
                result = _parse(_response(state))
                self.assertEqual(result.status, "inactive")

    def test_active_subscription_with_past_expiry_fails_closed(self) -> None:
        result = _parse(
            _response(
                "SUBSCRIPTION_STATE_ACTIVE",
                expiry=NOW - timedelta(seconds=1),
            )
        )

        self.assertEqual(result.status, "unknown")

    def test_pending_unknown_malformed_and_unacknowledged_fail_closed(self) -> None:
        pending = _parse(_response("SUBSCRIPTION_STATE_PENDING"))
        self.assertEqual(pending.status, "unknown")

        unknown = _parse(_response("SUBSCRIPTION_STATE_FUTURE_NEW_STATE"))
        self.assertEqual(unknown.status, "unknown")

        malformed = _parse(_response("SUBSCRIPTION_STATE_ACTIVE", expiry_text="bad"))
        self.assertEqual(malformed.status, "unknown")

        unacknowledged = _parse(
            _response(
                "SUBSCRIPTION_STATE_ACTIVE",
                acknowledgement_state="ACKNOWLEDGEMENT_STATE_PENDING",
            )
        )
        self.assertEqual(unacknowledged.status, "unknown")

    def test_wrong_package_product_or_base_plan_rejected(self) -> None:
        with self.assertRaises(ContractError):
            _parse(_response("SUBSCRIPTION_STATE_ACTIVE", package_id="wrong.package"))
        with self.assertRaises(ContractError):
            _parse(_response("SUBSCRIPTION_STATE_ACTIVE", product_id="wrong_product"))
        with self.assertRaises(ContractError):
            _parse(_response("SUBSCRIPTION_STATE_ACTIVE", base_plan_id="annual"))

    def test_test_purchase_indicator_is_preserved_without_special_access(self) -> None:
        result = _parse(_response("SUBSCRIPTION_STATE_ACTIVE", test_purchase=True))

        self.assertTrue(result.is_test_purchase)
        self.assertEqual(result.status, "active")


def _parse(response: dict[str, object]):
    return _parse_subscriptions_v2_response(
        response,
        now_utc=NOW,
        expected_package_id=PACKAGE,
        expected_product_id=PRODUCT,
        expected_base_plan_id=BASE_PLAN,
    )


def _response(
    state: str,
    *,
    package_id: str = PACKAGE,
    product_id: str = PRODUCT,
    base_plan_id: str = BASE_PLAN,
    expiry: datetime | None = None,
    expiry_text: str | None = None,
    acknowledgement_state: str = "ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED",
    test_purchase: bool = False,
) -> dict[str, object]:
    expiry_value = expiry_text
    if expiry_value is None:
        expiry_value = (expiry or (NOW + timedelta(days=30))).isoformat().replace(
            "+00:00",
            "Z",
        )
    response: dict[str, object] = {
        "packageName": package_id,
        "subscriptionState": state,
        "acknowledgementState": acknowledgement_state,
        "lineItems": [
            {
                "productId": product_id,
                "expiryTime": expiry_value,
                "offerDetails": {"basePlanId": base_plan_id},
            }
        ],
    }
    if test_purchase:
        response["testPurchase"] = {}
    return response


if __name__ == "__main__":
    unittest.main()
