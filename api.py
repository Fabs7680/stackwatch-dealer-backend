from __future__ import annotations

import atexit
import json
import logging
import os
import signal
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from flask import Flask, jsonify, make_response, request
from flask_cors import CORS

from main import build_collector
from storage import load_snapshot


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except Exception:
        return default


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


LOG_LEVEL = os.getenv("DEALER_API_LOG_LEVEL", "INFO").upper()
HOST = os.getenv("DEALER_API_HOST", "0.0.0.0")
PORT = _env_int("DEALER_API_PORT", 8000)
ENABLE_CORS = _env_bool("DEALER_API_ENABLE_CORS", True)
STARTUP_COLLECT = _env_bool("DEALER_API_STARTUP_COLLECT", True)
BACKGROUND_REFRESH = _env_bool("DEALER_API_BACKGROUND_REFRESH", True)
REFRESH_INTERVAL_SECONDS = max(60, _env_int("DEALER_API_REFRESH_INTERVAL_SECONDS", 43200))
REQUEST_REFRESH_TOKEN = os.getenv("DEALER_API_REFRESH_TOKEN", "").strip()
CORS_ORIGINS = os.getenv("DEALER_API_CORS_ORIGINS", "*").strip()
METALS_API_KEY = os.getenv("METALS_API_KEY", "").strip()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("dealer_api")

app = Flask(__name__)

if ENABLE_CORS:
    if CORS_ORIGINS == "*" or not CORS_ORIGINS:
        CORS(app)
    else:
        origins = [item.strip() for item in CORS_ORIGINS.split(",") if item.strip()]
        CORS(app, resources={r"/*": {"origins": origins}})

collector = build_collector()
collector_lock = threading.RLock()
refresh_stop_event = threading.Event()
refresh_thread: threading.Thread | None = None
last_refresh_started_at: str | None = None
last_refresh_completed_at: str | None = None
last_refresh_error: str | None = None

metals_cache: dict[str, Any] | None = None
metals_updated_at: str | None = None
metals_error: str | None = None


def _safe_load_snapshot():
    return load_snapshot()


def _http_get_json(url: str) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "StackWatch-Dealer-Backend/1.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=12) as response:
        raw = response.read().decode("utf-8")
        return json.loads(raw)


def _fetch_usd_aud() -> float:
    data = _http_get_json("https://open.er-api.com/v6/latest/USD")
    rates = data.get("rates") or {}
    aud = rates.get("AUD")
    if isinstance(aud, (int, float)) and aud > 0:
        return float(aud)
    raise RuntimeError("USD/AUD rate unavailable")


def _fetch_metals_fluctuation_pct(symbols: str) -> dict[str, float]:
    if not METALS_API_KEY:
        return {}

    end_date = datetime.now(timezone.utc) - timedelta(days=1)
    start_date = end_date - timedelta(days=1)

    url = (
        "https://metals-api.com/api/fluctuation"
        f"?access_key={METALS_API_KEY}"
        "&base=USD"
        f"&start_date={start_date.date().isoformat()}"
        f"&end_date={end_date.date().isoformat()}"
        f"&symbols={symbols}"
    )

    try:
        data = _http_get_json(url)
        rates = data.get("rates") or {}
    except Exception:
        return {}

    out: dict[str, float] = {}
    for symbol in ["XAU", "XAG", "XPT", "XPD"]:
        symbol_data = rates.get(symbol) or {}
        value = symbol_data.get("change_pct")
        if isinstance(value, (int, float)):
            out[symbol] = float(value)
        else:
            out[symbol] = 0.0
    return out


def _build_metals_payload() -> dict[str, Any]:
    if not METALS_API_KEY:
        raise RuntimeError("METALS_API_KEY is not configured")

    latest_url = (
        "https://metals-api.com/api/latest"
        f"?access_key={METALS_API_KEY}"
        "&base=USD"
        "&symbols=XAU,XAG,XPT,XPD"
    )

    latest = _http_get_json(latest_url)
    rates = latest.get("rates") or {}

    xau = float(rates.get("XAU") or 0.0)
    xag = float(rates.get("XAG") or 0.0)
    xpt = float(rates.get("XPT") or 0.0)
    xpd = float(rates.get("XPD") or 0.0)

    if xau <= 0 or xag <= 0:
        raise RuntimeError("Metals API returned invalid XAU/XAG rates")

    usd_aud = _fetch_usd_aud()
    change_map = _fetch_metals_fluctuation_pct("XAU,XAG,XPT,XPD")

    def q(price: float, change: float) -> dict[str, float]:
        return {
            "price": price,
            "change24hPct": change,
        }

    payload: dict[str, Any] = {
        "gold": q((1.0 / xau) * usd_aud, change_map.get("XAU", 0.0)),
        "silver": q((1.0 / xag) * usd_aud, change_map.get("XAG", 0.0)),
        "fetchedAt": _utc_now_iso(),
        "source": "metals-api-backend-cache",
    }

    if xpt > 0:
        payload["platinum"] = q((1.0 / xpt) * usd_aud, change_map.get("XPT", 0.0))
    if xpd > 0:
        payload["palladium"] = q((1.0 / xpd) * usd_aud, change_map.get("XPD", 0.0))

    return payload


