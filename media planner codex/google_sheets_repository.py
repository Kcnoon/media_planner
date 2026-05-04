from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from uuid import uuid4

import google.auth
from google.auth.exceptions import DefaultCredentialsError
import gspread
from gspread.exceptions import APIError

from config import Settings
from models import MediaPlanRequest, EditablePlanLine


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
]

LINE_HEADERS = [
    "plan_code",
    "line_id",
    "from",
    "to",
    "country",
    "page",
    "asset",
    "days",
    "buy_type",
    "rate",
    "views",
    "cost",
    "phase",
    "brand",
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


class GoogleSheetsRepository:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = self._client()
        try:
            self.sheet = self.client.open_by_key(settings.google_sheet_id)
        except APIError as exc:
            message = str(exc)
            if "sheets.googleapis.com" in message and "disabled" in message.lower():
                raise RuntimeError(
                    "Google Sheets API is disabled for the Google Cloud project used by your credentials. "
                    "Enable the Google Sheets API for that project, wait a few minutes, then retry."
                ) from exc
            raise RuntimeError(
                "Google Sheets rejected the request. Check that the API is enabled and the spreadsheet is shared with the authenticated account."
            ) from exc
        except PermissionError as exc:
            raise RuntimeError(
                "Cannot open the Google Sheet. Enable the Google Sheets API for the credentials project and share the spreadsheet with the authenticated account/service account."
            ) from exc

    def _client(self):
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        try:
            creds, _ = google.auth.default(scopes=scopes)
            return gspread.authorize(creds)
        except DefaultCredentialsError as exc:
            raise RuntimeError(
                "Google Sheets credentials are not configured. Set GOOGLE_APPLICATION_CREDENTIALS to a service-account JSON file that has edit access to the spreadsheet, or run `gcloud auth application-default login`."
            ) from exc

    def _worksheet(self, title: str):
        return self.sheet.worksheet(title)

    def _records(self, title: str) -> list[dict]:
        rows = self._worksheet(title).get_all_records()
        return [{str(k).strip(): v for k, v in row.items()} for row in rows]

    def _ensure_worksheet(self, title: str, headers: list[str]):
        try:
            ws = self.sheet.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = self.sheet.add_worksheet(title=title, rows=1000, cols=max(len(headers), 10))
            ws.update("A1", [headers])
            return ws

        # Keep headers in sync (we overwrite row 1 to avoid drift).
        ws.update("A1", [headers])
        return ws

    def fetch_slot_catalog(self) -> list[dict]:
        try:
            rows = self._records(self.settings.slot_data_tab)
        except Exception:
            rows = []
        catalog = []
        for row in rows:
            slot_code = str(get_first(row, "slot_code", "slot") or "").strip()
            if not slot_code:
                continue
            country = infer_country(row)
            catalog.append(
                {
                    "country": country,
                    "slot_code": slot_code,
                    "slot_name": str(get_first(row, "slot_name", "asset", "slot") or slot_code).strip(),
                    "page": str(get_first(row, "page", "publisher") or "").strip(),
                    "publisher": str(get_first(row, "publisher") or "").strip(),
                    "pricing_model": str(get_first(row, "pricing_model", "buy_type") or "cpm").strip(),
                }
            )
        if catalog:
            return catalog
        fallback = []
        for row in self._records(self.settings.booking_tab):
            slot_code = str(get_first(row, "slot_code", "slot") or "").strip()
            if not slot_code:
                continue
            fallback.append(
                {
                    "country": infer_country(row),
                    "slot_code": slot_code,
                    "slot_name": str(get_first(row, "slot_name", "asset") or slot_code).strip(),
                    "page": str(get_first(row, "publisher", "page") or "").strip(),
                    "publisher": str(get_first(row, "publisher") or "").strip(),
                    "pricing_model": str(get_first(row, "pricing_model") or "cpm").strip(),
                }
            )
        seen = set()
        result = []
        for row in fallback:
            key = (row["country"], row["slot_code"])
            if key in seen:
                continue
            seen.add(key)
            result.append(row)
        return result

    def fetch_slot_meta(self) -> dict[tuple[str, str], dict]:
        return {(row["country"], row["slot_code"]): row for row in self.fetch_slot_catalog()}

    def fetch_historical_performance(self, req: MediaPlanRequest) -> list[dict]:
        rows = self._records(self.settings.delivery_tab)
        start = req.start_date - timedelta(days=self.settings.historical_lookback_days)
        comcats = {norm(c) for c in req.comcats if norm(c)}
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

        eligible_rows: list[dict] = []
        for row in rows:
            row_date = parse_date(get_first(row, "date", "dt"))
            if not row_date or row_date < start or row_date >= req.start_date:
                continue
            if infer_country(row) not in countries:
                continue
            eligible_rows.append(row)

        def matches_comcat(row: dict) -> bool:
            row_comcat = norm(get_first(row, "comcat", "comcat_code"))
            row_category = norm(get_first(row, "category"))
            return row_comcat in comcats or row_category in comcats

        def matches_brand(row: dict) -> bool:
            row_brand = norm(get_first(row, "brand"))
            return row_brand in brand_names

        if req.brand_tag == "old":
            brand_matched = [row for row in eligible_rows if matches_brand(row)]
            if brand_matched:
                return aggregate(brand_matched, prefer_brand=True)

            comcat_fallback = [row for row in eligible_rows if matches_comcat(row)]
            return aggregate(comcat_fallback, prefer_brand=False)

        matched = [row for row in eligible_rows if matches_comcat(row)]
        return aggregate(matched, prefer_brand=False)

    def fetch_inventory(self, req: MediaPlanRequest) -> list[dict]:
        forecast_rows = self._records(self.settings.forecast_tab)
        booking_rows = self._records(self.settings.booking_tab)
        countries = country_values(req.countries)

        booked_by_date_slot: dict[tuple[date, str, str], int] = defaultdict(int)
        for row in booking_rows:
            dt = parse_date(get_first(row, "dt", "date"))
            if not dt or dt < req.start_date or dt > req.end_date:
                continue
            row_country = infer_country(row)
            if row_country not in countries:
                continue
            status = norm(get_first(row, "campaign_status", "status"))
            if status in {"cancelled", "canceled", "rejected"}:
                continue
            slot = str(get_first(row, "slot_code", "slot") or "").strip()
            booked_by_date_slot[(dt, row_country, slot)] += int(parse_number(get_first(row, "booked_views", "delivered_views")))

        inventory: list[dict] = []
        for row in forecast_rows:
            dt = parse_date(get_first(row, "dt", "date"))
            if not dt or dt < req.start_date or dt > req.end_date:
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
        for title, field in ((self.settings.delivery_tab, "comcat"), (self.settings.booking_tab, "comcat")):
            try:
                for row in self._records(title):
                    value = str(get_first(row, field, "comcat_code") or "").strip()
                    if value:
                        values.add(value)
            except Exception:
                continue
        return sorted(values, key=str.lower)

    def save_plan(self, req: MediaPlanRequest, rows: list[EditablePlanLine], summary: dict, plan_code: str | None = None) -> tuple[str, str]:
        plan_code = plan_code or f"MP-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6].upper()}"
        lines_ws = self._ensure_worksheet(self.settings.plan_lines_tab, LINE_HEADERS)
        runs_ws = self._ensure_worksheet(self.settings.plan_runs_tab, RUN_HEADERS)
        plan_link = f"https://docs.google.com/spreadsheets/d/{self.settings.google_sheet_id}/edit#gid={lines_ws.id}"

        runs_ws.append_row(
            [
                plan_code,
                datetime.utcnow().isoformat(timespec="seconds") + "Z",
                req.brand,
                req.brand_tag,
                ", ".join(req.sub_brands or []),
                ", ".join(req.comcats),
                ", ".join(req.countries),
                (req.marketplace or ""),
                req.start_date.isoformat(),
                req.end_date.isoformat(),
                req.budget,
                req.currency,
                "TRUE" if req.budget_locked else "FALSE",
                req.le_code or "",
                req.notes or "",
                req.objective,
                summary["allocated"],
                summary["remaining"],
                summary["estimated_views"],
                summary["line_count"],
                plan_link,
            ],
            value_input_option="USER_ENTERED",
        )

        if rows:
            lines_ws.append_rows(
                [
                    [
                        plan_code,
                        row.id,
                        row.from_date.isoformat(),
                        row.to_date.isoformat(),
                        row.country,
                        row.page,
                        row.asset,
                        row.days,
                        row.buyType,
                        row.rate,
                        row.views or "",
                        row.cost,
                        row.phase,
                        row.brand,
                        row.stype,
                        row.slot_code,
                        row.score,
                        row.available_views,
                        row.historical_ctr or "",
                        row.historical_roas or "",
                        row.historical_cpm or "",
                        row.note or "",
                        "TRUE" if row.manual else "FALSE",
                        "TRUE" if row.locked else "FALSE",
                    ]
                    for row in rows
                ],
                value_input_option="USER_ENTERED",
            )
        return plan_code, plan_link
