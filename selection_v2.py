"""Slot selection v2 — prototype of the improved logic (isolated, additive).

Incorporates the factors requested for better slot selection, all off the existing data
sources (forecasting = impressions, booking = already booked, rate card = CPD+CPM prices,
slot catalog = page/zone/category/dimension):

  1. eCPM deprioritisation — a CPD slot's effective CPM = cpd_rate x days / available x 1000.
     Within a placement group (country/page/zone/category) a CPD slot is dropped when a
     comparable CPM slot is cheaper per 1000 impressions.
  2. Budget utilisation — cascade through tiers and fill slots so a good share of budget is
     actually consumed (replaces the earlier RoAS underspend trim).
  3. Fill-rate — book >=60% of a slot's available impressions (target >=90%); if the budget
     can't reach 60% on a big slot, fall to a smaller slot in the group.
  4. Tiering — slots are tiered by visibility (Hero/Top-Fold ... Category page). The starting
     tier is set by budget; reach vs RoAS weighting tilts which tier/slot-type is preferred.
  5. One slot per page/zone (already enforced by the base planner; kept here explicitly).
  6. Weighting — reach-heavy -> high-impression/visibility slots; RoAS-heavy -> the matching
     category-page (CLP) slots.

This is a self-contained prototype used to produce the sample plans in the design spec; it
does NOT replace planner.plan_media yet. It reuses build_candidates for scoring + metadata.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from models import EditablePlanLine, MediaPlanRequest
from planner import (
    build_candidates,
    expand_candidates_for_countries,
    get_slot_meta,
    inclusive_days,
    normalize_objective,
    _slot_relevance_for_comcat,
)

# ── Tiering ──────────────────────────────────────────────────────────────────
# Tier 1 = premium high-visibility; Tier 3 = targeted / category page.
TIER_PREMIUM, TIER_MID, TIER_TARGETED = 1, 2, 3
TIER_LABEL = {1: "T1 · Premium visibility", 2: "T2 · Mid", 3: "T3 · Targeted / category"}

_T1_PATTERNS = re.compile(r"hero|top[\s_-]*fold|masthead|hp[\s_-]*sfu1|home[\s_-]*page.*(hero|banner)|super.?mall.*home", re.I)
_T3_PATTERNS = re.compile(r"\bclp\b|\bplp\b|\bpdp\b|category|salepage|order[\s_-]*conf|checkout|search|presearch", re.I)

# Illustrative budget -> starting tier bands (USD). Tunable; refined by the tiering report.
BUDGET_TIER_BANDS = [(40000, TIER_PREMIUM), (12000, TIER_MID), (0, TIER_TARGETED)]

# eCPM deprioritisation (NOT removal): a slot sinks to the bottom of its tier when its eCPM
# exceeds a comparable CPM slot in its placement group by this margin, OR when it breaches the
# global benchmark. Deprioritised slots stay eligible and are booked only if budget remains.
ECPM_MARGIN = 0.10
GLOBAL_ECPM_BENCHMARK = 20.0  # eCPM above this ($) is deprioritised regardless of group
FILL_TARGET = 0.90
FILL_MIN = 0.60


def _num(v, d=0.0) -> float:
    try:
        return float(v) if v not in (None, "") else d
    except (TypeError, ValueError):
        return d


def assign_tier(available: int, slot_name: str, page: str) -> int:
    text = f"{slot_name or ''} {page or ''}"
    if _T1_PATTERNS.search(text):
        return TIER_PREMIUM
    if _T3_PATTERNS.search(text):
        return TIER_TARGETED
    # size fallback: very large forecast reads as premium, tiny as targeted
    if available >= 500_000:
        return TIER_PREMIUM
    if available < 60_000:
        return TIER_TARGETED
    return TIER_MID


def starting_tier(budget: float) -> int:
    for threshold, tier in BUDGET_TIER_BANDS:
        if budget >= threshold:
            return tier
    return TIER_TARGETED


def _placement_group(s: dict) -> tuple:
    return (s["country"], (s["page"] or "").lower(), (s["zone"] or "").lower(), (s["category"] or "").lower())


def enrich(req: MediaPlanRequest, historical_rows, inventory_rows, slot_meta, settings) -> list[dict]:
    """One record per (country, slot) with forecast, both prices, eCPMs, tier and scores."""
    candidates = build_candidates(historical_rows, slot_meta, settings, req.marketplace)
    candidates = expand_candidates_for_countries(req, candidates, slot_meta, settings)

    available_by_slot: dict[tuple, int] = defaultdict(int)
    for row in inventory_rows:
        key = (row.get("country"), str(row.get("slot_code") or "").strip().lower())
        available_by_slot[key] += max(int(row.get("available_views") or 0), 0)

    days = inclusive_days(req.start_date, req.end_date)
    best: dict[tuple, dict] = {}
    for c in candidates:
        key = (c.country, c.slot_code)
        meta = get_slot_meta(slot_meta, c.country, c.slot_code)
        available = available_by_slot.get((c.country, str(c.slot_code or "").strip().lower()), 0)
        if available <= 0:
            continue
        cpm_rate = _num(meta.get("cpm_rate"))
        cpd_rate = _num(meta.get("cpd_rate"))
        ecpm_cpm = cpm_rate if cpm_rate > 0 else None
        ecpm_cpd = round(cpd_rate * days / max(available, 1) * 1000, 2) if cpd_rate > 0 else None
        record = {
            "country": c.country,
            "slot_code": c.slot_code,
            "slot_name": c.slot_name or c.slot_code,
            "page": c.page or c.category or "",
            "category": c.category or "",
            "zone": c.zone or "",
            "marketplace": c.marketplace,
            "available": int(available),
            "cpm_rate": round(cpm_rate, 2),
            "cpd_rate": round(cpd_rate, 2),
            "ecpm_cpm": round(ecpm_cpm, 2) if ecpm_cpm else None,
            "ecpm_cpd": ecpm_cpd,
            "roas": round(_num(c.roas), 2) if c.roas is not None else None,
            "ctr": c.ctr,
            "roas_score": c.roas_score,
            "visibility_score": c.visibility_score,
            "confidence": c.confidence_score,
            "tier": assign_tier(int(available), c.slot_name or "", c.page or c.category or ""),
        }
        # keep the strongest scoring candidate per (country, slot)
        prev = best.get(key)
        if prev is None or record["roas_score"] > prev["roas_score"]:
            best[key] = record
    return list(best.values())


def flag_ecpm(slots: list[dict], margin: float = ECPM_MARGIN, benchmark: float = GLOBAL_ECPM_BENCHMARK) -> tuple[list[dict], list[dict]]:
    """Pick each slot's cheaper buy type, then DEPRIORITISE (never drop) the pricey ones:
    a slot is flagged when its effective eCPM exceeds a comparable CPM slot in its placement
    group by `margin`, or when it breaches the global `benchmark`. Flagged slots stay in the
    pool but sink to the bottom of the ranking (see _order_key). Returns (all_slots, flagged)."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for s in slots:
        groups[_placement_group(s)].append(s)

    flagged: list[dict] = []
    for members in groups.values():
        cpm_ecpms = [s["ecpm_cpm"] for s in members if s.get("ecpm_cpm")]
        cheapest_cpm = min(cpm_ecpms) if cpm_ecpms else None
        for s in members:
            has_cpm, has_cpd = bool(s.get("ecpm_cpm")), bool(s.get("ecpm_cpd"))
            # choose the lower-eCPM buy type available
            if has_cpm and has_cpd:
                s["buy_type"] = "CPM" if s["ecpm_cpm"] <= s["ecpm_cpd"] else "CPD"
            else:
                s["buy_type"] = "CPD" if has_cpd else "CPM"
            eff = s["ecpm_cpm"] if s["buy_type"] == "CPM" else s["ecpm_cpd"]
            reasons = []
            if eff and eff > benchmark:
                reasons.append(f"eCPM ${eff:.0f} > ${benchmark:.0f} benchmark")
            if s["buy_type"] == "CPD" and cheapest_cpm and s.get("ecpm_cpd") and s["ecpm_cpd"] > cheapest_cpm * (1 + margin):
                reasons.append(f"pricier than group CPM ${cheapest_cpm:.0f} (+{int(margin*100)}%)")
            if reasons:
                s["deprioritized"] = True
                s["deprioritize_reason"] = "; ".join(reasons)
                flagged.append(s)
    return slots, flagged