def _refresh_metals_cache(reason: str) -> None:
    global metals_cache, metals_updated_at, metals_error

    logger.info("Refreshing metals cache. reason=%s", reason)

    try:
        payload = _build_metals_payload()
        metals_cache = payload
        metals_updated_at = payload.get("fetchedAt")
        metals_error = None
        logger.info("Metals cache refreshed. reason=%s fetched_at=%s", reason, metals_updated_at)
    except Exception as exc:
        metals_error = str(exc)
        logger.exception("Metals cache refresh failed. reason=%s error=%s", reason, exc)
        if metals_cache is None:
            raise


def _snapshot_age_seconds(snapshot: Any) -> float | None:
    updated_at = getattr(snapshot, "updated_at", None)
    dt = _parse_iso_datetime(updated_at)
    if dt is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())


def _run_collect(reason: str) -> Any:
    global last_refresh_started_at, last_refresh_completed_at, last_refresh_error

    with collector_lock:
        last_refresh_started_at = _utc_now_iso()
        last_refresh_error = None
        logger.info("Starting dealer collection. reason=%s", reason)

        try:
            snapshot = collector.collect_now()
            last_refresh_completed_at = _utc_now_iso()
            logger.info(
                "Dealer collection complete. reason=%s updated_at=%s",
                reason,
                getattr(snapshot, "updated_at", None),
            )
            return snapshot
        except Exception as exc:
            last_refresh_completed_at = _utc_now_iso()
            last_refresh_error = str(exc)
            logger.exception("Dealer collection failed. reason=%s error=%s", reason, exc)
            raise


def _ensure_snapshot_exists() -> None:
    try:
        snapshot = _safe_load_snapshot()
        dealers = getattr(snapshot, "dealers", None)
        if dealers:
            logger.info("Existing snapshot found on startup.")
        else:
            if STARTUP_COLLECT:
                try:
                    _run_collect("startup")
                except Exception as exc:
                    logger.warning("Initial collect failed: %s", exc)
    except Exception as exc:
        logger.warning("Snapshot load failed during startup check: %s", exc)
        if STARTUP_COLLECT:
            try:
                _run_collect("startup")
            except Exception as inner_exc:
                logger.warning("Initial collect failed: %s", inner_exc)

    try:
        _refresh_metals_cache("startup")
    except Exception as exc:
        logger.warning("Initial metals refresh failed: %s", exc)

def _background_refresh_loop() -> None:
    logger.info(
        "Background refresh loop started. interval_seconds=%s",
        REFRESH_INTERVAL_SECONDS,
    )

    while not refresh_stop_event.wait(REFRESH_INTERVAL_SECONDS):
        try:
            _run_collect("background")
        except Exception:
            # already logged in _run_collect
            pass

        try:
            _refresh_metals_cache("background")
        except Exception:
            # already logged in _refresh_metals_cache
            pass

    logger.info("Background refresh loop stopped.")


def _start_background_refresh_if_needed() -> None:
    global refresh_thread

    if not BACKGROUND_REFRESH:
        logger.info("Background refresh loop disabled.")
        return

    if refresh_thread is not None and refresh_thread.is_alive():
        return

    refresh_thread = threading.Thread(
        target=_background_refresh_loop,
        name="dealer-api-refresh-loop",
        daemon=True,
    )
    refresh_thread.start()


def _stop_background_refresh(*_: object) -> None:
    refresh_stop_event.set()

    global refresh_thread
    if refresh_thread is not None and refresh_thread.is_alive():
        refresh_thread.join(timeout=2.0)


@atexit.register
def _cleanup() -> None:
    _stop_background_refresh()


