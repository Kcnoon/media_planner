from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
import json
import re
from uuid import uuid4

from google.api_core.exceptions import NotFound
from google.cloud import bigquery

from config import Settings
from models import EditablePlanLine, MediaPlanRequest


RUN_HEADERS = [
    "plan_code",
    "created_at",
    "brand",
    "brand_tag",
    "sub_brands",
    "comcats",
    "countries",
    "marketplace",
    "start_date",
    "end_date",
    "budget_usd",
    "discount_pct",
    "currency",
    "budget_locked",
    "le_code",
    "notes",
    "objective",
    "allocated_usd",
    "remaining_usd",
    "estimated_views",
    "line_count",
    "plan_link",
    "request_json",
    "diagnostics_json",
]

LINE_HEADERS = [
    "plan_code",
    "line_id",
    "from",
    "to",
    "country",
    "page",
    "asset",
    "slot_name",
    "days",
    "buy_type",
    "rate",
    "gross_cpm",
    "net_cpm",
    "views",
    "cost",
    "gross_amount",
    "net_amount",
    "discount_pct",
    "phase",
    "brand",
    "marketplace",
    "category",
    "zone",
    "dimension",
    "stype",
    "slot_code",
    "score",
    "available_views",
    "historical_ctr",
    "historical_roas",
    "historical_cpm",
    "note",
    "manual",
    "locked",
]

