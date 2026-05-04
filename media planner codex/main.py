from __future__ import annotations

from datetime import date

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config import Settings, get_settings
from google_sheets_repository import GoogleSheetsRepository
from models import MediaPlanRequest, MediaPlanResponse
from plan_store import PlanStore
from planner import plan_media, suggest_slots


app = FastAPI(title="Noon Media Planner API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.exception_handler(Exception)
def unhandled_exception_handler(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"detail": {"message": str(exc) or "Internal Server Error"}})


def get_repo(settings: Settings = Depends(get_settings)) -> GoogleSheetsRepository:
    return GoogleSheetsRepository(settings)


def get_store() -> PlanStore:
    return PlanStore()


def build_response(req: MediaPlanRequest, rows, diagnostics, repo, store, plan_id=None):
    allocated = round(sum(row.cost for row in rows), 2)
    views = sum(row.views or 0 for row in rows)
    summary = {
        "brand": req.brand,
        "comcats": req.comcats,
        "countries": req.countries,
        "budget": req.budget,
        "allocated": allocated,
        "remaining": round(req.budget - allocated, 2),
        "estimated_views": views,
        "line_count": len(rows),
    }
    sheet_plan_code, sheet_plan_link = repo.save_plan(req, rows, summary, plan_code=plan_id)
    summary["sheet_plan_code"] = sheet_plan_code
    summary["sheet_plan_link"] = sheet_plan_link
    plan_id = store.save(req, rows, summary, diagnostics, plan_id=plan_id)
    summary["plan_id"] = plan_id
    return {"rows": rows, "summary": summary, "diagnostics": diagnostics}


@app.get("/")
def index():
    return FileResponse("noon_media_planner.html", headers={"Cache-Control": "no-store"})


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/options")
def list_options(repo: GoogleSheetsRepository = Depends(get_repo)):
    return {
        "countries": ["ae", "sa", "eg"],
        "comcats": repo.list_comcats(),
        "slots": repo.fetch_slot_catalog(),
    }


@app.post("/api/slot-preselection")
def slot_preselection(req: MediaPlanRequest, settings: Settings = Depends(get_settings), repo: GoogleSheetsRepository = Depends(get_repo)):
    historical_rows = repo.fetch_historical_performance(req)
    inventory_rows = repo.fetch_inventory(req)
    slot_meta = repo.fetch_slot_meta()
    suggestions = suggest_slots(req, historical_rows, inventory_rows, slot_meta, settings, limit=10)
    return {
        "suggestions": suggestions,
        "diagnostics": {
            "historical_rows": len(historical_rows),
            "inventory_rows": len(inventory_rows),
            "selected_countries": req.countries,
            "selected_comcats": req.comcats,
        },
    }


@app.post("/api/media-plan", response_model=MediaPlanResponse)
def create_media_plan(req: MediaPlanRequest, settings: Settings = Depends(get_settings), repo: GoogleSheetsRepository = Depends(get_repo), store: PlanStore = Depends(get_store)):
    if req.end_date < req.start_date:
        raise HTTPException(status_code=400, detail="end_date must be on or after start_date")
    if req.start_date < date.today():
        raise HTTPException(status_code=400, detail="start_date must be today or later")
    if req.budget <= 0:
        raise HTTPException(status_code=400, detail="budget must be positive")
    if not req.comcats:
        raise HTTPException(status_code=400, detail="select at least one comcat")
    if not req.countries:
        raise HTTPException(status_code=400, detail="select at least one country")
    if any(c.lower() not in {"ae", "sa", "eg"} for c in req.countries):
        raise HTTPException(status_code=400, detail="countries must be selected from ae, sa, eg")

    historical_rows = repo.fetch_historical_performance(req)
    inventory_rows = repo.fetch_inventory(req)
    slot_meta = repo.fetch_slot_meta()
    rows, diagnostics = plan_media(req, historical_rows, inventory_rows, slot_meta, settings)
    diagnostics.update({"selected_comcats": req.comcats, "selected_countries": req.countries, "brand_tag": req.brand_tag})
    if not rows:
        raise HTTPException(status_code=422, detail={"message": diagnostics.get("reason") or "No plan rows generated.", "diagnostics": diagnostics})
    return build_response(req, rows, diagnostics, repo, store)


@app.post("/api/media-plan/{plan_id}/regenerate", response_model=MediaPlanResponse)
def regenerate_media_plan(plan_id: str, req: MediaPlanRequest, settings: Settings = Depends(get_settings), repo: GoogleSheetsRepository = Depends(get_repo), store: PlanStore = Depends(get_store)):
    historical_rows = repo.fetch_historical_performance(req)
    inventory_rows = repo.fetch_inventory(req)
    slot_meta = repo.fetch_slot_meta()
    rows, diagnostics = plan_media(req, historical_rows, inventory_rows, slot_meta, settings)
    diagnostics.update({"selected_comcats": req.comcats, "selected_countries": req.countries, "brand_tag": req.brand_tag, "regenerated": True})
    if not rows:
        raise HTTPException(status_code=422, detail={"message": diagnostics.get("reason") or "No plan rows generated.", "diagnostics": diagnostics})
    return build_response(req, rows, diagnostics, repo, store, plan_id=plan_id)


@app.get("/api/media-plan/{plan_id}")
def get_media_plan(plan_id: str, store: PlanStore = Depends(get_store)):
    plan = store.get(plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="plan not found")
    return plan


app.mount("/static", StaticFiles(directory="."), name="static")
