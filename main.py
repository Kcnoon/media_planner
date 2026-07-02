from __future__ import annotations

import csv
from datetime import date
from functools import lru_cache
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from bigquery_repository import BigQueryRepository
from config import Settings, get_settings
from models import MediaPlanRequest, MediaPlanResponse
from planner import plan_media, suggest_slots


app = FastAPI(title="Noon Media Planner API")
BRAND_CODES_PATH = Path(__file__).with_name("brand_codes.csv")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.exception_handler(Exception)
def unhandled_exception_handler(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"detail": {"message": str(exc) or "Internal Server Error"}})


def get_repo(settings: Settings = Depends(get_settings)) -> BigQueryRepository:
    return BigQueryRepository(settings)


@lru_cache(maxsize=1)
def load_static_brand_codes() -> tuple[str, ...]:
    if not BRAND_CODES_PATH.exists():
        return tuple()
    with BRAND_CODES_PATH.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        values = {
            str(row.get("brand_code") or "").strip()
            for row in reader
            if str(row.get("brand_code") or "").strip()
        }
    return tuple(sorted(values, key=str.lower))


def search_static_brand_codes(query: str, limit: int = 30) -> list[str]:
    q = str(query or "").strip().lower()
    if len(q) < 2:
        return []
    starts_with = []
    contains = []
    for code in load_static_brand_codes():
        lowered = code.lower()
        if lowered.startswith(q):
            starts_with.append(code)
        elif q in lowered:
            contains.append(code)
        if len(starts_with) >= limit:
            break
    remaining = max(limit - len(starts_with), 0)
    return starts_with + contains[:remaining]


def build_response(req: MediaPlanRequest, rows, diagnostics, repo, plan_id=None):
    allocated = round(sum(row.cost for row in rows), 2)
    offdeck_allocated = round(sum(row.cost for row in rows if str(row.buyType or "").upper() == "OFF-DECK"), 2)
    on_deck_allocated = round(allocated - offdeck_allocated, 2)
    total_budget = float((req.budget or 0) + (req.offdeck_budget or 0))
    on_deck_rows = [row for row in rows if str(row.buyType or "").upper() != "OFF-DECK"]
    views = sum(row.views or 0 for row in on_deck_rows)
    weighted_ctr_den = sum(max(row.views or 0, 0) for row in on_deck_rows)
    weighted_roas_den = sum(max(float(row.cost or row.net_amount or 0), 0.0) for row in on_deck_rows)
    expected_ctr = (
        sum((row.historical_ctr or 0.0) * max(row.views or 0, 0) for row in on_deck_rows) / weighted_ctr_den
        if weighted_ctr_den > 0 else 0.0
    )
    expected_roas = (
        sum((row.historical_roas or 0.0) * max(float(row.cost or row.net_amount or 0), 0.0) for row in on_deck_rows) / weighted_roas_den
        if weighted_roas_den > 0 else 0.0
    )
    confidence_values = [float(item.get("confidence_score") or 0.0) for item in diagnostics.get("suggested_slots", []) if isinstance(item, dict)]
    if not confidence_values:
        confidence_values = [min(max(float(getattr(row, "score", 0.0) or 0.0), 0.0), 1.0) for row in rows if getattr(row, "score", None) is not None]
    overall_confidence = round((sum(confidence_values) / len(confidence_values)) * 100, 1) if confidence_values else 0.0
    summary = {
        "brand": req.brand,
        "comcats": req.comcats,
        "countries": req.countries,
        "budget": req.budget,
        "total_budget": total_budget,
        "on_deck_budget": req.budget,
        "offdeck_budget": req.offdeck_budget,
        "discount_pct": req.discount_pct,
        "allocated": allocated,
        "on_deck_allocated": on_deck_allocated,
        "offdeck_allocated": offdeck_allocated,
        "remaining": round(total_budget - allocated, 2),
        "estimated_views": views,
        "line_count": len(rows),
        "expected_ctr": round(expected_ctr, 4),
        "expected_roas": round(expected_roas, 4),
        "expected_reach": views,
        "overall_confidence": overall_confidence,
    }

    sheet_plan_code, sheet_plan_link = repo.save_plan(
        req,
        rows,
        summary,
        diagnostics,
        plan_code=plan_id,
    )
    summary["sheet_plan_code"] = sheet_plan_code
    summary["sheet_plan_link"] = sheet_plan_link
    summary["plan_id"] = sheet_plan_code
    return {"rows": rows, "summary": summary, "diagnostics": diagnostics}


def build_available_slots(req: MediaPlanRequest, inventory_rows, slot_meta):
    available_by_slot = {}
    for row in inventory_rows:
        available = max(int(row.get("available_views") or 0), 0)
        if available <= 0:
            continue
        key = (row.get("country") or "", row.get("slot_code") or "")
        if not key[0] or not key[1]:
            continue
        available_by_slot[key] = available_by_slot.get(key, 0) + available

    available_slots = []
    for key, available in available_by_slot.items():
        meta = slot_meta.get(key, {})
        cpm_rate = float(meta.get("cpm_rate") or 0) or 0.0
        cpd_rate = float(meta.get("cpd_rate") or 0) or 0.0
        slot_name = str(meta.get("slot_name") or key[1]).strip()
        available_slots.append(
            {
                "country": key[0],
                "slot_code": key[1],
                "slot_name": slot_name,
                "page": str(meta.get("page") or meta.get("publisher") or "").strip(),
                "category": str(meta.get("category") or meta.get("page") or "").strip(),
                "zone": str(meta.get("zone") or "").strip(),
                "dimension": str(meta.get("dimension") or "").strip(),
                "publisher": str(meta.get("publisher") or "").strip(),
                "pricing_model": str(meta.get("pricing_model") or "CPM").strip(),
                "pricing_options": list(meta.get("pricing_options") or [str(meta.get("pricing_model") or "CPM").strip()]),
                "rate": float(meta.get("rate") or 0) or 0.0,
                "cpm_rate": cpm_rate,
                "cpd_rate": cpd_rate,
                "available_views": available,
            }
        )
    available_slots.sort(key=lambda slot: (slot["country"], -slot["available_views"], slot["slot_name"].lower()))
    return available_slots