RUN_SCHEMA = [
    bigquery.SchemaField("plan_code", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("created_at", "TIMESTAMP"),
    bigquery.SchemaField("brand", "STRING"),
    bigquery.SchemaField("brand_tag", "STRING"),
    bigquery.SchemaField("sub_brands", "STRING"),
    bigquery.SchemaField("comcats", "STRING"),
    bigquery.SchemaField("countries", "STRING"),
    bigquery.SchemaField("marketplace", "STRING"),
    bigquery.SchemaField("start_date", "DATE"),
    bigquery.SchemaField("end_date", "DATE"),
    bigquery.SchemaField("budget_usd", "FLOAT"),
    bigquery.SchemaField("discount_pct", "FLOAT"),
    bigquery.SchemaField("currency", "STRING"),
    bigquery.SchemaField("budget_locked", "BOOL"),
    bigquery.SchemaField("le_code", "STRING"),
    bigquery.SchemaField("notes", "STRING"),
    bigquery.SchemaField("objective", "STRING"),
    bigquery.SchemaField("allocated_usd", "FLOAT"),
    bigquery.SchemaField("remaining_usd", "FLOAT"),
    bigquery.SchemaField("estimated_views", "INT64"),
    bigquery.SchemaField("line_count", "INT64"),
    bigquery.SchemaField("plan_link", "STRING"),
    bigquery.SchemaField("request_json", "STRING"),
    bigquery.SchemaField("diagnostics_json", "STRING"),
]

LINE_SCHEMA = [
    bigquery.SchemaField("plan_code", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("line_id", "INT64"),
    bigquery.SchemaField("from", "DATE"),
    bigquery.SchemaField("to", "DATE"),
    bigquery.SchemaField("country", "STRING"),
    bigquery.SchemaField("page", "STRING"),
    bigquery.SchemaField("asset", "STRING"),
    bigquery.SchemaField("slot_name", "STRING"),
    bigquery.SchemaField("days", "INT64"),
    bigquery.SchemaField("buy_type", "STRING"),
    bigquery.SchemaField("rate", "FLOAT"),
    bigquery.SchemaField("gross_cpm", "FLOAT"),
    bigquery.SchemaField("net_cpm", "FLOAT"),
    bigquery.SchemaField("views", "INT64"),
    bigquery.SchemaField("cost", "FLOAT"),
    bigquery.SchemaField("gross_amount", "FLOAT"),
    bigquery.SchemaField("net_amount", "FLOAT"),
    bigquery.SchemaField("discount_pct", "FLOAT"),
    bigquery.SchemaField("phase", "STRING"),
    bigquery.SchemaField("brand", "STRING"),
    bigquery.SchemaField("marketplace", "STRING"),
    bigquery.SchemaField("category", "STRING"),
    bigquery.SchemaField("zone", "STRING"),
    bigquery.SchemaField("dimension", "STRING"),
    bigquery.SchemaField("stype", "STRING"),
    bigquery.SchemaField("slot_code", "STRING"),
    bigquery.SchemaField("score", "FLOAT"),
    bigquery.SchemaField("available_views", "INT64"),
    bigquery.SchemaField("historical_ctr", "FLOAT"),
    bigquery.SchemaField("historical_roas", "FLOAT"),
    bigquery.SchemaField("historical_cpm", "FLOAT"),
    bigquery.SchemaField("note", "STRING"),
    bigquery.SchemaField("manual", "BOOL"),
    bigquery.SchemaField("locked", "BOOL"),
]


def norm(value) -> str:
    return str(value or "").strip().lower()


def country_values(countries: list[str]) -> set[str]:
    aliases = {
        "ae": {"ae", "uae", "united arab emirates"},
        "uae": {"ae", "uae", "united arab emirates"},
        "sa": {"sa", "ksa", "saudi", "saudi arabia"},
        "ksa": {"sa", "ksa", "saudi", "saudi arabia"},
        "eg": {"eg", "egypt"},
        "egypt": {"eg", "egypt"},
    }
    values = set()
    for country in countries:
        value = norm(country)
        values |= aliases.get(value, {value})
    return values


def get_first(row: dict, *names: str):
    lowered = {norm(k): v for k, v in row.items()}
    for name in names:
        if norm(name) in lowered:
            return lowered[norm(name)]
    return None


def infer_country(row: dict) -> str:
    explicit = norm(get_first(row, "country"))
    if explicit:
        if explicit in {"uae", "united arab emirates"}:
            return "ae"
        if explicit in {"ksa", "saudi", "saudi arabia"}:
            return "sa"
        if explicit == "egypt":
            return "eg"
        return explicit

    slot = norm(get_first(row, "slot_code", "slot"))
    if slot.startswith("ae_") or "_ae_" in slot or slot.startswith("noon_ae_"):
        return "ae"
    if slot.startswith("sa_") or "_sa_" in slot or slot.startswith("noon_sa_"):
        return "sa"
    if slot.startswith("eg_") or "_eg_" in slot or slot.startswith("noon_eg_"):
        return "eg"
    return ""


def parse_date(value) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, (int, float)) and value:
        return (datetime(1899, 12, 30) + timedelta(days=float(value))).date()
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def parse_number(value) -> float:
    text = str(value or "").replace(",", "").strip()
    if text in {"", "-"}:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def text_tokens(value: str) -> set[str]:
    return {
        token
        for token in re.split(r"[^a-z0-9]+", norm(value))
        if token and len(token) > 2
    }


def parse_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return norm(value) in {"1", "true", "yes", "y"}


class BigQueryRepository:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = bigquery.Client(project=settings.bigquery_project or None)
        self._schema_cache: dict[str, dict[str, str]] = {}

    def _table_schema(self, table_id: str) -> dict[str, str]:
        cached = self._schema_cache.get(table_id)
        if cached is not None:
            return cached
        table = self.client.get_table(table_id)
        schema = {norm(field.name): field.name for field in table.schema}
        self._schema_cache[table_id] = schema
        return schema

    def _field_name(self, table_id: str, *aliases: str) -> str | None:
        schema = self._table_schema(table_id)
        for alias in aliases:
            match = schema.get(norm(alias))
            if match:
                return match
        return None

    def _query_records(self, sql: str, params: list[bigquery.ScalarQueryParameter] | None = None) -> list[dict]:
        job_config = bigquery.QueryJobConfig(query_parameters=params or [])
        rows = self.client.query(
            sql,
            job_config=job_config,
            location=self.settings.bigquery_location or None,
        ).result()
        return [dict(row.items()) for row in rows]

    def _table_records_for_window(
        self,
        table_id: str,
        start: date,
        end: date,
        *date_aliases: str,
    ) -> list[dict]:
        date_field = self._field_name(table_id, *date_aliases)
        if not date_field:
            return self._query_records(f"SELECT * FROM `{table_id}`")
        sql = f"""
            SELECT *
            FROM `{table_id}`
            WHERE DATE(`{date_field}`) BETWEEN @start_date AND @end_date
        """
        return self._query_records(
            sql,
            [
                bigquery.ScalarQueryParameter("start_date", "DATE", start.isoformat()),
                bigquery.ScalarQueryParameter("end_date", "DATE", end.isoformat()),
            ],
        )

    def _ensure_table(self, table_id: str, schema: list[bigquery.SchemaField]) -> None:
        try:
            table = self.client.get_table(table_id)
        except NotFound:
            table = bigquery.Table(table_id, schema=schema)
            self.client.create_table(table)
            self._schema_cache.pop(table_id, None)
            return

        existing = {field.name: field for field in table.schema}
        missing = [field for field in schema if field.name not in existing]
        if missing:
            table.schema = [*table.schema, *missing]
            self.client.update_table(table, ["schema"])
            self._schema_cache.pop(table_id, None)

    def _fetch_rate_card_map(
        self,
        start: date,
        end: date,
    ) -> tuple[dict[tuple[str, str], float], dict[str, float], dict[tuple[str, str], dict[str, float]], dict[str, dict[str, float]]]:
        rows = self._table_records_for_window(
            self.settings.slot_rate_card_table,
            start,
            end,
            "date",
            "dt",
        )
        by_country_slot: dict[tuple[str, str], list[float]] = defaultdict(list)
        by_slot: dict[str, list[float]] = defaultdict(list)
        schedule_by_country_slot: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
        schedule_by_slot: dict[str, dict[str, float]] = defaultdict(dict)
        for row in rows:
            slot_code = str(get_first(row, "slot_code", "slot") or "").strip()
            if not slot_code:
                continue
            country = infer_country(row)
            dt = parse_date(get_first(row, "date", "dt"))
            rate = parse_number(get_first(row, "rate_usd", "rate", "usd_rate", "cpm", "price"))
            if rate <= 0:
                continue
            by_slot[slot_code].append(rate)
            if dt:
                schedule_by_slot[slot_code][dt.isoformat()] = rate
            if country:
                by_country_slot[(country, slot_code)].append(rate)
                if dt:
                    schedule_by_country_slot[(country, slot_code)][dt.isoformat()] = rate
        averaged_country_slot = {
            key: round(sum(values) / len(values), 4)
            for key, values in by_country_slot.items()
            if values
        }
        averaged_slot = {
            key: round(sum(values) / len(values), 4)
            for key, values in by_slot.items()
            if values
        }
        return averaged_country_slot, averaged_slot, dict(schedule_by_country_slot), dict(schedule_by_slot)

    def fetch_slot_catalog(self, req: MediaPlanRequest | None = None) -> list[dict]:
        rows = self._query_records(f"SELECT * FROM `{self.settings.slot_data_table}`")
        rate_by_country_slot: dict[tuple[str, str], float] = {}
        rate_by_slot: dict[str, float] = {}
        schedule_by_country_slot: dict[tuple[str, str], dict[str, float]] = {}
        schedule_by_slot: dict[str, dict[str, float]] = {}
        if req is not None:
            rate_by_country_slot, rate_by_slot, schedule_by_country_slot, schedule_by_slot = self._fetch_rate_card_map(req.start_date, req.end_date)
        catalog = []
        for row in rows:
            slot_code = str(get_first(row, "slot_code", "slot") or "").strip()
            if not slot_code:
                continue
            country = infer_country(row)
            rate_schedule = schedule_by_country_slot.get((country, slot_code)) or schedule_by_slot.get(slot_code) or {}
            rate = (
                rate_by_country_slot.get((country, slot_code))
                or rate_by_slot.get(slot_code)
                or parse_number(get_first(row, "cpm", "rate", "gross_cpm", "price"))
            )
            catalog.append(
                {
                    "country": country,
                    "slot_code": slot_code,
                    "slot_name": str(get_first(row, "slot_name", "asset", "slot") or slot_code).strip(),
                    "page": str(get_first(row, "category", "page", "publisher") or "").strip(),
                    "category": str(get_first(row, "category", "page", "publisher") or "").strip(),
                    "zone": str(get_first(row, "zone") or "").strip(),
                    "dimension": str(get_first(row, "dimension", "dimensions", "size", "ad_size", "creative_dimension", "dimension information") or "").strip(),
                    "publisher": str(get_first(row, "publisher") or "").strip(),
                    "pricing_model": str(get_first(row, "pricing_model", "buy_type") or "cpm").strip(),
                    "rate": rate if rate > 0 else 10.0,
                    "rate_schedule": rate_schedule,
                }
            )
        return catalog

    def fetch_slot_meta(self, req: MediaPlanRequest | None = None) -> dict[tuple[str, str], dict]:
        return {(row["country"], row["slot_code"]): row for row in self.fetch_slot_catalog(req)}

    def fetch_historical_performance(self, req: MediaPlanRequest) -> list[dict]:
        start = req.start_date - timedelta(days=self.settings.historical_lookback_days)
        rows = self._table_records_for_window(
            self.settings.delivery_table,
            start,
            req.start_date - timedelta(days=1),
            "date",
            "dt",
        )
        comcats = {norm(c) for c in req.comcats if norm(c)}
        comcat_tokens = {token for value in comcats for token in text_tokens(value)}
        countries = country_values(req.countries)
        brand = norm(req.brand)
        sub_brands = {norm(value) for value in (req.sub_brands or []) if norm(value)}
        brand_names = {value for value in {brand, *sub_brands} if value}

        def aggregate(source_rows: list[dict], prefer_brand: bool) -> list[dict]:
            grouped: dict[tuple[str, str, str, str], dict] = {}
            active_dates: dict[tuple[str, str, str, str], set[date]] = defaultdict(set)
            for row in source_rows:
                row_country = infer_country(row)
                key = (
                    row_country,
                    str(get_first(row, "slot_code", "slot") or "").strip(),
                    str(get_first(row, "publisher", "page") or "").strip(),
                    str(get_first(row, "pricing_model", "buy_type", "buy type") or "").strip(),
                )
                if not key[1]:
                    continue
                target = grouped.setdefault(
                    key,
                    {
                        "country": row_country,
                        "slot_code": key[1],
                        "publisher": key[2],
                        "pricing_model": key[3],
                        "views": 0,
                        "clicks": 0,
                        "revenue": 0.0,
                        "spends": 0.0,
                        "active_days": 0,
                        "brand_specific": False,
                    },
                )
                target["views"] += int(parse_number(get_first(row, "views", "impressions")))
                target["clicks"] += int(parse_number(get_first(row, "clicks")))
                target["revenue"] += parse_number(get_first(row, "revenue"))
                target["spends"] += parse_number(get_first(row, "spends", "spend", "cost"))
                if prefer_brand and norm(get_first(row, "brand")) == brand:
                    target["brand_specific"] = True
                row_date = parse_date(get_first(row, "date", "dt"))
                if row_date:
                    active_dates[key].add(row_date)
            for key, target in grouped.items():
                target["active_days"] = max(len(active_dates[key]), 1)
            return list(grouped.values())

        def row_country_match(row: dict) -> bool:
            return infer_country(row) in countries

        def matches_exact_comcat(row: dict) -> bool:
            row_comcat = norm(get_first(row, "comcat", "comcat_code"))
            row_category = norm(get_first(row, "category"))
            return row_comcat in comcats or row_category in comcats

        def matches_similar_comcat(row: dict) -> bool:
            if matches_exact_comcat(row):
                return True
            haystacks = [
                norm(get_first(row, "comcat", "comcat_code")),
                norm(get_first(row, "category")),
                norm(get_first(row, "slot_name", "asset", "slot")),
                norm(get_first(row, "slot_code", "slot")),
            ]
            haystack_tokens = set().union(*(text_tokens(value) for value in haystacks if value))
            if comcat_tokens & haystack_tokens:
                return True
            for value in haystacks:
                if not value:
                    continue
                if any(comcat in value or value in comcat for comcat in comcats):
                    return True
            return False

        def matches_brand(row: dict) -> bool:
            row_brand = norm(get_first(row, "brand"))
            return row_brand in brand_names

        requested_brand_tag = req.brand_tag or self.infer_brand_tag(req)
        selected_country_rows = [row for row in rows if row_country_match(row)]
        all_brand_rows = [row for row in rows if matches_brand(row)]
        selected_brand_rows = [row for row in selected_country_rows if matches_brand(row)]
        brand_exact_country_rows = [row for row in selected_brand_rows if matches_exact_comcat(row)]
        brand_similar_country_rows = [row for row in selected_brand_rows if matches_similar_comcat(row)]
        brand_exact_global_rows = [row for row in all_brand_rows if matches_exact_comcat(row)]
        brand_similar_global_rows = [row for row in all_brand_rows if matches_similar_comcat(row)]
        exact_country_rows = [row for row in selected_country_rows if matches_exact_comcat(row)]
        similar_country_rows = [row for row in selected_country_rows if matches_similar_comcat(row)]
        exact_global_rows = [row for row in rows if matches_exact_comcat(row)]
        similar_global_rows = [row for row in rows if matches_similar_comcat(row)]

        if requested_brand_tag == "old":
            if brand_exact_country_rows:
                return aggregate(brand_exact_country_rows, prefer_brand=True)
            if brand_similar_country_rows:
                return aggregate(brand_similar_country_rows, prefer_brand=True)
            if brand_exact_global_rows:
                return aggregate(brand_exact_global_rows, prefer_brand=True)
            if brand_similar_global_rows:
                return aggregate(brand_similar_global_rows, prefer_brand=True)
            if exact_country_rows:
                return aggregate(exact_country_rows, prefer_brand=False)
            if similar_country_rows:
                return aggregate(similar_country_rows, prefer_brand=False)
            if exact_global_rows:
                return aggregate(exact_global_rows, prefer_brand=False)
            return aggregate(similar_global_rows, prefer_brand=False)

        if exact_country_rows:
            return aggregate(exact_country_rows, prefer_brand=False)
        if similar_country_rows:
            return aggregate(similar_country_rows, prefer_brand=False)
        if exact_global_rows:
            return aggregate(exact_global_rows, prefer_brand=False)
        return aggregate(similar_global_rows, prefer_brand=False)

    def infer_brand_tag(self, req: MediaPlanRequest) -> str:
        start = req.start_date - timedelta(days=self.settings.historical_lookback_days)
        rows = self._table_records_for_window(
            self.settings.delivery_table,
            start,
            req.start_date - timedelta(days=1),
            "date",
            "dt",
        )
        countries = country_values(req.countries)
        brand = norm(req.brand)
        sub_brands = {norm(value) for value in (req.sub_brands or []) if norm(value)}
        brand_names = {value for value in {brand, *sub_brands} if value}
        for row in rows:
            if infer_country(row) not in countries:
                continue
            if norm(get_first(row, "brand")) in brand_names:
                return "old"
        return "new"

    def fetch_inventory(self, req: MediaPlanRequest) -> list[dict]:
        forecast_rows = self._table_records_for_window(
            self.settings.forecast_table,
            req.start_date,
            req.end_date,
            "dt",
            "date",
        )
        booking_rows = self._table_records_for_window(
            self.settings.booking_table,
            req.start_date,
            req.end_date,
            "dt",
            "date",
        )
        countries = country_values(req.countries)

        booked_by_date_slot: dict[tuple[date, str, str], int] = defaultdict(int)
        for row in booking_rows:
            dt = parse_date(get_first(row, "dt", "date"))
            if not dt:
                continue
            row_country = infer_country(row)
            if row_country not in countries:
                continue
            slot = str(get_first(row, "slot_code", "slot") or "").strip()
            booked_by_date_slot[(dt, row_country, slot)] += int(parse_number(get_first(row, "booked_views", "delivered_views")))

        inventory: list[dict] = []
        for row in forecast_rows:
            dt = parse_date(get_first(row, "dt", "date"))
            if not dt:
                continue
            row_country = infer_country(row)
            if row_country not in countries:
                continue
            slot = str(get_first(row, "slot", "slot_code") or "").strip()
            forecast = int(parse_number(get_first(row, "slot_sessions", "forecast_views", "views")))
            booked = booked_by_date_slot.get((dt, row_country, slot), 0)
            inventory.append(
                {
                    "dt": dt,
                    "country": row_country,
                    "slot_code": slot,
                    "forecast_views": forecast,
                    "booked_views": booked,
                    "available_views": max(forecast - booked, 0),
                }
            )
        return inventory

    def list_comcats(self) -> list[str]:
        values = set()
        for table_id in (self.settings.delivery_table, self.settings.booking_table):
            field = self._field_name(table_id, "comcat", "comcat_code")
            if not field:
                continue
            rows = self._query_records(
                f"""
                SELECT DISTINCT CAST(`{field}` AS STRING) AS value
                FROM `{table_id}`
                WHERE `{field}` IS NOT NULL AND CAST(`{field}` AS STRING) != ''
                """
            )
            for row in rows:
                value = str(row.get("value") or "").strip()
                if value:
                    values.add(value)
        return sorted(values, key=str.lower)

    def save_plan(
        self,
        req: MediaPlanRequest,
        rows: list[EditablePlanLine],
        summary: dict,
        diagnostics: dict,
        plan_code: str | None = None,
    ) -> tuple[str, str]:
        self._ensure_table(self.settings.plan_runs_table, RUN_SCHEMA)
        self._ensure_table(self.settings.plan_lines_table, LINE_SCHEMA)

        plan_code = plan_code or f"MP-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6].upper()}"
        base_url = self.settings.public_app_url.strip().rstrip("/")
        plan_link = f"{base_url}/?plan={plan_code}&view=start" if base_url else ""

        delete_params = [bigquery.ScalarQueryParameter("plan_code", "STRING", plan_code)]
        self.client.query(
            f"DELETE FROM `{self.settings.plan_runs_table}` WHERE plan_code = @plan_code",
            job_config=bigquery.QueryJobConfig(query_parameters=delete_params),
            location=self.settings.bigquery_location or None,
        ).result()
        self.client.query(
            f"DELETE FROM `{self.settings.plan_lines_table}` WHERE plan_code = @plan_code",
            job_config=bigquery.QueryJobConfig(query_parameters=delete_params),
            location=self.settings.bigquery_location or None,
        ).result()

        run_row = {
            "plan_code": plan_code,
            "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "brand": req.brand,
            "brand_tag": req.brand_tag or "",
            "sub_brands": ", ".join(req.sub_brands or []),
            "comcats": ", ".join(req.comcats),
            "countries": ", ".join(req.countries),
            "marketplace": req.marketplace or "",
            "start_date": req.start_date.isoformat(),
            "end_date": req.end_date.isoformat(),
            "budget_usd": float(req.budget),
            "discount_pct": float(req.discount_pct),
            "currency": req.currency,
            "budget_locked": bool(req.budget_locked),
            "le_code": req.le_code or "",
            "notes": req.notes or "",
            "objective": req.objective,
            "allocated_usd": float(summary["allocated"]),
            "remaining_usd": float(summary["remaining"]),
            "estimated_views": int(summary["estimated_views"]),
            "line_count": int(summary["line_count"]),
            "plan_link": plan_link,
            "request_json": req.model_dump_json(by_alias=True),
            "diagnostics_json": json.dumps(diagnostics),
        }
        run_errors = self.client.insert_rows_json(self.settings.plan_runs_table, [run_row])
        if run_errors:
            raise RuntimeError(f"Failed to write media_plan_runs: {run_errors}")

        line_rows = [
            {
                "plan_code": plan_code,
                "line_id": int(row.id),
                "from": row.from_date.isoformat(),
                "to": row.to_date.isoformat(),
                "country": row.country,
                "page": row.page,
                "asset": row.asset,
                "slot_name": row.slot_name or row.asset,
                "days": int(row.days),
                "buy_type": row.buyType,
                "rate": float(row.rate),
                "gross_cpm": float(row.gross_cpm),
                "net_cpm": float(row.net_cpm),
                "views": int(row.views or 0),
                "cost": float(row.cost),
                "gross_amount": float(row.gross_amount),
                "net_amount": float(row.net_amount),
                "discount_pct": float(row.discount_pct),
                "phase": row.phase,
                "brand": row.brand,
                "marketplace": row.marketplace,
                "category": row.category,
                "zone": row.zone,
                "dimension": row.dimension,
                "stype": row.stype,
                "slot_code": row.slot_code,
                "score": float(row.score),
                "available_views": int(row.available_views or 0),
                "historical_ctr": float(row.historical_ctr) if row.historical_ctr is not None else None,
                "historical_roas": float(row.historical_roas) if row.historical_roas is not None else None,
                "historical_cpm": float(row.historical_cpm) if row.historical_cpm is not None else None,
                "note": row.note or "",
                "manual": bool(row.manual),
                "locked": bool(row.locked),
            }
            for row in rows
        ]
        if line_rows:
            line_errors = self.client.insert_rows_json(self.settings.plan_lines_table, line_rows)
            if line_errors:
                raise RuntimeError(f"Failed to write media_plan_lines: {line_errors}")
        return plan_code, plan_link

    def get_saved_plan(self, reference: str) -> dict | None:
        lookup = (reference or "").strip()
        if not lookup:
            return None
        run_rows = self._query_records(
            f"""
            SELECT *
            FROM `{self.settings.plan_runs_table}`
            WHERE UPPER(plan_code) = UPPER(@plan_code)
            ORDER BY created_at DESC
            LIMIT 1
            """,
            [bigquery.ScalarQueryParameter("plan_code", "STRING", lookup)],
        )
        if not run_rows:
            return None

        run_row = run_rows[0]
        plan_code = str(run_row.get("plan_code") or lookup).strip()
        line_rows = self._query_records(
            f"""
            SELECT *
            FROM `{self.settings.plan_lines_table}`
            WHERE plan_code = @plan_code
            ORDER BY line_id
            """,
            [bigquery.ScalarQueryParameter("plan_code", "STRING", plan_code)],
        )
        request = json.loads(run_row.get("request_json") or "{}")
        diagnostics = json.loads(run_row.get("diagnostics_json") or "{}")
        rows = [
            EditablePlanLine.model_validate(
                {
                    "id": row.get("line_id"),
                    "from": row.get("from"),
                    "to": row.get("to"),
                    "country": row.get("country") or "",
                    "page": row.get("page") or "",
                    "marketplace": row.get("marketplace") or "",
                    "category": row.get("category") or "",
                    "zone": row.get("zone") or "",
                    "dimension": row.get("dimension") or "",
                    "asset": row.get("asset") or "",
                    "slot_name": row.get("slot_name") or row.get("asset") or "",
                    "days": row.get("days") or 0,
                    "buyType": row.get("buy_type") or "",
                    "rate": row.get("rate") or 0.0,
                    "gross_cpm": row.get("gross_cpm") or 0.0,
                    "net_cpm": row.get("net_cpm") or 0.0,
                    "views": row.get("views") or 0,
                    "cost": row.get("cost") or 0.0,
                    "gross_amount": row.get("gross_amount") or 0.0,
                    "net_amount": row.get("net_amount") or 0.0,
                    "discount_pct": row.get("discount_pct") or 0.0,
                    "phase": row.get("phase") or "",
                    "brand": row.get("brand") or "",
                    "stype": row.get("stype") or "reach",
                    "slot_code": row.get("slot_code") or "",
                    "score": row.get("score") or 0.0,
                    "available_views": row.get("available_views") or 0,
                    "historical_ctr": row.get("historical_ctr"),
                    "historical_roas": row.get("historical_roas"),
                    "historical_cpm": row.get("historical_cpm"),
                    "note": row.get("note") or "",
                    "manual": parse_bool(row.get("manual")),
                    "locked": parse_bool(row.get("locked")),
                }
            ).model_dump(mode="json", by_alias=True)
            for row in line_rows
        ]

        summary = {
            "brand": run_row.get("brand") or "",
            "comcats": [value.strip() for value in str(run_row.get("comcats") or "").split(",") if value.strip()],
            "countries": [value.strip() for value in str(run_row.get("countries") or "").split(",") if value.strip()],
            "budget": parse_number(run_row.get("budget_usd")),
            "discount_pct": parse_number(run_row.get("discount_pct")),
            "allocated": parse_number(run_row.get("allocated_usd")),
            "remaining": parse_number(run_row.get("remaining_usd")),
            "estimated_views": int(parse_number(run_row.get("estimated_views"))),
            "line_count": int(parse_number(run_row.get("line_count"))),
            "sheet_plan_code": plan_code,
            "sheet_plan_link": str(run_row.get("plan_link") or "").strip(),
            "plan_id": plan_code,
        }
        return {
            "plan_id": plan_code,
            "created_at": str(run_row.get("created_at") or ""),
            "request": request,
            "rows": rows,
            "summary": summary,
            "diagnostics": diagnostics,
            "response": {
                "rows": rows,
                "summary": summary,
                "diagnostics": diagnostics,
            },
        }
