from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from models import MediaPlanRequest, EditablePlanLine


class PlanStore:
    def __init__(self, path: str = "media_plans.db"):
        self.path = Path(path)
        self._init()

    def _connect(self):
        return sqlite3.connect(self.path)

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS media_plans (
                  plan_id TEXT PRIMARY KEY,
                  created_at TEXT NOT NULL,
                  brand TEXT NOT NULL,
                  comcat TEXT NOT NULL,
                  country TEXT NOT NULL,
                  start_date TEXT NOT NULL,
                  end_date TEXT NOT NULL,
                  budget REAL NOT NULL,
                  request_json TEXT NOT NULL,
                  response_json TEXT NOT NULL
                )
                """
            )

    def save(self, req: MediaPlanRequest, rows: list[EditablePlanLine], summary: dict, diagnostics: dict, plan_id: str | None = None) -> str:
        plan_id = plan_id or str(uuid4())
        response = {
            "rows": [row.model_dump(mode="json", by_alias=True) for row in rows],
            "summary": summary,
            "diagnostics": diagnostics,
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO media_plans (
                  plan_id, created_at, brand, comcat, country, start_date, end_date,
                  budget, request_json, response_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan_id,
                    datetime.now(timezone.utc).isoformat(),
                    req.brand,
                    ", ".join(req.comcats),
                    ", ".join(req.countries),
                    req.start_date.isoformat(),
                    req.end_date.isoformat(),
                    req.budget,
                    req.model_dump_json(by_alias=True),
                    json.dumps(response),
                ),
            )
        return plan_id

    def get(self, plan_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT response_json FROM media_plans WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
        if not row:
            return None
        return json.loads(row[0])
