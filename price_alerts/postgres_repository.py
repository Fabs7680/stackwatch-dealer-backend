from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .contracts import ContractError
from .decimal_utils import decimal_to_unscaled
from .models import (
    EntitlementState,
    Installation,
    NotificationPreferences,
    PriceAlertDefinition,
    PriceAlertObservation,
    PriceAlertSource,
    PriceAlertTriggerEvent,
    QuietHoursPolicy,
    SOURCE_SPOT,
    as_utc,
)
from .providers import FxSnapshot, SpotProviderSnapshot
from .repository import FcmTokenRegistration, _spot_source


class PostgresPriceAlertRepository:
    def __init__(self, *, database_url: str) -> None:
        if not database_url.strip():
            raise ContractError("service_unavailable", "DATABASE_URL is not configured")
        self._database_url = database_url
        self._worker_lock_connection = None

    def create_installation(
        self,
        *,
        installation: Installation,
        encoded_secret_hash: str,
        preferences: NotificationPreferences,
    ) -> None:
        def run(conn) -> None:
            with conn.transaction():
                conn.execute(
                    """
                    INSERT INTO price_alert_installations(
                        installation_id, platform, package_id, app_version_name,
                        app_version_code, locale, time_zone_id, created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        installation.installation_id,
                        installation.platform,
                        installation.package_id,
                        installation.app_version_name,
                        installation.app_version_code,
                        installation.locale,
                        installation.time_zone_id,
                        installation.created_at_utc,
                        installation.updated_at_utc,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO price_alert_installation_credentials(
                        installation_id, secret_hash
                    ) VALUES (%s, %s)
                    """,
                    (installation.installation_id, encoded_secret_hash),
                )
                self._upsert_preferences(conn, installation.installation_id, preferences)

        self._with_connection(run)

    def credential_hash_for_installation(self, installation_id: str) -> str | None:
        def run(conn):
            row = conn.execute(
                """
                SELECT c.secret_hash
                FROM price_alert_installation_credentials c
                JOIN price_alert_installations i USING (installation_id)
                WHERE c.installation_id = %s AND i.deleted_at IS NULL
                """,
                (installation_id,),
            ).fetchone()
            return row[0] if row else None

        return self._with_connection(run)

    def update_installation_settings(
        self,
        *,
        installation_id: str,
        locale: str | None,
        time_zone_id: str | None,
        preferences: NotificationPreferences,
    ) -> None:
        def run(conn) -> None:
            with conn.transaction():
                conn.execute(
                    """
                    UPDATE price_alert_installations
                    SET locale = %s, time_zone_id = %s, updated_at = now()
                    WHERE installation_id = %s AND deleted_at IS NULL
                    """,
                    (locale, time_zone_id, installation_id),
                )
                self._upsert_preferences(conn, installation_id, preferences)

        self._with_connection(run)

    def upsert_entitlement(self, *, installation_id: str, entitlement: EntitlementState) -> None:
        entitlement_id = hashlib.sha256(f"{installation_id}:stackwatch_pro".encode("utf-8")).hexdigest()

        def run(conn) -> None:
            conn.execute(
                """
                INSERT INTO price_alert_entitlements(
                    entitlement_id, installation_id, package_id, product_id,
                    purchase_token_hash, status, verified_until, expires_at,
                    last_verified_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (entitlement_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    verified_until = EXCLUDED.verified_until,
                    expires_at = EXCLUDED.expires_at,
                    last_verified_at = EXCLUDED.last_verified_at,
                    updated_at = now()
                """,
                (
                    entitlement_id,
                    installation_id,
                    "com.northstack.stackwatch",
                    "stackwatch_pro",
                    "not-retained",
                    entitlement.status,
                    entitlement.verified_until_utc,
                    entitlement.expires_at_utc,
                    entitlement.last_verified_at_utc,
                ),
            )

        self._with_connection(run)

    def entitlement_for_installation(self, installation_id: str) -> EntitlementState | None:
        def run(conn):
            row = conn.execute(
                """
                SELECT status, verified_until, expires_at, last_verified_at
                FROM price_alert_entitlements
                WHERE installation_id = %s
                ORDER BY last_verified_at DESC
                LIMIT 1
                """,
                (installation_id,),
            ).fetchone()
            if row is None:
                return None
            return EntitlementState(
                status=row[0],
                verified_until_utc=row[1],
                expires_at_utc=row[2],
                last_verified_at_utc=row[3],
            )

        return self._with_connection(run)

    def upsert_fcm_token(
        self,
        *,
        installation_id: str,
        token_hash: str,
        token_ciphertext: bytes,
        key_version: str,
        platform: str,
        token_issued_at_utc: datetime,
    ) -> None:
        token_id = hashlib.sha256(token_hash.encode("utf-8")).hexdigest()

        def run(conn) -> None:
            conn.execute(
                """
                INSERT INTO price_alert_fcm_tokens(
                    token_id, installation_id, token_hash, token_ciphertext,
                    token_key_version, platform, token_issued_at, last_seen_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (token_hash) DO UPDATE SET
                    installation_id = EXCLUDED.installation_id,
                    token_ciphertext = EXCLUDED.token_ciphertext,
                    token_key_version = EXCLUDED.token_key_version,
                    platform = EXCLUDED.platform,
                    token_issued_at = EXCLUDED.token_issued_at,
                    last_seen_at = now(),
                    revoked_at = NULL,
                    updated_at = now()
                """,
                (
                    token_id,
                    installation_id,
                    token_hash,
                    token_ciphertext,
                    key_version,
                    platform,
                    token_issued_at_utc,
                ),
            )

        self._with_connection(run)

    def revoke_fcm_token(self, *, installation_id: str, token_hash: str | None = None) -> None:
        def run(conn) -> None:
            if token_hash:
                conn.execute(
                    """
                    UPDATE price_alert_fcm_tokens
                    SET revoked_at = now(), updated_at = now()
                    WHERE installation_id = %s AND token_hash = %s
                    """,
                    (installation_id, token_hash),
                )
            else:
                conn.execute(
                    """
                    UPDATE price_alert_fcm_tokens
                    SET revoked_at = now(), updated_at = now()
                    WHERE installation_id = %s
                    """,
                    (installation_id,),
                )

        self._with_connection(run)

    def active_fcm_tokens_for_installation(self, *, installation_id: str) -> list[FcmTokenRegistration]:
        def run(conn) -> list[FcmTokenRegistration]:
            rows = conn.execute(
                """
                SELECT token_hash, token_ciphertext, token_key_version, platform
                FROM price_alert_fcm_tokens
                WHERE installation_id = %s AND revoked_at IS NULL
                ORDER BY last_seen_at DESC
                """,
                (installation_id,),
            ).fetchall()
            return [
                FcmTokenRegistration(
                    token_hash=row[0],
                    token_ciphertext=bytes(row[1] or b""),
                    key_version=row[2] or "",
                    platform=row[3],
                )
                for row in rows
            ]

        return self._with_connection(run)

    def upsert_alert(self, alert: PriceAlertDefinition) -> PriceAlertDefinition:
        return self._save_alert(alert)

    def alert(self, *, installation_id: str, alert_id: str) -> PriceAlertDefinition | None:
        def run(conn):
            row = conn.execute(
                """
                SELECT
                    d.alert_id, d.installation_id, d.created_at, d.updated_at,
                    d.status, d.source_kind, d.provider_id, d.metal_id,
                    d.dealer_id, d.dealer_country_code, d.product_id,
                    d.quote_id, d.quote_side, d.source_currency_code,
                    d.source_unit_id, d.price_basis, d.source_url,
                    d.source_verified, d.alert_currency_code, d.condition,
                    d.target_numeric, d.unit_id, d.rearm_required,
                    d.restored_review_required, d.revision,
                    s.baseline_observation_id, s.last_observation_id,
                    s.last_comparison_state, s.triggered_at,
                    s.triggered_observation_id,
                    p.quiet_hours_enabled, p.quiet_hours_start_minute,
                    p.quiet_hours_end_minute, p.quiet_hours_time_zone_id
                FROM price_alert_definitions d
                LEFT JOIN price_alert_states s USING (alert_id)
                LEFT JOIN price_alert_notification_preferences p USING (installation_id)
                WHERE d.installation_id = %s AND d.alert_id = %s
                """,
                (installation_id, alert_id),
            ).fetchone()
            return None if row is None else _alert_from_row(row)

        return self._with_connection(run)

    def update_alert(self, alert: PriceAlertDefinition) -> PriceAlertDefinition:
        return self._save_alert(alert.copy_with(revision=alert.revision + 1))

    def delete_alert(self, *, installation_id: str, alert_id: str, deleted_at_utc: datetime) -> None:
        def run(conn) -> None:
            with conn.transaction():
                conn.execute(
                    "DELETE FROM price_alert_definitions WHERE installation_id = %s AND alert_id = %s",
                    (installation_id, alert_id),
                )
                conn.execute(
                    """
                    INSERT INTO price_alert_deletion_tombstones(
                        tombstone_id, installation_id, entity_type, entity_id, reason, deleted_at, expires_at
                    ) VALUES (%s, %s, 'alert', %s, 'user_deleted', %s, %s)
                    ON CONFLICT (installation_id, entity_type, entity_id) DO NOTHING
                    """,
                    (
                        hashlib.sha256(f"{installation_id}:alert:{alert_id}".encode("utf-8")).hexdigest(),
                        installation_id,
                        alert_id,
                        deleted_at_utc,
                        deleted_at_utc + timedelta(days=90),
                    ),
                )

        self._with_connection(run)

    def delete_all_alerts(self, *, installation_id: str, deleted_at_utc: datetime) -> None:
        def run(conn) -> None:
            rows = conn.execute(
                "SELECT alert_id FROM price_alert_definitions WHERE installation_id = %s",
                (installation_id,),
            ).fetchall()
            with conn.transaction():
                conn.execute(
                    "DELETE FROM price_alert_definitions WHERE installation_id = %s",
                    (installation_id,),
                )
                for row in rows:
                    alert_id = row[0]
                    conn.execute(
                        """
                        INSERT INTO price_alert_deletion_tombstones(
                            tombstone_id, installation_id, entity_type, entity_id, reason, deleted_at, expires_at
                        ) VALUES (%s, %s, 'alert', %s, 'user_deleted_all', %s, %s)
                        ON CONFLICT (installation_id, entity_type, entity_id) DO NOTHING
                        """,
                        (
                            hashlib.sha256(f"{installation_id}:alert:{alert_id}".encode("utf-8")).hexdigest(),
                            installation_id,
                            alert_id,
                            deleted_at_utc,
                            deleted_at_utc + timedelta(days=90),
                        ),
                    )

        self._with_connection(run)

    def list_alerts(self, *, installation_id: str, limit: int) -> list[PriceAlertDefinition]:
        def run(conn) -> list[PriceAlertDefinition]:
            rows = conn.execute(
                """
                SELECT
                    d.alert_id, d.installation_id, d.created_at, d.updated_at,
                    d.status, d.source_kind, d.provider_id, d.metal_id,
                    d.dealer_id, d.dealer_country_code, d.product_id,
                    d.quote_id, d.quote_side, d.source_currency_code,
                    d.source_unit_id, d.price_basis, d.source_url,
                    d.source_verified, d.alert_currency_code, d.condition,
                    d.target_numeric, d.unit_id, d.rearm_required,
                    d.restored_review_required, d.revision,
                    s.baseline_observation_id, s.last_observation_id,
                    s.last_comparison_state, s.triggered_at,
                    s.triggered_observation_id,
                    p.quiet_hours_enabled, p.quiet_hours_start_minute,
                    p.quiet_hours_end_minute, p.quiet_hours_time_zone_id
                FROM price_alert_definitions d
                LEFT JOIN price_alert_states s USING (alert_id)
                LEFT JOIN price_alert_notification_preferences p USING (installation_id)
                WHERE d.installation_id = %s
                ORDER BY d.updated_at DESC
                LIMIT %s
                """,
                (installation_id, limit),
            ).fetchall()
            return [_alert_from_row(row) for row in rows]

        return self._with_connection(run)

    def list_trigger_events(self, *, installation_id: str, limit: int) -> list[PriceAlertTriggerEvent]:
        def run(conn) -> list[PriceAlertTriggerEvent]:
            rows = conn.execute(
                """
                SELECT
                    event_id, alert_id, installation_id, triggered_at,
                    observation_id, source_kind, metal_id, condition,
                    target_numeric, triggered_price_numeric,
                    alert_currency_code, unit_id, price_basis,
                    provider_timestamp, dealer_id, product_id, revision
                FROM price_alert_trigger_events
                WHERE installation_id = %s
                ORDER BY triggered_at DESC
                LIMIT %s
                """,
                (installation_id, limit),
            ).fetchall()
            return [_trigger_event_from_row(row) for row in rows]

        return self._with_connection(run)

    def count_resumable_alerts(self, *, installation_id: str, exclude_alert_id: str | None = None) -> int:
        def run(conn) -> int:
            row = conn.execute(
                """
                SELECT count(*)
                FROM price_alert_definitions
                WHERE installation_id = %s
                  AND (%s IS NULL OR alert_id <> %s)
                  AND status IN (
                    'activeWaitingForBaseline', 'activeArmed', 'paused',
                    'sourceUnavailable', 'notificationPermissionRequired',
                    'proSuspended', 'restoreReviewRequired'
                  )
                """,
                (installation_id, exclude_alert_id, exclude_alert_id),
            ).fetchone()
            return int(row[0])

        return self._with_connection(run)

    def persist_spot_snapshot(self, snapshot: SpotProviderSnapshot) -> list[PriceAlertObservation]:
        observations = []
        for item in snapshot.observations:
            source = _spot_source(item)
            price_unscaled, price_scale = decimal_to_unscaled(item.usd_per_troy_ounce)
            observation = PriceAlertObservation(
                observation_id=item.observation_id,
                source=source,
                metal_id=item.metal_id,
                price=item.usd_per_troy_ounce,
                currency_code="USD",
                unit_id="oz",
                price_basis="perUnit",
                provider_timestamp_utc=item.provider_timestamp_utc,
                received_at_utc=item.received_at_utc,
                is_authoritative=True,
                is_cached=False,
                is_stale=False,
                source_available=True,
                fx_required=False,
                fx_is_stale=False,
                valid_until_utc=item.received_at_utc + timedelta(minutes=35),
                source_url=item.source_url,
            )
            observations.append(observation)

            def run(conn) -> None:
                conn.execute(
                    """
                    INSERT INTO price_alert_quote_observations(
                        observation_id, source_kind, provider_id, metal_id,
                        quote_id, currency_code, unit_id, price_basis,
                        price_numeric, price_unscaled, price_scale,
                        provider_timestamp, received_at, valid_until,
                        is_authoritative, is_cached, is_stale, source_available,
                        fx_required, fx_is_stale, source_url
                    ) VALUES (%s, 'spot', %s, %s, %s, 'USD', 'oz', 'perUnit',
                              %s, %s, %s, %s, %s, %s, TRUE, FALSE, FALSE, TRUE,
                              FALSE, FALSE, %s)
                    ON CONFLICT (observation_id) DO NOTHING
                    """,
                    (
                        item.observation_id,
                        source.provider_id,
                        item.metal_id,
                        source.quote_id,
                        item.usd_per_troy_ounce,
                        price_unscaled,
                        price_scale,
                        item.provider_timestamp_utc,
                        item.received_at_utc,
                        observation.valid_until_utc,
                        item.source_url,
                    ),
                )

            self._with_connection(run)
        return observations

    def persist_fx_snapshot(self, snapshot: FxSnapshot) -> None:
        def run(conn) -> None:
            with conn.transaction():
                for currency_code, rate in snapshot.rates.items():
                    rate_unscaled, rate_scale = decimal_to_unscaled(rate)
                    conn.execute(
                        """
                        INSERT INTO price_alert_fx_observations(
                            observation_id, provider_id, base_currency_code,
                            quote_currency_code, rate_numeric, rate_unscaled,
                            rate_scale, provider_timestamp, received_at,
                            is_authoritative, is_stale
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE, FALSE)
                        ON CONFLICT (observation_id) DO NOTHING
                        """,
                        (
                            hashlib.sha256(
                                f"{snapshot.provider_id}:USD:{currency_code}:{snapshot.provider_timestamp_utc.isoformat()}".encode("utf-8")
                            ).hexdigest(),
                            snapshot.provider_id,
                            "USD",
                            currency_code,
                            rate,
                            rate_unscaled,
                            rate_scale,
                            snapshot.provider_timestamp_utc,
                            snapshot.received_at_utc,
                        ),
                    )

        self._with_connection(run)

    def latest_fx_snapshot(self) -> FxSnapshot | None:
        def run(conn) -> FxSnapshot | None:
            rows = conn.execute(
                """
                WITH latest AS (
                    SELECT provider_id, provider_timestamp, received_at
                    FROM price_alert_fx_observations
                    WHERE base_currency_code = 'USD'
                      AND is_authoritative = TRUE
                      AND is_stale = FALSE
                    ORDER BY provider_timestamp DESC
                    LIMIT 1
                )
                SELECT f.provider_id, f.quote_currency_code, f.rate_numeric,
                       f.provider_timestamp, f.received_at
                FROM price_alert_fx_observations f
                JOIN latest l
                  ON f.provider_id = l.provider_id
                 AND f.provider_timestamp = l.provider_timestamp
                 AND f.received_at = l.received_at
                WHERE f.base_currency_code = 'USD'
                  AND f.is_authoritative = TRUE
                  AND f.is_stale = FALSE
                """,
            ).fetchall()
            if not rows:
                return None
            rates = {"USD": Decimal("1")}
            for row in rows:
                rates[str(row[1])] = Decimal(row[2])
            first = rows[0]
            return FxSnapshot(
                provider_id=first[0],
                base_currency_code="USD",
                rates=rates,
                provider_timestamp_utc=as_utc(first[3]),
                received_at_utc=as_utc(first[4]),
            )

        return self._with_connection(run)

    def list_active_alerts(self, *, limit: int) -> list[PriceAlertDefinition]:
        def run(conn) -> list[PriceAlertDefinition]:
            rows = conn.execute(
                """
                SELECT
                    d.alert_id, d.installation_id, d.created_at, d.updated_at,
                    d.status, d.source_kind, d.provider_id, d.metal_id,
                    d.dealer_id, d.dealer_country_code, d.product_id,
                    d.quote_id, d.quote_side, d.source_currency_code,
                    d.source_unit_id, d.price_basis, d.source_url,
                    d.source_verified, d.alert_currency_code, d.condition,
                    d.target_numeric, d.unit_id, d.rearm_required,
                    d.restored_review_required, d.revision,
                    s.baseline_observation_id, s.last_observation_id,
                    s.last_comparison_state, s.triggered_at,
                    s.triggered_observation_id,
                    p.quiet_hours_enabled, p.quiet_hours_start_minute,
                    p.quiet_hours_end_minute, p.quiet_hours_time_zone_id
                FROM price_alert_definitions d
                LEFT JOIN price_alert_states s USING (alert_id)
                LEFT JOIN price_alert_notification_preferences p USING (installation_id)
                WHERE d.status IN ('activeWaitingForBaseline', 'activeArmed')
                  AND d.source_kind = 'spot'
                ORDER BY d.updated_at ASC
                LIMIT %s
                """,
                (limit,),
            ).fetchall()
            return [_alert_from_row(row) for row in rows]

        return self._with_connection(run)

    def create_trigger_event_once(self, event: PriceAlertTriggerEvent) -> bool:
        target_unscaled, target_scale = decimal_to_unscaled(event.target)
        price_unscaled, price_scale = decimal_to_unscaled(event.triggered_price)

        def run(conn) -> bool:
            result = conn.execute(
                """
                INSERT INTO price_alert_trigger_events(
                    event_id, alert_id, installation_id, observation_id,
                    source_kind, metal_id, condition, target_numeric,
                    target_unscaled, target_scale, triggered_price_numeric,
                    triggered_price_unscaled, triggered_price_scale,
                    alert_currency_code, unit_id, price_basis,
                    provider_timestamp, triggered_at, dealer_id, product_id,
                    revision
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (alert_id, observation_id) DO NOTHING
                """,
                (
                    event.event_id,
                    event.alert_id,
                    event.installation_id,
                    event.observation_id,
                    event.source.source_kind,
                    event.metal_id,
                    event.condition,
                    event.target,
                    target_unscaled,
                    target_scale,
                    event.triggered_price,
                    price_unscaled,
                    price_scale,
                    event.alert_currency_code,
                    event.unit_id,
                    event.price_basis,
                    event.provider_timestamp_utc,
                    event.triggered_at_utc,
                    event.dealer_id,
                    event.product_id,
                    event.revision,
                ),
            )
            return result.rowcount == 1

        return self._with_connection(run)

    def create_delivery_once(
        self,
        *,
        installation_id: str,
        event_id: str,
        payload_hash: str,
        delivery_state: str,
        fcm_token_hash: str | None = None,
    ) -> bool:
        delivery_id = _delivery_id(
            installation_id=installation_id,
            event_id=event_id,
            payload_hash=payload_hash,
            fcm_token_hash=fcm_token_hash,
        )

        def run(conn) -> bool:
            result = conn.execute(
                """
                INSERT INTO price_alert_notification_deliveries(
                    delivery_id, installation_id, event_id, delivery_state,
                    notification_payload_hash, fcm_token_hash
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (delivery_id) DO NOTHING
                """,
                (
                    delivery_id,
                    installation_id,
                    event_id,
                    delivery_state,
                    payload_hash,
                    fcm_token_hash,
                ),
            )
            return result.rowcount == 1

        return self._with_connection(run)

    def update_delivery_result(
        self,
        *,
        installation_id: str,
        event_id: str,
        payload_hash: str,
        delivery_state: str,
        fcm_token_hash: str | None,
        provider_message_id: str | None,
        delivered_at_utc: datetime | None,
    ) -> None:
        delivery_id = _delivery_id(
            installation_id=installation_id,
            event_id=event_id,
            payload_hash=payload_hash,
            fcm_token_hash=fcm_token_hash,
        )

        def run(conn) -> None:
            conn.execute(
                """
                UPDATE price_alert_notification_deliveries
                SET delivery_state = %s,
                    provider_message_id = %s,
                    delivered_at = %s
                WHERE delivery_id = %s
                """,
                (delivery_state, provider_message_id, delivered_at_utc, delivery_id),
            )

        self._with_connection(run)

    def count_provider_attempts(
        self,
        *,
        provider_id: str,
        cycle_start_utc: datetime,
        cycle_end_utc: datetime,
    ) -> int:
        def run(conn) -> int:
            row = conn.execute(
                """
                SELECT count(*)
                FROM price_alert_provider_usage_records
                WHERE provider_id = %s
                  AND result = 'started'
                  AND attempted_at >= %s
                  AND attempted_at < %s
                """,
                (provider_id, cycle_start_utc, cycle_end_utc),
            ).fetchone()
            return int(row[0])

        return self._with_connection(run)

    def record_provider_attempt(
        self,
        *,
        provider_id: str,
        attempted_at_utc: datetime,
        result: str,
        reason: str,
    ) -> None:
        usage_id = hashlib.sha256(
            f"{provider_id}:{attempted_at_utc.isoformat()}:{result}:{reason}".encode("utf-8")
        ).hexdigest()

        def run(conn) -> None:
            conn.execute(
                """
                INSERT INTO price_alert_provider_usage_records(
                    usage_id, provider_id, attempted_at, result, reason
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (usage_id) DO NOTHING
                """,
                (usage_id, provider_id, attempted_at_utc, result, reason[:128]),
            )

        self._with_connection(run)

    def acquire_worker_lock(self, *, lock_name: str, now_utc: datetime) -> bool:
        lock_id = int(hashlib.sha256(lock_name.encode("utf-8")).hexdigest()[:15], 16)
        if self._worker_lock_connection is not None:
            return False
        try:
            import psycopg
        except Exception as exc:
            raise ContractError("service_unavailable", "psycopg dependency unavailable") from exc
        conn = psycopg.connect(self._database_url)
        row = conn.execute("SELECT pg_try_advisory_lock(%s)", (lock_id,)).fetchone()
        if bool(row[0]):
            self._worker_lock_connection = conn
            return True
        conn.close()
        return False

    def release_worker_lock(self, *, lock_name: str) -> None:
        lock_id = int(hashlib.sha256(lock_name.encode("utf-8")).hexdigest()[:15], 16)
        conn = self._worker_lock_connection
        self._worker_lock_connection = None
        if conn is None:
            return
        try:
            conn.execute("SELECT pg_advisory_unlock(%s)", (lock_id,))
        finally:
            conn.close()

    def _save_alert(self, alert: PriceAlertDefinition) -> PriceAlertDefinition:
        target_unscaled, target_scale = decimal_to_unscaled(alert.target)

        def run(conn) -> PriceAlertDefinition:
            with conn.transaction():
                result = conn.execute(
                    """
                    INSERT INTO price_alert_definitions(
                        alert_id, installation_id, status, source_kind,
                        provider_id, metal_id, dealer_id, dealer_country_code,
                        product_id, quote_id, quote_side, source_currency_code,
                        source_unit_id, price_basis, source_url,
                        source_verified, alert_currency_code, condition,
                        target_numeric, target_unscaled, target_scale, unit_id,
                        rearm_required, restored_review_required, revision,
                        created_at, updated_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (alert_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        source_kind = EXCLUDED.source_kind,
                        provider_id = EXCLUDED.provider_id,
                        metal_id = EXCLUDED.metal_id,
                        quote_id = EXCLUDED.quote_id,
                        source_currency_code = EXCLUDED.source_currency_code,
                        source_unit_id = EXCLUDED.source_unit_id,
                        price_basis = EXCLUDED.price_basis,
                        source_verified = EXCLUDED.source_verified,
                        alert_currency_code = EXCLUDED.alert_currency_code,
                        condition = EXCLUDED.condition,
                        target_numeric = EXCLUDED.target_numeric,
                        target_unscaled = EXCLUDED.target_unscaled,
                        target_scale = EXCLUDED.target_scale,
                        unit_id = EXCLUDED.unit_id,
                        rearm_required = EXCLUDED.rearm_required,
                        restored_review_required = EXCLUDED.restored_review_required,
                        revision = price_alert_definitions.revision + 1,
                        updated_at = EXCLUDED.updated_at
                    RETURNING revision
                    """,
                    (
                        alert.alert_id,
                        alert.installation_id,
                        alert.status,
                        alert.source.source_kind,
                        alert.source.provider_id,
                        alert.metal_id,
                        alert.source.dealer_id,
                        alert.source.dealer_country_code,
                        alert.source.product_id,
                        alert.source.quote_id,
                        alert.source.quote_side,
                        alert.source.source_currency_code,
                        alert.source.source_unit_id,
                        alert.price_basis,
                        alert.source.source_url,
                        alert.source.verified,
                        alert.alert_currency_code,
                        alert.condition,
                        alert.target,
                        target_unscaled,
                        target_scale,
                        alert.unit_id,
                        alert.rearm_required,
                        alert.restored_review_required,
                        alert.revision,
                        alert.created_at_utc,
                        alert.updated_at_utc,
                    ),
                )
                revision = int(result.fetchone()[0])
                conn.execute(
                    """
                    INSERT INTO price_alert_states(
                        alert_id, baseline_observation_id, last_observation_id,
                        last_comparison_state, triggered_at,
                        triggered_observation_id, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (alert_id) DO UPDATE SET
                        baseline_observation_id = EXCLUDED.baseline_observation_id,
                        last_observation_id = EXCLUDED.last_observation_id,
                        last_comparison_state = EXCLUDED.last_comparison_state,
                        triggered_at = EXCLUDED.triggered_at,
                        triggered_observation_id = EXCLUDED.triggered_observation_id,
                        updated_at = now()
                    """,
                    (
                        alert.alert_id,
                        alert.baseline_observation_id,
                        alert.last_observation_id,
                        alert.last_comparison_state,
                        alert.triggered_at_utc,
                        alert.triggered_observation_id,
                    ),
                )
                return alert.copy_with(revision=revision)

        return self._with_connection(run)

    def _upsert_preferences(self, conn, installation_id: str, preferences: NotificationPreferences) -> None:
        quiet = preferences.quiet_hours_policy
        conn.execute(
            """
            INSERT INTO price_alert_notification_preferences(
                installation_id, notifications_enabled,
                show_price_details_in_notifications,
                quiet_hours_enabled, quiet_hours_start_minute,
                quiet_hours_end_minute, quiet_hours_time_zone_id, revision
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (installation_id) DO UPDATE SET
                notifications_enabled = EXCLUDED.notifications_enabled,
                show_price_details_in_notifications = EXCLUDED.show_price_details_in_notifications,
                quiet_hours_enabled = EXCLUDED.quiet_hours_enabled,
                quiet_hours_start_minute = EXCLUDED.quiet_hours_start_minute,
                quiet_hours_end_minute = EXCLUDED.quiet_hours_end_minute,
                quiet_hours_time_zone_id = EXCLUDED.quiet_hours_time_zone_id,
                revision = EXCLUDED.revision,
                updated_at = now()
            """,
            (
                installation_id,
                preferences.notifications_enabled,
                preferences.show_price_details_in_notifications,
                quiet.enabled,
                quiet.start_minute,
                quiet.end_minute,
                quiet.time_zone_id,
                preferences.revision,
            ),
        )

    def _with_connection(self, callback):
        try:
            import psycopg
        except Exception as exc:
            raise ContractError("service_unavailable", "psycopg dependency unavailable") from exc
        with psycopg.connect(self._database_url) as conn:
            return callback(conn)


