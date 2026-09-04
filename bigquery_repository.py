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


MIN_RECENT_BOOKED_VIEWS = 1_000


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


def slot_code_key(value) -> str:
    return norm(value)


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


def normalize_pricing_model(value) -> str:
    raw = norm(value)
    if raw in {"cpd", "cost per day", "cost_per_day", "day"}:
        return "CPD"
    return "CPM"


def infer_pricing_model_from_slot(slot_code: str, slot_name: str | None = None) -> str:
    text = f"{slot_code or ''} {slot_name or ''}".lower()
    if "cpd" in text or "cost per day" in text or "cost_per_day" in text:
        return "CPD"
    return "CPM"


def pricing_options_from_values(cpm_value, cpd_value) -> list[str]:
    options = []
    if parse_number(cpm_value) > 0:
        options.append("CPM")
    if parse_number(cpd_value) > 0:
        options.append("CPD")
    if "CPM" in options:
        options = ["CPM"] + [option for option in options if option != "CPM"]
    return list(dict.fromkeys(options))


def pricing_options_for_type(pricing_model: str) -> list[str]:
    return [normalize_pricing_model(pricing_model)]


def is_cpd_booking_row(row: dict) -> bool:
    pricing_value = get_first(row, "type", "pricing_type", "pricing_model", "buy_type", "buy type")
    if normalize_pricing_model(pricing_value) == "CPD":
        return True
    slot_code = str(get_first(row, "slot_code", "slot") or "")
    slot_name = str(get_first(row, "slot_name", "asset") or "")
    return infer_pricing_model_from_slot(slot_code, slot_name) == "CPD"


def combined_dimension(row: dict) -> str:
    app_dimension = str(get_first(row, "app_dimension", "app dimension") or "").strip()
    web_dimension = str(get_first(row, "web_dimension", "web dimension") or "").strip()
    if app_dimension and web_dimension:
        return f"App: {app_dimension} | Web: {web_dimension}"
    if app_dimension:
        return f"App: {app_dimension}"
    if web_dimension:
        return f"Web: {web_dimension}"
    return str(
        get_first(
            row,
            "dimension",
            "dimensions",
            "size",
            "ad_size",
            "creative_dimension",
            "dimension information",
        )
        or ""
    ).strip()