try:
    signal.signal(signal.SIGTERM, _stop_background_refresh)
except Exception:
    pass

# Let Ctrl+C use Python's default KeyboardInterrupt handling.


@app.after_request
def _apply_common_headers(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    response.headers["X-Dealer-API"] = "StackWatch"
    return response


@app.errorhandler(Exception)
def _handle_unexpected_error(exc: Exception):
    logger.exception("Unhandled API error: %s", exc)
    return (
        jsonify(
            {
                "ok": False,
                "error": "internal_server_error",
                "message": str(exc),
            }
        ),
        500,
    )


@app.get("/health")
def health():
    try:
        snapshot = _safe_load_snapshot()
        snapshot_dict = snapshot.to_dict()
        snapshot_age = _snapshot_age_seconds(snapshot)
        dealer_count = len(snapshot_dict.get("dealers", {}))

        return jsonify(
            {
                "ok": True,
                "service": "dealer_api",
                "utc_now": _utc_now_iso(),
                "background_refresh": BACKGROUND_REFRESH,
                "refresh_interval_seconds": REFRESH_INTERVAL_SECONDS,
                "last_refresh_started_at": last_refresh_started_at,
                "last_refresh_completed_at": last_refresh_completed_at,
                "last_refresh_error": last_refresh_error,
                "snapshot_updated_at": snapshot_dict.get("updated_at"),
                "snapshot_age_seconds": snapshot_age,
                "dealer_count": dealer_count,
            }
        )
    except Exception as exc:
        logger.exception("Health check failed: %s", exc)
        return (
            jsonify(
                {
                    "ok": False,
                    "service": "dealer_api",
                    "utc_now": _utc_now_iso(),
                    "error": "health_check_failed",
                    "message": str(exc),
                    "last_refresh_started_at": last_refresh_started_at,
                    "last_refresh_completed_at": last_refresh_completed_at,
                    "last_refresh_error": last_refresh_error,
                }
            ),
            503,
        )


@app.get("/prices")
def prices():
    try:
        snapshot = _safe_load_snapshot()
        payload = snapshot.to_dict()
        payload["spot"] = metals_cache
        payload["spot_updated_at"] = metals_updated_at
        payload["spot_error"] = metals_error

        response = make_response(jsonify(payload), 200)
        response.headers["X-Snapshot-Updated-At"] = str(payload.get("updated_at", ""))
        response.headers["X-Spot-Updated-At"] = str(metals_updated_at or "")
        return response
    except Exception as exc:
        logger.exception("Failed to load prices snapshot: %s", exc)
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "snapshot_unavailable",
                    "message": str(exc),
                    "last_refresh_started_at": last_refresh_started_at,
                    "last_refresh_completed_at": last_refresh_completed_at,
                    "last_refresh_error": last_refresh_error,
                    "spot_updated_at": metals_updated_at,
                    "spot_error": metals_error,
                }
            ),
            503,
        )

@app.post("/refresh")
def refresh():
    if REQUEST_REFRESH_TOKEN:
        supplied = request.headers.get("X-Refresh-Token", "").strip()
        if supplied != REQUEST_REFRESH_TOKEN:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "unauthorized",
                        "message": "Invalid refresh token.",
                    }
                ),
                401,
            )

    try:
        snapshot = _run_collect("manual_refresh")
        try:
            _refresh_metals_cache("manual_refresh")
        except Exception:
            pass

        payload = snapshot.to_dict()
        payload["spot"] = metals_cache
        payload["spot_updated_at"] = metals_updated_at
        payload["spot_error"] = metals_error
        return jsonify(payload)
    except Exception as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "refresh_failed",
                    "message": str(exc),
                    "last_refresh_started_at": last_refresh_started_at,
                    "last_refresh_completed_at": last_refresh_completed_at,
                    "last_refresh_error": last_refresh_error,
                    "spot_updated_at": metals_updated_at,
                    "spot_error": metals_error,
                }
            ),
            503,
        )

_ensure_snapshot_exists()
_start_background_refresh_if_needed()

if __name__ == "__main__":
    logger.info(
        "Starting dealer API. host=%s port=%s startup_collect=%s background_refresh=%s interval=%s",
        HOST,
        PORT,
        STARTUP_COLLECT,
        BACKGROUND_REFRESH,
        REFRESH_INTERVAL_SECONDS,
    )
    try:
        app.run(host=HOST, port=PORT, debug=False, threaded=True)
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received. Shutting down dealer API.")
        _stop_background_refresh()