@app.get("/")
def index():
    return FileResponse("noon_media_planner.html", headers={"Cache-Control": "no-store"})


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/options")
def list_options(repo: BigQueryRepository = Depends(get_repo)):
    return {
        "countries": ["ae", "sa", "eg"],
        "comcats": repo.list_comcats(),
        "slots": repo.fetch_slot_catalog(),
    }


@app.get("/api/brand-codes/search")
def search_brand_codes(q: str = "", limit: int = 30):
    return {
        "brand_codes": search_static_brand_codes(q, max(1, min(int(limit or 30), 50))),
    }


@app.post("/api/slot-preselection")
def slot_preselection(req: MediaPlanRequest, settings: Settings = Depends(get_settings), repo: BigQueryRepository = Depends(get_repo)):
    req.brand_tag = req.brand_tag or repo.infer_brand_tag(req)
    historical_rows = repo.fetch_historical_performance(req)
    inventory_rows = repo.fetch_inventory(req)
    slot_meta = repo.fetch_slot_meta(req)
    suggestions = suggest_slots(req, historical_rows, inventory_rows, slot_meta, settings, limit=max(len(req.countries), 1) * (6 if req.budget <= 10000 else 10))
    available_slots = build_available_slots(req, inventory_rows, slot_meta)
    offdeck_slots = repo.fetch_offdeck_slots(req)
    return {
        "suggestions": suggestions,
        "available_slots": available_slots,
        "offdeck_slots": offdeck_slots,
        "diagnostics": {
            "historical_rows": len(historical_rows),
            "inventory_rows": len(inventory_rows),
            "selected_countries": req.countries,
            "selected_comcats": req.comcats,
            "brand_tag": req.brand_tag,
        },
    }


@app.post("/api/media-plan", response_model=MediaPlanResponse)
def create_media_plan(req: MediaPlanRequest, settings: Settings = Depends(get_settings), repo: BigQueryRepository = Depends(get_repo)):
    if req.end_date < req.start_date:
        raise HTTPException(status_code=400, detail="end_date must be on or after start_date")
    if req.start_date < date.today():
        raise HTTPException(status_code=400, detail="start_date must be today or later")
    if req.budget <= 0:
        raise HTTPException(status_code=400, detail="on-deck budget must be positive")
    req.total_budget = req.budget + req.offdeck_budget
    if not req.comcats:
        raise HTTPException(status_code=400, detail="select at least one comcat")
    if not req.countries:
        raise HTTPException(status_code=400, detail="select at least one country")
    if any(c.lower() not in {"ae", "sa", "eg"} for c in req.countries):
        raise HTTPException(status_code=400, detail="countries must be selected from ae, sa, eg")

    req.brand_tag = req.brand_tag or repo.infer_brand_tag(req)
    historical_rows = repo.fetch_historical_performance(req)
    inventory_rows = repo.fetch_inventory(req)
    slot_meta = repo.fetch_slot_meta(req)
    rows, diagnostics = plan_media(req, historical_rows, inventory_rows, slot_meta, settings)
    diagnostics.update({"selected_comcats": req.comcats, "selected_countries": req.countries, "brand_tag": req.brand_tag})
    if not rows:
        raise HTTPException(status_code=422, detail={"message": diagnostics.get("reason") or "No plan rows generated.", "diagnostics": diagnostics})
    return build_response(req, rows, diagnostics, repo)


@app.post("/api/media-plan/{plan_id}/regenerate", response_model=MediaPlanResponse)
def regenerate_media_plan(plan_id: str, req: MediaPlanRequest, settings: Settings = Depends(get_settings), repo: BigQueryRepository = Depends(get_repo)):
    if req.budget <= 0:
        raise HTTPException(status_code=400, detail="on-deck budget must be positive")
    req.total_budget = req.budget + req.offdeck_budget
    req.brand_tag = req.brand_tag or repo.infer_brand_tag(req)
    historical_rows = repo.fetch_historical_performance(req)
    inventory_rows = repo.fetch_inventory(req)
    slot_meta = repo.fetch_slot_meta(req)
    rows, diagnostics = plan_media(req, historical_rows, inventory_rows, slot_meta, settings)
    diagnostics.update({"selected_comcats": req.comcats, "selected_countries": req.countries, "brand_tag": req.brand_tag, "regenerated": True})
    if not rows:
        raise HTTPException(status_code=422, detail={"message": diagnostics.get("reason") or "No plan rows generated.", "diagnostics": diagnostics})
    return build_response(req, rows, diagnostics, repo, plan_id=plan_id)


@app.get("/api/media-plan/{plan_id}")
def get_media_plan(plan_id: str, repo: BigQueryRepository = Depends(get_repo)):
    plan = repo.get_saved_plan(plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="plan not found")
    return plan


app.mount("/static", StaticFiles(directory="."), name="static")
