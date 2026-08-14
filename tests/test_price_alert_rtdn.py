from __future__ import annotations

import base64
import json
import sys
import unittest
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from price_alerts.contracts import ContractError  # noqa: E402
from price_alerts.rtdn import parse_rtdn_pubsub_message  # noqa: E402


PACKAGE = "com.northstack.stackwatch"
PRODUCT = "stackwatch_pro"


class RtdnBoundaryTests(unittest.TestCase):
    def test_subscription_notification_requires_play_reverification(self) -> None:
        event = parse_rtdn_pubsub_message(
            _pubsub_message("msg-1", notification_type=3),
            expected_package_id=PACKAGE,
            expected_product_id=PRODUCT,
        )

        self.assertEqual(event.message_id, "msg-1")
        self.assertEqual(event.product_id, PRODUCT)
        self.assertEqual(event.notification_type, 3)
        self.assertTrue(event.requires_play_reverification)
        self.assertIn("msg-1", event.idempotency_key)
        self.assertNotIn("purchase-token", repr(event))

    def test_duplicate_message_produces_same_idempotency_key(self) -> None:
        first = parse_rtdn_pubsub_message(
            _pubsub_message("msg-duplicate", notification_type=2),
            expected_package_id=PACKAGE,
            expected_product_id=PRODUCT,
        )
        second = parse_rtdn_pubsub_message(
            _pubsub_message("msg-duplicate", notification_type=2),
            expected_package_id=PACKAGE,
            expected_product_id=PRODUCT,
        )

        self.assertEqual(first.idempotency_key, second.idempotency_key)

    def test_current_documented_subscription_types_are_accepted(self) -> None:
        for notification_type in [17, 18, 19, 20, 22]:
            with self.subTest(notification_type=notification_type):
                event = parse_rtdn_pubsub_message(
                    _pubsub_message("msg-current", notification_type=notification_type),
                    expected_package_id=PACKAGE,
                    expected_product_id=PRODUCT,
                )

                self.assertEqual(event.notification_type, notification_type)
                self.assertTrue(event.requires_play_reverification)

    def test_wrong_package_or_subscription_rejected(self) -> None:
        with self.assertRaises(ContractError):
            parse_rtdn_pubsub_message(
                _pubsub_message("msg-2", package_id="wrong.package"),
                expected_package_id=PACKAGE,
                expected_product_id=PRODUCT,
            )
        with self.assertRaises(ContractError):
            parse_rtdn_pubsub_message(
                _pubsub_message("msg-3", product_id="wrong_product"),
                expected_package_id=PACKAGE,
                expected_product_id=PRODUCT,
            )

    def test_malformed_or_unsupported_messages_rejected(self) -> None:
        for payload in [
            {},
            {"message": {"messageId": "msg", "data": "not-base64"}},
            _pubsub_message("msg-4", notification_type=999),
            _pubsub_message("msg-5", purchase_token=""),
        ]:
            with self.subTest(payload=payload):
                with self.assertRaises(ContractError):
                    parse_rtdn_pubsub_message(
                        payload,
                        expected_package_id=PACKAGE,
                        expected_product_id=PRODUCT,
                    )


def _pubsub_message(
    message_id: str,
    *,
    package_id: str = PACKAGE,
    product_id: str = PRODUCT,
    notification_type: int = 4,
    purchase_token: str = "purchase-token-for-tests",
) -> dict[str, object]:
    payload = {
        "version": "1.0",
        "packageName": package_id,
        "eventTimeMillis": "1786723200000",
        "subscriptionNotification": {
            "version": "1.0",
            "notificationType": notification_type,
            "purchaseToken": purchase_token,
            "subscriptionId": product_id,
        },
    }
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    return {"message": {"messageId": message_id, "data": encoded}}


if __name__ == "__main__":
    unittest.main()
