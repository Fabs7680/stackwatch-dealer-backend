from __future__ import annotations

import argparse
import hashlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .contracts import ContractError


BACKEND_DIR = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = BACKEND_DIR / "migrations"


@dataclass(frozen=True)
class Migration:
    version: str
    path: Path
    sha256: str
    sql: str


def load_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    migrations: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        migrations.append(
            Migration(
                version=path.stem,
                path=path,
                sha256=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
                sql=sql,
            )
        )
    return migrations


class MigrationRunner:
    def __init__(self, *, database_url: str) -> None:
        if not database_url.strip():
            raise ContractError("service_unavailable", "DATABASE_URL is not configured")
        self._database_url = database_url

    def status(self) -> dict[str, object]:
        migrations = load_migrations()
        return {
            "configured": True,
            "migrationCount": len(migrations),
            "latestVersion": migrations[-1].version if migrations else None,
        }

    def check(self) -> dict[str, object]:
        migrations = load_migrations()
        self._with_connection(lambda conn: self._ensure_table(conn))
        applied = self._with_connection(self._applied_versions)
        pending = [item.version for item in migrations if item.version not in applied]
        return {
            "configured": True,
            "current": not pending,
            "applied": sorted(applied),
            "pending": pending,
        }

    def apply(self) -> dict[str, object]:
        migrations = load_migrations()

        def run(conn):
            self._ensure_table(conn)
            applied = self._applied_versions(conn)
            applied_now = []
            with conn.transaction():
                for migration in migrations:
                    if migration.version in applied:
                        continue
                    conn.execute(migration.sql)
                    conn.execute(
                        """
                        INSERT INTO price_alert_schema_migrations(version, sha256)
                        VALUES (%s, %s)
                        """,
                        (migration.version, migration.sha256),
                    )
                    applied_now.append(migration.version)
            return applied_now

        applied_now = self._with_connection(run)
        return {"configured": True, "appliedNow": applied_now}

    def _with_connection(self, callback):
        try:
            import psycopg
        except Exception as exc:
            raise ContractError("service_unavailable", "psycopg dependency unavailable") from exc
        with psycopg.connect(self._database_url) as conn:
            return callback(conn)

    def _ensure_table(self, conn) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS price_alert_schema_migrations (
                version TEXT PRIMARY KEY,
                sha256 TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )

    def _applied_versions(self, conn) -> set[str]:
        cursor = conn.execute("SELECT version FROM price_alert_schema_migrations")
        return {row[0] for row in cursor.fetchall()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bullionova Price Alerts migrations")
    parser.add_argument("--check", action="store_true", help="Check pending migrations")
    parser.add_argument("--apply", action="store_true", help="Apply pending migrations")
    parser.add_argument("--status", action="store_true", help="Show local migration metadata")
    args = parser.parse_args(argv)
    database_url = os.getenv("DATABASE_URL", "")
    if args.status or not (args.apply or args.check):
        migrations = load_migrations()
        print(
            {
                "configured": bool(database_url.strip()),
                "migrationCount": len(migrations),
                "latestVersion": migrations[-1].version if migrations else None,
            }
        )
        return 0
    try:
        runner = MigrationRunner(database_url=database_url)
        if args.apply:
            print(runner.apply())
            return 0
        result = runner.check()
        print(result)
        return 0 if result["current"] else 2
    except ContractError as exc:
        print(
            {
                "configured": False,
                "ok": False,
                "code": exc.code,
                "message": exc.message,
            },
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