class BigQueryRepository:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = bigquery.Client(project=settings.bigquery_project or None)
        self._schema_cache: dict[str, dict[str, str]] = {}
        self._recent_booking_cache: dict[tuple[date, tuple[str, ...]], dict[tuple[str, str], int]] = {}

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
    ) -> tuple[dict[tuple[str, str], dict], dict[str, dict]]:
        rows = self._table_records_for_window(
            self.settings.slot_rate_card_table,
            start,
            end,
            "date",
            "dt",
        )

        def bucket():
            return {
                "cpm_rates": [],
                "cpd_rates": [],
                "cpm_rate_schedule": {},
                "cpd_rate_schedule": {},
            }

        by_country_slot: dict[tuple[str, str], dict] = defaultdict(bucket)
        by_slot: dict[str, dict] = defaultdict(bucket)
        for row in rows:
            slot_code = str(get_first(row, "slot_code", "slot") or "").strip()
            if not slot_code:
                continue
            slot_key = slot_code_key(slot_code)
            country = infer_country(row)
            dt = parse_date(get_first(row, "date", "dt"))
            cpm_rate = parse_number(get_first(row, "cpm_rate"))
            cpd_rate = parse_number(get_first(row, "cpd_rate"))
            target_slot = by_slot[slot_key]
            if cpm_rate > 0:
                target_slot["cpm_rates"].append(cpm_rate)
                if dt:
                    target_slot["cpm_rate_schedule"][dt.isoformat()] = cpm_rate
            if cpd_rate > 0:
                target_slot["cpd_rates"].append(cpd_rate)
                if dt:
                    target_slot["cpd_rate_schedule"][dt.isoformat()] = cpd_rate
            if country:
                target_country_slot = by_country_slot[(country, slot_key)]
                if cpm_rate > 0:
                    target_country_slot["cpm_rates"].append(cpm_rate)
                    if dt:
                        target_country_slot["cpm_rate_schedule"][dt.isoformat()] = cpm_rate
                if cpd_rate > 0:
                    target_country_slot["cpd_rates"].append(cpd_rate)
                    if dt:
                        target_country_slot["cpd_rate_schedule"][dt.isoformat()] = cpd_rate

        def finalize(raw_map: dict) -> dict:
            finalized = {}
            for key, payload in raw_map.items():
                cpm_rates = payload.get("cpm_rates") or []
                cpd_rates = payload.get("cpd_rates") or []
                cpm_rate = round(sum(cpm_rates) / len(cpm_rates), 4) if cpm_rates else 0.0
                cpd_rate = round(sum(cpd_rates) / len(cpd_rates), 4) if cpd_rates else 0.0
                finalized[key] = {
                    "cpm_rate": cpm_rate,
                    "cpd_rate": cpd_rate,
                    "cpm_rate_schedule": dict(payload.get("cpm_rate_schedule") or {}),
                    "cpd_rate_schedule": dict(payload.get("cpd_rate_schedule") or {}),
                    "pricing_options": pricing_options_from_values(cpm_rate, cpd_rate),
                }
            return finalized

        return finalize(dict(by_country_slot)), finalize(dict(by_slot))

    def _fetch_booked_cpd_slot_keys(self, req: MediaPlanRequest) -> set[tuple[str, str]]:
        rows = self._table_records_for_window(
            self.settings.booking_table,
            req.start_date,
            req.end_date,
            "dt",
            "date",
        )
        countries = country_values(req.countries)
        booked: set[tuple[str, str]] = set()
        for row in rows:
            if not is_cpd_booking_row(row):
                continue
            country = infer_country(row)
            if countries and country not in countries:
                continue
            slot_code = str(get_first(row, "slot_code", "slot") or "").strip()
            if slot_code:
                booked.add((country, slot_code_key(slot_code)))
        return booked

    def _fetch_recent_booked_views(self, req: MediaPlanRequest) -> dict[tuple[str, str], int]:
        """Booked views by country/slot in the rolling six-month window ending yesterday."""
        requested_countries = tuple(sorted(country_values(req.countries)))
        as_of_date = date.today()
        cache_key = (as_of_date, requested_countries)
        cached = self._recent_booking_cache.get(cache_key)
        if cached is not None:
            return cached

        table_id = self.settings.adgroup_booked_delivered_table
        date_field = self._field_name(
            table_id,
            "date", "dt", "event_date", "booking_date", "adgroup_date", "report_date",
        )
        slot_field = self._field_name(table_id, "slot_code", "slot", "placement_code")
        booked_field = self._field_name(
            table_id,
            "booked_views", "booked_view", "booked_impressions", "booked_sessions",
            "views_booked", "booking_views", "booked",
        )
        country_field = self._field_name(table_id, "country", "country_code", "market")
        missing = [
            label
            for label, field in (("date", date_field), ("slot", slot_field), ("booked views", booked_field))
            if not field
        ]
        if missing:
            raise RuntimeError(
                f"{table_id} is missing required {', '.join(missing)} column(s) for slot booking eligibility."
            )

        country_select = f"CAST(`{country_field}` AS STRING)" if country_field else "''"
        sql = f"""
            SELECT
                {country_select} AS country,
                CAST(`{slot_field}` AS STRING) AS slot_code,
                SUM(COALESCE(SAFE_CAST(`{booked_field}` AS FLOAT64), 0)) AS booked_views
            FROM `{table_id}`
            WHERE DATE(`{date_field}`) >= DATE_SUB(@as_of_date, INTERVAL 6 MONTH)
              AND DATE(`{date_field}`) < @as_of_date
            GROUP BY 1, 2
        """
        rows = self._query_records(
            sql,
            [
                bigquery.ScalarQueryParameter("as_of_date", "DATE", as_of_date.isoformat()),
            ],
        )
        booked_by_slot: dict[tuple[str, str], int] = defaultdict(int)
        countries = set(requested_countries)
        for row in rows:
            country = infer_country(row)
            if countries and country and country not in countries:
                continue
            slot_code = slot_code_key(get_first(row, "slot_code", "slot"))
            if not slot_code:
                continue
            booked_by_slot[(country, slot_code)] += int(parse_number(get_first(row, "booked_views")))
        result = dict(booked_by_slot)
        self._recent_booking_cache[cache_key] = result
        return result

    def fetch_slot_catalog(self, req: MediaPlanRequest | None = None) -> list[dict]:
        rows = self._query_records(f"SELECT * FROM `{self.settings.slot_data_table}`")
        rate_by_country_slot: dict[tuple[str, str], dict] = {}
        rate_by_slot: dict[str, dict] = {}
        cpd_blocked_slot_keys: set[tuple[str, str]] = set()
        recent_booked_views: dict[tuple[str, str], int] | None = None
        exclude_cpd_by_budget = False
        if req is not None:
            rate_by_country_slot, rate_by_slot = self._fetch_rate_card_map(req.start_date, req.end_date)
            cpd_blocked_slot_keys = self._fetch_booked_cpd_slot_keys(req)
            recent_booked_views = self._fetch_recent_booked_views(req)
            exclude_cpd_by_budget = float(getattr(req, "budget", 0) or 0) < 15000
        catalog = []
        for row in rows:
            slot_code = str(get_first(row, "slot_code", "slot") or "").strip()
            if not slot_code:
                continue
            slot_name = str(get_first(row, "slot_name", "asset", "slot") or slot_code).strip()
            country = infer_country(row)
            normalized_slot_code = slot_code_key(slot_code)
            booked_views = None
            if recent_booked_views is not None:
                booked_views = recent_booked_views.get(
                    (country, normalized_slot_code),
                    recent_booked_views.get(("", normalized_slot_code), 0),
                )
                if booked_views < MIN_RECENT_BOOKED_VIEWS:
                    continue
            rate_meta = rate_by_country_slot.get((country, normalized_slot_code)) or rate_by_slot.get(normalized_slot_code) or {}
            pricing_model = normalize_pricing_model(get_first(row, "type", "pricing_type", "buy_type", "pricing_model") or infer_pricing_model_from_slot(slot_code, slot_name))
            if pricing_model == "CPD" and (exclude_cpd_by_budget or (country, normalized_slot_code) in cpd_blocked_slot_keys):
                continue
            has_rate_card = bool(
                rate_meta
                and (
                    rate_meta.get("cpm_rate")
                    or rate_meta.get("cpd_rate")
                    or rate_meta.get("cpm_rate_schedule")
                    or rate_meta.get("cpd_rate_schedule")
                )
            )
            cpm_rate = float(rate_meta.get("cpm_rate") or 0.0) if has_rate_card else 0.0
            cpd_rate = float(rate_meta.get("cpd_rate") or 0.0) if has_rate_card else 0.0
            pricing_options = pricing_options_for_type(pricing_model)
            if pricing_model == "CPM" and cpm_rate <= 0:
                cpm_rate = float(self.settings.default_cpm)
            if pricing_model == "CPD" and cpd_rate <= 0:
                cpd_rate = float(self.settings.default_cpd)
            default_rate = cpd_rate if pricing_model == "CPD" else cpm_rate
            catalog.append(
                {
                    "country": country,
                    "slot_code": slot_code,
                    "slot_name": slot_name,
                    "page": str(get_first(row, "category", "page", "publisher") or "").strip(),
                    "category": str(get_first(row, "category", "page", "publisher") or "").strip(),
                    "zone": str(get_first(row, "zone") or "").strip(),
                    "dimension": combined_dimension(row),
                    "publisher": str(get_first(row, "publisher") or "").strip(),
                    "type": pricing_model,
                    "pricing_model": pricing_model,
                    "pricing_options": pricing_options,
                    "cpm_rate": cpm_rate,
                    "cpd_rate": cpd_rate,
                    "rate": default_rate,
                    "rate_schedule": dict(rate_meta.get("cpm_rate_schedule") or {}),
                    "cpm_rate_schedule": dict(rate_meta.get("cpm_rate_schedule") or {}),
                    "cpd_rate_schedule": dict(rate_meta.get("cpd_rate_schedule") or {}),
                    "booked_views_last_6_months": booked_views,
                }
            )
        return catalog

    def fetch_slot_meta(self, req: MediaPlanRequest | None = None) -> dict[tuple[str, str], dict]:
        return {(row["country"], row["slot_code"]): row for row in self.fetch_slot_catalog(req)}

    def fetch_offdeck_slots(self, req: MediaPlanRequest | None = None) -> list[dict]:
        rows = self._query_records(f"SELECT * FROM `{self.settings.offdeck_slots_table}`")
        countries = country_values(req.countries) if req is not None else set()
        recent_booked_views = self._fetch_recent_booked_views(req) if req is not None else None
        slots = []
        seen = set()
        for index, row in enumerate(rows):
            country = infer_country(row)
            if countries and country not in countries:
                continue
            page = str(get_first(row, "Page", "page") or "").strip()
            slot_code = str(get_first(row, "slot_code") or "").strip()
            slot_name = str(get_first(row, "slot_name") or slot_code or f"offdeck_{index + 1}").strip()
            key = slot_code or slot_name
            if not key or key in seen:
                continue
            normalized_key = slot_code_key(key)
            booked_views = None
            if recent_booked_views is not None:
                booked_views = recent_booked_views.get(
                    (country, normalized_key),
                    recent_booked_views.get(("", normalized_key), 0),
                )
                if booked_views < MIN_RECENT_BOOKED_VIEWS:
                    continue
            seen.add(key)
            slots.append(
                {
                    "slot_key": key,
                    "slot_code": slot_code or key,
                    "slot_name": slot_name,
                    "page": page,
                    "category": page,
                    "marketplace": "offdeck",
                    "country": country,
                    "dimension": "",
                    "description": "",
                    "booked_views_last_6_months": booked_views,
                }
            )
        slots.sort(key=lambda slot: (str(slot.get("country") or ""), str(slot.get("page") or "").lower(), str(slot.get("slot_name") or "").lower()))
        return slots

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
        selected_brands = {norm(value) for value in (getattr(req, "brands", []) or []) if norm(value)}
        sub_brands = {norm(value) for value in (req.sub_brands or []) if norm(value)}
        brand_names = {value for value in {brand, *selected_brands, *sub_brands} if value}

        # Fine page per slot from the catalog, so page×comcat pooling is at the real page
        # level (Home Page / CLP / Salepage…), not the coarse delivery publisher field.
        slot_pages: dict[tuple[str, str], str] = {}
        for _cat in self.fetch_slot_catalog():
            _c, _s = _cat.get("country"), slot_code_key(_cat.get("slot_code"))
            if _c and _s:
                slot_pages[(_c, _s)] = str(_cat.get("page") or _cat.get("category") or "").strip()

        def aggregate(source_rows: list[dict], prefer_brand: bool) -> list[dict]:
            grouped: dict[tuple[str, str, str, str], dict] = {}
            active_dates: dict[tuple[str, str, str, str], set[date]] = defaultdict(set)
            active_dates_30: dict[tuple[str, str, str, str], set[date]] = defaultdict(set)
            active_dates_90: dict[tuple[str, str, str, str], set[date]] = defaultdict(set)
            active_dates_180: dict[tuple[str, str, str, str], set[date]] = defaultdict(set)
            # Weighted RoAS is pooled at (page × comcat) level, not per slot: a thinly
            # delivered slot inherits the robust page×comcat ratio instead of its own noise.
            pagecomcat_rev: dict[tuple[str, str], float] = defaultdict(float)
            pagecomcat_spend: dict[tuple[str, str], float] = defaultdict(float)
            key_pc_spend: dict[tuple, dict[tuple[str, str], float]] = defaultdict(lambda: defaultdict(float))
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
                        "views_30d": 0,
                        "clicks_30d": 0,
                        "revenue_30d": 0.0,
                        "spends_30d": 0.0,
                        "active_days_30d": 0,
                        "views_90d": 0,
                        "clicks_90d": 0,
                        "revenue_90d": 0.0,
                        "spends_90d": 0.0,
                        "active_days_90d": 0,
                        "views_180d": 0,
                        "clicks_180d": 0,
                        "revenue_180d": 0.0,
                        "spends_180d": 0.0,
                        "active_days_180d": 0,
                        "brand_specific": False,
                    },
                )
                row_views = int(parse_number(get_first(row, "views", "impressions")))
                row_clicks = int(parse_number(get_first(row, "clicks")))
                row_revenue = parse_number(get_first(row, "revenue"))
                row_spends = parse_number(get_first(row, "spends", "spend", "cost"))
                target["views"] += row_views
                target["clicks"] += row_clicks
                target["revenue"] += row_revenue
                target["spends"] += row_spends
                pc_key = (
                    norm(slot_pages.get((row_country, slot_code_key(get_first(row, "slot_code", "slot"))), "")
                         or get_first(row, "page", "publisher", "category")),
                    norm(get_first(row, "comcat", "comcat_code", "category")),
                )
                pagecomcat_rev[pc_key] += row_revenue
                pagecomcat_spend[pc_key] += row_spends
                key_pc_spend[key][pc_key] += row_spends
                if prefer_brand and norm(get_first(row, "brand")) == brand:
                    target["brand_specific"] = True
                row_date = parse_date(get_first(row, "date", "dt"))
                if row_date:
                    active_dates[key].add(row_date)
                    lag = (req.start_date - row_date).days
                    if lag <= 30:
                        target["views_30d"] += row_views
                        target["clicks_30d"] += row_clicks
                        target["revenue_30d"] += row_revenue
                        target["spends_30d"] += row_spends
                        active_dates_30[key].add(row_date)
                    if lag <= 90:
                        target["views_90d"] += row_views
                        target["clicks_90d"] += row_clicks
                        target["revenue_90d"] += row_revenue
                        target["spends_90d"] += row_spends
                        active_dates_90[key].add(row_date)
                    if lag <= 180:
                        target["views_180d"] += row_views
                        target["clicks_180d"] += row_clicks
                        target["revenue_180d"] += row_revenue
                        target["spends_180d"] += row_spends
                        active_dates_180[key].add(row_date)
            for key, target in grouped.items():
                target["active_days"] = max(len(active_dates[key]), 1)
                target["active_days_30d"] = len(active_dates_30[key])
                target["active_days_90d"] = len(active_dates_90[key])
                target["active_days_180d"] = len(active_dates_180[key])
                # Assign the slot the weighted RoAS of its dominant (page, comcat) bucket.
                pcs = key_pc_spend.get(key)
                if pcs:
                    dom = max(pcs, key=pcs.get)
                    dom_spend = pagecomcat_spend.get(dom, 0.0)
                    target["roas_pagecomcat"] = round(pagecomcat_rev[dom] / dom_spend, 6) if dom_spend > 0 else None
                    target["pagecomcat"] = f"{dom[0]}|{dom[1]}"
                else:
                    target["roas_pagecomcat"] = None
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

        def dedupe_rows(source_rows: list[dict]) -> list[dict]:
            seen = set()
            unique = []
            for row in source_rows:
                marker = (
                    infer_country(row),
                    str(get_first(row, "slot_code", "slot") or "").strip(),
                    str(get_first(row, "date", "dt") or "").strip(),
                    str(get_first(row, "brand") or "").strip().lower(),
                    str(get_first(row, "pricing_model", "buy_type", "buy type") or "").strip().lower(),
                )
                if marker in seen:
                    continue
                seen.add(marker)
                unique.append(row)
            return unique

        if requested_brand_tag == "old":
            union_country = dedupe_rows([*brand_exact_country_rows, *brand_similar_country_rows, *exact_country_rows, *similar_country_rows])
            if union_country:
                return aggregate(union_country, prefer_brand=True)
            union_global = dedupe_rows([*brand_exact_global_rows, *brand_similar_global_rows, *exact_global_rows, *similar_global_rows])
            return aggregate(union_global, prefer_brand=True)

        if exact_country_rows or similar_country_rows:
            return aggregate(dedupe_rows([*exact_country_rows, *similar_country_rows]), prefer_brand=False)
        return aggregate(dedupe_rows([*exact_global_rows, *similar_global_rows]), prefer_brand=False)

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
        selected_brands = {norm(value) for value in (getattr(req, "brands", []) or []) if norm(value)}
        sub_brands = {norm(value) for value in (req.sub_brands or []) if norm(value)}
        brand_names = {value for value in {brand, *selected_brands, *sub_brands} if value}
        for row in rows:
            if infer_country(row) not in countries:
                continue
            if norm(get_first(row, "brand")) in brand_names:
                return "old"
        return "new"

    def fetch_inventory(self, req: MediaPlanRequest) -> list[dict]:
        active_slots = {
            (row["country"], slot_code_key(row["slot_code"])): row
            for row in self.fetch_slot_catalog(req)
            if row.get("country") and row.get("slot_code")
        }
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
            slot_key_value = slot_code_key(slot)
            if (row_country, slot_key_value) not in active_slots:
                continue
            booked_by_date_slot[(dt, row_country, slot_key_value)] += int(parse_number(get_first(row, "booked_views", "delivered_views")))

        inventory: list[dict] = []
        for row in forecast_rows:
            dt = parse_date(get_first(row, "dt", "date"))
            if not dt:
                continue
            row_country = infer_country(row)
            if row_country not in countries:
                continue
            slot = str(get_first(row, "slot", "slot_code") or "").strip()
            slot_key_value = slot_code_key(slot)
            active_meta = active_slots.get((row_country, slot_key_value))
            if not active_meta:
                continue
            forecast = int(parse_number(get_first(row, "slot_sessions", "forecast_views", "views")))
            booked = booked_by_date_slot.get((dt, row_country, slot_key_value), 0)
            inventory.append(
                {
                    "dt": dt,
                    "country": row_country,
                    "slot_code": active_meta.get("slot_code") or slot,
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

    def list_brand_codes(self) -> list[str]:
        rows = self._query_records(
            f"""
            SELECT DISTINCT CAST(brand_code AS STRING) AS brand_code
            FROM `{self.settings.brand_code_table}`
            WHERE brand_code IS NOT NULL AND CAST(brand_code AS STRING) != ''
            ORDER BY brand_code
            """
        )
        values = {str(row.get("brand_code") or "").strip() for row in rows}
        return sorted([value for value in values if value], key=str.lower)

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
            "budget_usd": float(req.total_budget or ((req.budget or 0) + (req.offdeck_budget or 0))),
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