def _delivery_id(
    *,
    installation_id: str,
    event_id: str,
    payload_hash: str,
    fcm_token_hash: str | None,
) -> str:
    token_part = fcm_token_hash or "none"
    return hashlib.sha256(
        f"{installation_id}:{event_id}:{payload_hash}:{token_part}".encode("utf-8")
    ).hexdigest()


def _alert_from_row(row) -> PriceAlertDefinition:
    source = PriceAlertSource(
        source_kind=row[5],
        provider_id=row[6],
        metal_id=row[7],
        dealer_id=row[8],
        dealer_country_code=row[9],
        product_id=row[10],
        quote_id=row[11],
        quote_side=row[12],
        source_currency_code=row[13],
        source_unit_id=row[14],
        price_basis=row[15],
        source_url=row[16],
        verified=bool(row[17]),
    )
    quiet_hours = QuietHoursPolicy(
        enabled=bool(row[30]) if row[30] is not None else False,
        start_minute=int(row[31] or 0),
        end_minute=int(row[32] or 0),
        time_zone_id=row[33],
    )
    return PriceAlertDefinition(
        alert_id=row[0],
        installation_id=row[1],
        created_at_utc=as_utc(row[2]),
        updated_at_utc=as_utc(row[3]),
        status=row[4],
        source=source,
        metal_id=row[7],
        alert_currency_code=row[18],
        condition=row[19],
        target=Decimal(row[20]),
        unit_id=row[21],
        price_basis=row[15],
        rearm_required=bool(row[22]),
        restored_review_required=bool(row[23]),
        revision=int(row[24]),
        baseline_observation_id=row[25],
        last_observation_id=row[26],
        last_comparison_state=row[27],
        triggered_at_utc=as_utc(row[28]) if row[28] else None,
        triggered_observation_id=row[29],
        quiet_hours_policy=quiet_hours,
    )


