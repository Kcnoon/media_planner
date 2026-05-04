from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, Field


Objective = Literal["reach", "roas", "both"]
BrandTag = Literal["old", "new"]
Marketplace = Literal["core", "supermall", "both"]


class Phase(BaseModel):
    name: str
    from_date: date = Field(alias="from")
    to_date: date = Field(alias="to")


class EditablePlanLine(BaseModel):
    id: int
    from_date: date = Field(alias="from")
    to_date: date = Field(alias="to")
    country: str
    page: str
    asset: str
    days: int
    buyType: str
    rate: float
    views: Optional[int]
    cost: float
    phase: str
    brand: str
    stype: Literal["reach", "conv"]
    slot_code: str
    score: float = 0.0
    available_views: int = 0
    historical_ctr: Optional[float] = None
    historical_roas: Optional[float] = None
    historical_cpm: Optional[float] = None
    note: str = ""
    manual: bool = False
    locked: bool = False


class MediaPlanRequest(BaseModel):
    brand: str
    brand_tag: BrandTag
    comcats: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)

    marketplace: Marketplace = "both"
    sub_brands: list[str] = Field(default_factory=list)

    start_date: date
    end_date: date

    # Budget is entered in USD. Currency is used for display only.
    budget: float
    currency: str
    budget_locked: bool = False

    le_code: Optional[str] = None
    notes: Optional[str] = None

    objective: Objective
    reach_weight: int = 60
    roas_weight: int = 40
    phases: list[Phase] = Field(default_factory=list)
    selected_slot_keys: list[str] = Field(default_factory=list)
    foc_slot_keys: list[str] = Field(default_factory=list)

    plan_id: Optional[str] = None
    current_rows: list[EditablePlanLine] = Field(default_factory=list)
    excluded_slot_keys: list[str] = Field(default_factory=list)


class MediaPlanResponse(BaseModel):
    rows: list[EditablePlanLine]
    summary: dict
    diagnostics: dict
