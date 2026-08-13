from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta


TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    return False


def env_int(name: str, default: int, minimum: int | None = None) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        value = default
    else:
        try:
            value = int(raw.strip())
        except ValueError:
            value = default
    if minimum is not None:
        return max(minimum, value)
    return value


def env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class PriceAlertsServerConfig:
    environment: str = "production"
    enabled: bool = False
    worker_enabled: bool = False
    database_url: str = ""
    token_encryption_keys: str = ""
    token_hash_key: str = ""
    metals_api_key: str = ""
    metals_poll_interval_seconds: int = 600
    metals_plan_limit: int = 5000
    metals_application_hard_limit: int = 4800
    metals_warning_threshold: int = 4500
    metals_billing_cycle_anchor_day: int = 1
    spot_freshness_seconds: int = 2100
    fx_freshness_seconds: int = 90000
    fx_provider_enabled: bool = False
    fcm_enabled: bool = False
    firebase_credentials_file: str = ""
    play_verification_enabled: bool = False
    google_play_credentials_file: str = ""
    package_id: str = "com.northstack.stackwatch"
    product_id: str = "stackwatch_pro"
    max_resumable_alerts: int = 50
    max_visible_trigger_events: int = 100
    worker_batch_size: int = 100
    public_backend_base_url: str = "https://stackwatch-dealer-backend.onrender.com"
    allow_test_entitlements: bool = False
    allow_synthetic_quotes: bool = False
    max_test_entitlement_ttl_hours: int = 24
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "PriceAlertsServerConfig":
        return cls(
            environment=env_str("BULLIONOVA_ENVIRONMENT", "production").lower(),
            enabled=env_bool("BULLIONOVA_PRICE_ALERTS_SERVER_ENABLED", False),
            worker_enabled=env_bool("BULLIONOVA_PRICE_ALERTS_WORKER_ENABLED", False),
            database_url=env_str("DATABASE_URL"),
            token_encryption_keys=env_str("PRICE_ALERTS_TOKEN_ENCRYPTION_KEYS"),
            token_hash_key=env_str("PRICE_ALERTS_TOKEN_HASH_KEY"),
            metals_api_key=env_str("METALS_API_KEY"),
            metals_poll_interval_seconds=env_int(
                "PRICE_ALERTS_METALS_POLL_INTERVAL_SECONDS",
                600,
                minimum=600,
            ),
            metals_plan_limit=env_int("PRICE_ALERTS_METALS_PLAN_LIMIT", 5000, minimum=1),
            metals_application_hard_limit=env_int(
                "PRICE_ALERTS_METALS_APPLICATION_HARD_LIMIT",
                4800,
                minimum=1,
            ),
            metals_warning_threshold=env_int(
                "PRICE_ALERTS_METALS_WARNING_THRESHOLD",
                4500,
                minimum=1,
            ),
            metals_billing_cycle_anchor_day=env_int(
                "PRICE_ALERTS_METALS_BILLING_CYCLE_ANCHOR_DAY",
                1,
                minimum=1,
            ),
            spot_freshness_seconds=env_int(
                "PRICE_ALERTS_SPOT_FRESHNESS_SECONDS",
                2100,
                minimum=600,
            ),
            fx_freshness_seconds=env_int(
                "PRICE_ALERTS_FX_FRESHNESS_SECONDS",
                90000,
                minimum=3600,
            ),
            fx_provider_enabled=env_bool("PRICE_ALERTS_FX_PROVIDER_ENABLED", False),
            fcm_enabled=env_bool("PRICE_ALERTS_FCM_ENABLED", False),
            firebase_credentials_file=env_str("PRICE_ALERTS_FIREBASE_CREDENTIALS_FILE"),
            play_verification_enabled=env_bool(
                "PRICE_ALERTS_PLAY_VERIFICATION_ENABLED",
                False,
            ),
            google_play_credentials_file=env_str("PRICE_ALERTS_GOOGLE_PLAY_CREDENTIALS_FILE"),
            package_id=env_str("PRICE_ALERTS_ANDROID_PACKAGE_ID", "com.northstack.stackwatch"),
            product_id=env_str("PRICE_ALERTS_PLAY_PRODUCT_ID", "stackwatch_pro"),
            max_resumable_alerts=env_int("PRICE_ALERTS_MAX_RESUMABLE_ALERTS", 50, minimum=1),
            max_visible_trigger_events=env_int(
                "PRICE_ALERTS_MAX_VISIBLE_TRIGGER_EVENTS",
                100,
                minimum=1,
            ),
            worker_batch_size=env_int("PRICE_ALERTS_WORKER_BATCH_SIZE", 100, minimum=1),
            public_backend_base_url=env_str(
                "PRICE_ALERTS_PUBLIC_BACKEND_BASE_URL",
                "https://stackwatch-dealer-backend.onrender.com",
            ),
            allow_test_entitlements=env_bool(
                "BULLIONOVA_PRICE_ALERTS_ALLOW_TEST_ENTITLEMENTS",
                False,
            ),
            allow_synthetic_quotes=env_bool(
                "BULLIONOVA_PRICE_ALERTS_ALLOW_SYNTHETIC_QUOTES",
                False,
            ),
            max_test_entitlement_ttl_hours=env_int(
                "PRICE_ALERTS_MAX_TEST_ENTITLEMENT_TTL_HOURS",
                24,
                minimum=1,
            ),
            log_level=env_str("PRICE_ALERTS_LOG_LEVEL", "INFO"),
        )

    @property
    def spot_freshness_window(self) -> timedelta:
        return timedelta(seconds=self.spot_freshness_seconds)

    @property
    def fx_freshness_window(self) -> timedelta:
        return timedelta(seconds=self.fx_freshness_seconds)

    def readiness_state(self) -> str:
        if not self.enabled:
            return "disabled"
        missing = []
        if not self.database_url:
            missing.append("database")
        if not self.token_encryption_keys or not self.token_hash_key:
            missing.append("token_protection")
        if not self.metals_api_key:
            missing.append("metals_provider")
        if missing:
            return "configuring"
        if self.metals_application_hard_limit > self.metals_plan_limit:
            return "degraded"
        return "ready"

    def safe_health_payload(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "workerEnabled": self.worker_enabled,
            "environment": self.environment,
            "state": self.readiness_state(),
            "databaseConfigured": bool(self.database_url),
            "tokenProtectionConfigured": bool(
                self.token_encryption_keys and self.token_hash_key
            ),
            "metalsProviderConfigured": bool(self.metals_api_key),
            "fxProviderEnabled": self.fx_provider_enabled,
            "fcmConfigured": self.fcm_enabled,
            "firebaseCredentialsFileConfigured": bool(self.firebase_credentials_file),
            "playVerifierConfigured": self.play_verification_enabled and bool(
                self.google_play_credentials_file
            ),
            "metalsPollIntervalSeconds": self.metals_poll_interval_seconds,
            "metalsPlanLimit": self.metals_plan_limit,
            "metalsApplicationHardLimit": self.metals_application_hard_limit,
            "metalsWarningThreshold": self.metals_warning_threshold,
            "packageId": self.package_id,
            "productId": self.product_id,
            "testEntitlementsEnabled": self.allow_test_entitlements
            and self.environment == "staging",
            "syntheticQuotesEnabled": self.allow_synthetic_quotes
            and self.environment == "staging",
        }