def _weighting(req: MediaPlanRequest) -> float:
    """0 = pure reach tilt, 1 = pure RoAS tilt."""
    if normalize_objective(req.objective) == "roas":
        return 1.0
    if normalize_objective(req.objective) == "visibility":
        return 0.0
    reach = max(int(getattr(req, "reach_weight", 60) or 0), 0)
    roas = max(int(getattr(req, "roas_weight", 40) or 0), 0)
    return roas / (reach + roas) if (reach + roas) else 0.5


def _order_key(s: dict, req: MediaPlanRequest, tilt: float):
    """Reach-tilt favours bigger/high-visibility slots; RoAS-tilt favours the matching
    category-page slots and higher RoAS. eCPM is the efficiency tiebreak (cheaper first)."""
    comcat_bonus = 0.0
    if tilt >= 0.5 and req.comcats:
        comcat_bonus = 0.15 * max((_slot_relevance_for_comcat_dict(s, cc) for cc in req.comcats), default=0.0)
    # RoAS component ranks directly by the (page × comcat) weighted RoAS — normalised so 8x+
    # maps to 1.0 — rather than a slot-level confidence-dragged score. The pooled figure is
    # already robust, so we let plan building follow it straight.
    roas_norm = min((s.get("roas") or 0.0) / 8.0, 1.0)
    reach_norm = min(s["available"] / 2_000_000, 1.0) + (0.15 if s["tier"] == 1 else 0.0)
    score = tilt * (roas_norm + comcat_bonus) + (1 - tilt) * reach_norm
    eff = s.get("ecpm_cpm") or s.get("ecpm_cpd") or 9999
    # deprioritised (high-eCPM) slots sort below everything else in the tier — booked only
    # if budget still remains once the efficient inventory is exhausted.
    not_deprioritized = 0 if s.get("deprioritized") else 1
    return (not_deprioritized, round(score, 4), -eff)


