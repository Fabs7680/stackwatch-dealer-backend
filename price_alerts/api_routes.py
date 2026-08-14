from __future__ import annotations

from typing import Any, Callable

from flask import Blueprint, jsonify, request

from .config import PriceAlertsServerConfig
from .contracts import ApiErrorResponse, ContractError, SCHEMA_VERSION
from .play import GooglePlayDeveloperApiVerifier, UnconfiguredPlayVerifier
from .postgres_repository import PostgresPriceAlertRepository
from .security import token_protector_from_env
from .service import PriceAlertServerService


ServerFactory = Callable[[], PriceAlertServerService]


def create_price_alerts_blueprint(
    *,
    config: PriceAlertsServerConfig | None = None,
    server_factory: ServerFactory | None = None,
) -> Blueprint:
    cfg = config or PriceAlertsServerConfig.from_env()
    blueprint = Blueprint("price_alerts_v1", __name__)

    def service() -> PriceAlertServerService:
        if server_factory is not None:
            return server_factory()
        if not cfg.enabled:
            raise ContractError("service_unavailable", "Price Alerts server is disabled")
        if cfg.readiness_state() != "ready":
            raise ContractError("service_unavailable", "Price Alerts server is not ready")
        play = (
            GooglePlayDeveloperApiVerifier(
                credentials_file=cfg.google_play_credentials_file
            )
            if cfg.play_verification_enabled
            else UnconfiguredPlayVerifier()
        )
        return PriceAlertServerService(
            config=cfg,
            repository=PostgresPriceAlertRepository(database_url=cfg.database_url),
            play_verifier=play,
            token_protector=token_protector_from_env(),
        )

    def disabled_or_call(callback):
        if not cfg.enabled:
            return _error("service_unavailable", "Price Alerts server is disabled", 503)
        try:
            return jsonify(callback())
        except ContractError as exc:
            return _error(exc.code, exc.message, _status_for_code(exc.code))

    def authenticated(callback):
        if not cfg.enabled:
            return _error("service_unavailable", "Price Alerts server is disabled", 503)
        try:
            installation_id, secret = _credentials()
            active_service = service()
            active_service.authenticate(
                installation_id=installation_id,
                installation_secret=secret,
            )
            return jsonify(callback(active_service, installation_id))
        except ContractError as exc:
            return _error(exc.code, exc.message, _status_for_code(exc.code))

    @blueprint.get("/v1/health")
    def health():
        payload = {
            "schemaVersion": SCHEMA_VERSION,
            "ready": cfg.readiness_state() == "ready",
            "priceAlertsEnabled": cfg.enabled,
            "dealerAlertsEnabled": False,
            "serverTimeUtc": _server_time(),
            "priceAlerts": cfg.safe_health_payload(),
        }
        return jsonify(payload), 200 if cfg.readiness_state() in {"disabled", "ready"} else 503

    @blueprint.post("/v1/installations/register")
    def register_installation():
        return disabled_or_call(lambda: service().register_installation(_json_body()))

    @blueprint.patch("/v1/installations/<installation_id>/settings")
    def update_installation_settings(installation_id: str):
        return authenticated(
            lambda active_service, auth_id: active_service.update_installation_settings(
                {**_json_body(), "installationId": installation_id or auth_id}
            )
        )

    @blueprint.post("/v1/entitlements/verify")
    def verify_entitlement():
        return authenticated(
            lambda active_service, auth_id: active_service.verify_entitlement(
                {**_json_body(), "installationId": auth_id}
            )
        )

    @blueprint.get("/v1/entitlements/status")
    def entitlement_status():
        return authenticated(
            lambda active_service, auth_id: active_service.entitlement_status(
                installation_id=auth_id,
            )
        )

    @blueprint.post("/v1/fcm-token")
    def upsert_fcm_token():
        return authenticated(
            lambda active_service, auth_id: active_service.upsert_fcm_token(
                {**_json_body(), "installationId": auth_id}
            )
        )

    @blueprint.delete("/v1/fcm-token")
    def delete_fcm_token():
        return authenticated(
            lambda active_service, auth_id: active_service.delete_fcm_token(
                {**_json_body(), "installationId": auth_id}
            )
        )

    @blueprint.put("/v1/alerts/<alert_id>")
    def upsert_alert(alert_id: str):
        return authenticated(
            lambda active_service, auth_id: active_service.upsert_alert(
                alert_id=alert_id,
                payload={**_json_body(), "installationId": auth_id},
            )
        )

    @blueprint.post("/v1/alerts/<alert_id>:pause")
    def pause_alert(alert_id: str):
        return authenticated(
            lambda active_service, auth_id: active_service.alert_action(
                installation_id=auth_id,
                alert_id=alert_id,
                action="pause",
                payload=_json_body(),
            )
        )

    @blueprint.post("/v1/alerts/<alert_id>:resume")
    def resume_alert(alert_id: str):
        return authenticated(
            lambda active_service, auth_id: active_service.alert_action(
                installation_id=auth_id,
                alert_id=alert_id,
                action="resume",
                payload=_json_body(),
            )
        )

    @blueprint.post("/v1/alerts/<alert_id>:rearm")
    def rearm_alert(alert_id: str):
        return authenticated(
            lambda active_service, auth_id: active_service.alert_action(
                installation_id=auth_id,
                alert_id=alert_id,
                action="rearm",
                payload=_json_body(),
            )
        )

    @blueprint.delete("/v1/alerts/<alert_id>")
    def delete_alert(alert_id: str):
        return authenticated(
            lambda active_service, auth_id: active_service.delete_alert(
                installation_id=auth_id,
                alert_id=alert_id,
                payload=_json_body(optional=True),
            )
        )

    @blueprint.delete("/v1/price-alerts")
    def delete_all_price_alerts():
        return authenticated(
            lambda active_service, auth_id: active_service.delete_all_alerts(
                {**_json_body(), "installationId": auth_id}
            )
        )

    @blueprint.get("/v1/alerts/sync")
    def sync_alerts():
        return authenticated(
            lambda active_service, auth_id: active_service.sync_alerts(
                installation_id=auth_id,
            )
        )

    @blueprint.get("/v1/events/sync")
    def sync_events():
        return authenticated(
            lambda active_service, auth_id: active_service.sync_events(
                installation_id=auth_id,
            )
        )

    return blueprint


def _json_body(*, optional: bool = False) -> dict[str, Any]:
    if optional and not request.data:
        return {}
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ContractError("malformed_request", "JSON object required")
    if len(request.data or b"") > 32_768:
        raise ContractError("malformed_request", "Request body too large")
    return body


def _credentials() -> tuple[str, str]:
    auth = request.headers.get("Authorization", "").strip()
    if auth.startswith("BullionovaInstallation "):
        value = auth.removeprefix("BullionovaInstallation ").strip()
        installation_id, separator, secret = value.partition(":")
        if separator and installation_id and secret:
            return installation_id, secret
    installation_id = request.headers.get("X-Installation-Id", "").strip()
    secret = request.headers.get("X-Installation-Secret", "").strip()
    if installation_id and secret:
        return installation_id, secret
    raise ContractError("unauthorised_installation", "Installation credentials required")


def _error(code: str, message: str, status: int):
    return jsonify(ApiErrorResponse(code=code, message=message).to_json()), status


def _status_for_code(code: str) -> int:
    return {
        "unauthorised_installation": 401,
        "entitlement_required": 402,
        "entitlement_invalid": 403,
        "alert_limit_reached": 409,
        "conflict": 409,
        "rate_limited": 429,
        "service_unavailable": 503,
    }.get(code, 400)


def _server_time() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