def _trigger_event_from_row(row) -> PriceAlertTriggerEvent:
    source_kind = row[5]
    metal_id = row[6]
    currency_code = row[10]
    unit_id = row[11]
    price_basis = row[12]
    provider_id = "bullionova-spot" if source_kind == SOURCE_SPOT else "dealer-world"
    quote_id = (
        f"bullionova-spot:{metal_id}:{currency_code}:{unit_id or 'oz'}"
        if source_kind == SOURCE_SPOT
        else f"{row[14]}:{row[15]}:{source_kind}"
    )
    source = PriceAlertSource(
        source_kind=source_kind,
        provider_id=provider_id,
        metal_id=metal_id,
        quote_id=quote_id,
        source_currency_code=currency_code,
        source_unit_id=unit_id,
        price_basis=price_basis,
        verified=source_kind == SOURCE_SPOT,
        dealer_id=row[14],
        product_id=row[15],
    )
    return PriceAlertTriggerEvent(
        event_id=row[0],
        alert_id=row[1],
        installation_id=row[2],
        triggered_at_utc=as_utc(row[3]),
        observation_id=row[4],
        source=source,
        metal_id=metal_id,
        condition=row[7],
        target=Decimal(row[8]),
        triggered_price=Decimal(row[9]),
        alert_currency_code=currency_code,
        unit_id=unit_id,
        price_basis=price_basis,
        provider_timestamp_utc=as_utc(row[13]),
        dealer_id=row[14],
        product_id=row[15],
        revision=int(row[16]),
    )