def _slot_relevance_for_comcat_dict(s: dict, comcat: str) -> float:
    from .planner import _slot_values_relevance_for_comcat
    return _slot_values_relevance_for_comcat(s.get("category"), s.get("page"), s.get("slot_code"), s.get("slot_name"), comcat)


def plan_media_v2(req: MediaPlanRequest, historical_rows, inventory_rows, slot_meta, settings) -> tuple[list[dict], dict]:
    days = inclusive_days(req.start_date, req.end_date)
    budget = float(req.budget or 0)
    slots = enrich(req, historical_rows, inventory_rows, slot_meta, settings)
    slots, flagged = flag_ecpm(slots)
    tilt = _weighting(req)
    start_tier = starting_tier(budget)

    used_page_zone: set = set()
    used_page: set = set()
    rows: list[dict] = []
    remaining = budget
    fill_notes: list[str] = []

    def try_book(s: dict, strict_page: bool = False, relax: bool = False) -> bool:
        nonlocal remaining
        page_key = (s["country"], (s["page"] or "").lower())
        pz = (s["country"], (s["page"] or "").lower(), (s["zone"] or "").lower())
        if pz in used_page_zone:
            return False  # never two of the same page+zone
        if strict_page and page_key in used_page:
            return False  # pass A: one slot per page
        available = s["available"]
        buy = s.get("buy_type", "CPM")
        if buy == "CPD":
            cost = round(s["cpd_rate"] * days, 2)
            if cost > remaining:
                return False  # can't afford whole-slot CPD → try a cheaper/smaller slot
            book_views, fill = available, 1.0
        else:
            rate = s["cpm_rate"]
            if rate <= 0:
                return False
            book_views = int(min(available, remaining / rate * 1000))
            fill = book_views / available if available else 0.0
            floor = FILL_MIN * (0.5 if relax else 1.0)  # relax the fill floor only on the top-up pass
            if fill < floor:
                if not relax:
                    fill_notes.append(f"skip {s['slot_name']} — budget fills only {fill*100:.0f}% (<60%)")
                return False  # move to a lower-impression slot that we can fill
            cost = round(book_views * rate / 1000, 2)
        rows.append({
            **{k: s[k] for k in ("country", "slot_code", "slot_name", "page", "zone", "category", "marketplace", "available", "roas", "tier")},
            "tier_label": TIER_LABEL[s["tier"]],
            "buy_type": buy,
            "rate": s["cpm_rate"] if buy == "CPM" else s["cpd_rate"],
            "ecpm": s.get("ecpm_cpm") if buy == "CPM" else s.get("ecpm_cpd"),
            "booked_views": int(book_views),
            "fill_pct": round(fill * 100, 1),
            "cost": cost,
            "deprioritized": bool(s.get("deprioritized")),
        })
        remaining = round(remaining - cost, 2)
        used_page_zone.add(pz)
        used_page.add(page_key)
        return True

    # Cascade through tiers: the budget's starting tier first, then outward (so a small
    # budget starting at T3 can still spend into T2, and a big budget cascades T1→T2→T3).
    tier_order = sorted((TIER_PREMIUM, TIER_MID, TIER_TARGETED), key=lambda t: (abs(t - start_tier), t))

    def cascade(strict_page: bool, stop_frac: float) -> None:
        for tier in tier_order:
            if remaining <= budget * stop_frac:
                return
            for s in sorted([x for x in slots if x["tier"] == tier], key=lambda x: _order_key(x, req, tilt), reverse=True):
                if remaining <= budget * stop_frac:
                    return
                try_book(s, strict_page=strict_page)

    cascade(strict_page=True, stop_frac=0.02)          # Pass A — one slot per page
    if remaining > budget * 0.05:
        cascade(strict_page=False, stop_frac=0.02)     # Pass B — relax to one per (page, zone)
    if remaining > budget * 0.10:                      # Top-up — relax the fill floor to consume budget
        for s in sorted(slots, key=lambda x: _order_key(x, req, tilt), reverse=True):
            if remaining <= budget * 0.05:
                break
            try_book(s, strict_page=False, relax=True)

    allocated = round(budget - remaining, 2)
    on_deck_rev = sum((_num(r.get("roas")) * r["cost"]) for r in rows)
    diagnostics = {
        "budget": round(budget),
        "allocated": round(allocated),
        "utilization_pct": round(allocated / budget * 100, 1) if budget else 0.0,
        "line_count": len(rows),
        "start_tier": TIER_LABEL[start_tier],
        "weighting_tilt": round(tilt, 2),
        "blended_roas": round(on_deck_rev / allocated, 2) if allocated else 0.0,
        "avg_fill_pct": round(sum(r["fill_pct"] for r in rows) / len(rows), 1) if rows else 0.0,
        "tier_breakdown": {TIER_LABEL[t]: round(sum(r["cost"] for r in rows if r["tier"] == t)) for t in (1, 2, 3)},
        "distinct_pages": len({(r["country"], (r["page"] or "").lower()) for r in rows}),
        "deprioritized_count": len(flagged),
        "deprioritized_booked": sum(1 for r in rows if r.get("deprioritized")),
        "deprioritized_examples": [
            {"slot": s["slot_name"], "buy_type": s.get("buy_type"),
             "ecpm": s["ecpm_cpm"] if s.get("buy_type") == "CPM" else s.get("ecpm_cpd"),
             "reason": s.get("deprioritize_reason")}
            for s in flagged[:8]
        ],
        "fill_notes": fill_notes[:8],
    }
    return rows, diagnostics


