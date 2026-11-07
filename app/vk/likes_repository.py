"""Immutable, complete collection snapshots for VK likes reports."""

import json
from datetime import datetime, timezone
from typing import Any

from app.storage.database import Database


class VKLikesRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def save_report(self, report: dict[str, Any]) -> None:
        connection = self.database.require_connection()
        await connection.execute(
            "INSERT INTO vk_like_reports(group_id, captured_at, payload_json) VALUES (?, ?, ?)",
            (
                report["group_id"],
                datetime.now(timezone.utc).isoformat(),  # noqa: UP017 - Python 3.10
                json.dumps(report, ensure_ascii=False),
            ),
        )
        await connection.commit()

    async def latest_report(self) -> dict[str, Any] | None:
        async with self.database.require_connection().execute(
            "SELECT payload_json FROM vk_like_reports ORDER BY id DESC LIMIT 1",
        ) as cursor:
            row = await cursor.fetchone()
        return json.loads(row["payload_json"]) if row else None