def to_editable_rows(v2_rows: list[dict], req: MediaPlanRequest) -> list[EditablePlanLine]:
    """Convert v2 selection output into the standard EditablePlanLine shape so the existing
    planner UI (and build_response / AI read / save) renders a V2 plan unchanged. Single
    full-flight phase; discounts are not yet modelled in v2 (follow-up)."""
    days = inclusive_days(req.start_date, req.end_date)
    discount = float(getattr(req, "discount_pct", 0) or 0)
    factor = max(1 - discount / 100, 0.0001)
    tilt = _weighting(req)
    default_stype = "conv" if tilt >= 0.5 else "reach"
    out: list[EditablePlanLine] = []
    for i, r in enumerate(v2_rows, 1):
        net = round(float(r.get("cost") or 0), 2)
        buy = r.get("buy_type", "CPM")
        ecpm = float(r.get("ecpm") or 0)
        out.append(EditablePlanLine.model_validate({
            "id": i,
            "from": req.start_date,
            "to": req.end_date,
            "country": r.get("country", ""),
            "page": r.get("page", "") or r.get("category", ""),
            "marketplace": r.get("marketplace", ""),
            "category": r.get("category", ""),
            "zone": r.get("zone", ""),
            "dimension": "",
            "asset": r.get("slot_name", ""),
            "slot_name": r.get("slot_name", ""),
            "days": days,
            "buyType": buy,
            "rate": round(ecpm if buy == "CPM" else float(r.get("rate") or 0), 4),
            "gross_cpm": round(ecpm / factor, 4) if buy == "CPM" else 0.0,
            "net_cpm": round(ecpm, 4) if buy == "CPM" else 0.0,
            "views": int(r.get("booked_views") or 0),
            "cost": net,
            "gross_amount": round(net / factor, 2),
            "net_amount": net,
            "discount_pct": discount,
            "phase": "Full flight",
            "brand": req.brand,
            "stype": default_stype,
            "slot_code": r.get("slot_code", ""),
            "score": round(min(float(r.get("roas") or 0) / 10.0, 1.0), 3),
            "available_views": int(r.get("available") or 0),
            "historical_ctr": None,
            "historical_roas": r.get("roas"),
            "historical_cpm": None,
            "note": "Deprioritised (high eCPM)" if r.get("deprioritized") else "",
            "manual": False,
            "locked": False,
        }))
    return out
