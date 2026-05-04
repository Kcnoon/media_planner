from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from math import isfinite

from models import MediaPlanRequest, Phase, EditablePlanLine


@dataclass(frozen=True)
class Candidate:
    country: str
    slot_code: str
    slot_name: str | None
    page: str | None
    publisher: str | None
    pricing_model: str
    views: int
    clicks: int
    revenue: float
    spends: float
    active_days: int
    brand_specific: bool
    reach_score: float
    conv_score: float
    cpm: float | None
    cpd: float | None
    ctr: float | None
    roas: float | None


def inclusive_days(start: date, end: date) -> int:
    return max((end - start).days + 1, 1)


def normalize_pricing_model(value: str | None) -> str:
    raw = (value or "").strip().lower()
    if raw in {"cpd", "cost per day", "cost_per_day", "day"}:
        return "CPD"
    return "CPM"


def is_supermall(slot_code: str, slot_name: str | None) -> bool:
    text = f"{slot_name or ''} {slot_code}".lower()
    return "supermall" in text or "super_mall" in text


def page_from_slot(slot_code: str, publisher: str | None, explicit_page: str | None = None) -> str:
    if explicit_page:
        return explicit_page
    text = f"{publisher or ''} {slot_code}".lower()
    if "clp" in text or "category" in text:
        return "CLP"
    if "pdp" in text:
        return "PDP"
    if "search" in text:
        return "Search"
    return publisher or "Home Page"


def asset_from_slot(slot_code: str, slot_name: str | None) -> str:
    return slot_name or slot_code.replace("_", " ").title()


def default_phases(req: MediaPlanRequest) -> list[Phase]:
    if req.phases:
        return req.phases
    return [Phase.model_validate({"name": "Full flight", "from": req.start_date, "to": req.end_date})]


def objective_weights(req: MediaPlanRequest) -> tuple[float, float]:
    if req.objective == "reach":
        return 1.0, 0.0
    if req.objective == "roas":
        return 0.0, 1.0
    reach = max(req.reach_weight, 0)
    roas = max(req.roas_weight, 0)
    total = reach + roas
    if total <= 0:
        return 0.6, 0.4
    return reach / total, roas / total


def build_candidates(historical_rows: list[dict], slot_meta: dict[tuple[str, str], dict], settings, marketplace: str | None) -> list[Candidate]:
    candidates: list[Candidate] = []
    for row in historical_rows:
        country = row.get("country") or ""
        slot_code = row.get("slot_code") or ""
        if not country or not slot_code:
            continue
        views = int(row.get("views") or 0)
        clicks = int(row.get("clicks") or 0)
        spends = float(row.get("spends") or 0)
        revenue = float(row.get("revenue") or 0)
        active_days = int(row.get("active_days") or 1)
        if views < settings.min_slot_views and spends <= 0:
            continue

        meta = slot_meta.get((country, slot_code), {})
        slot_name = meta.get("slot_name")
        if marketplace and marketplace != "both":
            sm = is_supermall(slot_code, slot_name)
            if marketplace == "supermall" and not sm:
                continue
            if marketplace == "core" and sm:
                continue

        cpm = spends * 1000 / views if views > 0 and spends > 0 else None
        ctr = clicks / views if views > 0 else None
        roas = revenue / spends if spends > 0 else None
        cpd = spends / active_days if active_days > 0 and spends > 0 else None

        safe_cpm = cpm if cpm and isfinite(cpm) and cpm > 0 else settings.default_cpm
        reach_score = views / safe_cpm
        conv_score = (roas or 0) * 100000 + (ctr or 0) * 1000 + revenue / max(spends, 1)
        if row.get("brand_specific"):
            reach_score *= 1.15
            conv_score *= 1.2

        candidates.append(
            Candidate(
                country=country,
                slot_code=slot_code,
                slot_name=slot_name,
                page=meta.get("page"),
                publisher=meta.get("publisher") or row.get("publisher"),
                pricing_model=normalize_pricing_model(meta.get("pricing_model") or row.get("pricing_model")),
                views=views,
                clicks=clicks,
                revenue=revenue,
                spends=spends,
                active_days=active_days,
                brand_specific=bool(row.get("brand_specific")),
                reach_score=reach_score,
                conv_score=conv_score,
                cpm=cpm,
                cpd=cpd,
                ctr=ctr,
                roas=roas,
            )
        )

    deduped: dict[tuple[str, str, str], Candidate] = {}
    for candidate in sorted(candidates, key=lambda c: (c.brand_specific, c.views), reverse=True):
        deduped.setdefault((candidate.country, candidate.slot_code, candidate.pricing_model), candidate)
    return list(deduped.values())


def _coerce_rows(req: MediaPlanRequest) -> list[EditablePlanLine]:
    coerced: list[EditablePlanLine] = []
    for r in req.current_rows:
        if not getattr(r, "locked", False) and not getattr(r, "manual", False):
            continue
        payload = r.model_dump(by_alias=True) if hasattr(r, "model_dump") else r
        coerced.append(EditablePlanLine.model_validate(payload))
    return coerced


def _per_country_min_lines(req: MediaPlanRequest) -> int:
    # Budget is entered in USD.
    return 6 if req.budget < 10000 else 10


def _inventory_by_slot_phase(req: MediaPlanRequest, inventory_rows: list[dict]) -> dict[tuple[str, str, str], int]:
    inventory_by_slot_phase: dict[tuple[str, str, str], int] = defaultdict(int)
    for row in inventory_rows:
        dt = row["dt"]
        country = row["country"]
        slot = row["slot_code"]
        available = max(int(row.get("available_views") or 0), 0)
        for phase in default_phases(req):
            if phase.from_date <= dt <= phase.to_date:
                inventory_by_slot_phase[(country, slot, phase.name)] += available
    return inventory_by_slot_phase


def _candidate_score(candidate: Candidate, objective: str) -> float:
    if objective == "reach":
        return candidate.reach_score
    if objective == "roas":
        return candidate.conv_score
    return max(candidate.reach_score, candidate.conv_score)


def suggest_slots(
    req: MediaPlanRequest,
    historical_rows: list[dict],
    inventory_rows: list[dict],
    slot_meta: dict[tuple[str, str], dict],
    settings,
    limit: int = 10,
) -> list[dict]:
    candidates = build_candidates(historical_rows, slot_meta, settings, req.marketplace)
    inventory = _inventory_by_slot_phase(req, inventory_rows)
    phases = default_phases(req)
    seen: set[str] = set()
    suggestions: list[dict] = []

    ranked = sorted(candidates, key=lambda candidate: _candidate_score(candidate, req.objective), reverse=True)
    for candidate in ranked:
        slot_key = f"{candidate.country}|{candidate.slot_code}"
        if slot_key in seen:
            continue
        available = sum(inventory.get((candidate.country, candidate.slot_code, phase.name), 0) for phase in phases)
        if available <= 0:
            continue
        seen.add(slot_key)
        suggestions.append(
            {
                "slot_key": slot_key,
                "country": candidate.country,
                "slot_code": candidate.slot_code,
                "slot_name": candidate.slot_name or asset_from_slot(candidate.slot_code, candidate.slot_name),
                "page": page_from_slot(candidate.slot_code, candidate.publisher, candidate.page),
                "pricing_model": candidate.pricing_model,
                "available_views": available,
                "historical_cpm": candidate.cpm,
                "historical_ctr": candidate.ctr,
                "historical_roas": candidate.roas,
                "score": round(_candidate_score(candidate, req.objective), 4),
            }
        )
        if len(suggestions) >= limit:
            break
    return suggestions


def plan_media(
    req: MediaPlanRequest,
    historical_rows: list[dict],
    inventory_rows: list[dict],
    slot_meta: dict[tuple[str, str], dict],
    settings,
) -> tuple[list[EditablePlanLine], dict]:
    candidates = build_candidates(historical_rows, slot_meta, settings, req.marketplace)
    if not candidates:
        return [], {"reason": "No historical delivery rows found for the selected brand/comcat/countries."}

    inventory_by_slot_phase = _inventory_by_slot_phase(req, inventory_rows)

    phases = default_phases(req)
    reach_weight, roas_weight = objective_weights(req)

    rows = _coerce_rows(req)
    spent_total = sum(row.cost for row in rows)
    line_id = max([row.id for row in rows], default=0) + 1

    excluded_slot_keys = set(req.excluded_slot_keys) | {f"{row.country}|{row.slot_code}" for row in rows if row.slot_code}
    selected_slot_keys = {value for value in req.selected_slot_keys if value}
    foc_slot_keys = {value for value in req.foc_slot_keys if value}

    countries = [c for c in req.countries if c]
    if not countries:
        return [], {"reason": "No countries selected."}

    # We split budget evenly across selected countries for planning.
    country_budget = req.budget / len(countries)
    spent_by_country: dict[str, float] = defaultdict(float)
    for row in rows:
        spent_by_country[row.country] += float(row.cost or 0)

    # Primary allocation pass: per-country, per-phase, per-objective.
    for country in countries:
        country_candidates = [c for c in candidates if c.country == country]
        if selected_slot_keys:
            country_candidates = [candidate for candidate in country_candidates if f"{candidate.country}|{candidate.slot_code}" in selected_slot_keys]
        if not country_candidates:
            continue

        country_spent = spent_by_country.get(country, 0.0)
        for phase in phases:
            phase_days = inclusive_days(phase.from_date, phase.to_date)
            total_days = sum(inclusive_days(p.from_date, p.to_date) for p in phases)
            phase_share = phase_days / max(total_days, 1)

            for stype, weight, score_name in (
                ("reach", reach_weight, "reach_score"),
                ("conv", roas_weight, "conv_score"),
            ):
                target = country_budget * phase_share * weight
                if target <= 0:
                    continue

                already = sum(
                    row.cost
                    for row in rows
                    if row.country == country and row.phase == phase.name and row.stype == stype
                )
                remaining_target = max(target - already, 0)
                ranked = sorted(country_candidates, key=lambda c: getattr(c, score_name), reverse=True)
                used_in_phase = 0

                for candidate in ranked:
                    if used_in_phase >= settings.max_lines_per_phase or remaining_target <= 1:
                        break

                    slot_key = f"{candidate.country}|{candidate.slot_code}"
                    if slot_key in excluded_slot_keys:
                        continue

                    exact_inventory_key = (candidate.country, candidate.slot_code, phase.name)
                    available = inventory_by_slot_phase.get(exact_inventory_key, 0)
                    if available <= 0:
                        continue

                    buy_type = "Cost Per Day" if candidate.pricing_model == "CPD" else "CPM"
                    days = inclusive_days(phase.from_date, phase.to_date)
                    slot_key = f"{candidate.country}|{candidate.slot_code}"
                    is_foc = slot_key in foc_slot_keys

                    if buy_type == "CPM":
                        rate = round(candidate.cpm or settings.default_cpm, 2)
                        planned_views = int(
                            min(
                                available,
                                max(
                                    remaining_target * 1000 / max(rate, 0.01),
                                    settings.min_slot_views,
                                ),
                            )
                        )
                        if planned_views < settings.min_slot_views:
                            continue
                        cost = round(planned_views * rate / 1000, 2)
                    else:
                        rate = round(candidate.cpd or settings.default_cpd, 2)
                        max_days = min(days, max(int(max(remaining_target, rate) // max(rate, 0.01)), 1))
                        cost = round(rate * max_days, 2)
                        planned_views = (
                            min(available, int(candidate.views / max(candidate.active_days, 1) * max_days))
                            or None
                        )

                    # Don't meaningfully exceed total budget.
                    if is_foc:
                        cost = 0.0
                    elif spent_total + cost > req.budget * 1.01:
                        cost = max(req.budget - spent_total, 0)
                        if cost <= 1:
                            break
                        if buy_type == "CPM":
                            planned_views = int(cost * 1000 / max(rate, 0.01))

                    rows.append(
                        EditablePlanLine.model_validate(
                            {
                                "id": line_id,
                                "from": phase.from_date,
                                "to": phase.to_date,
                                "country": candidate.country,
                                "page": page_from_slot(candidate.slot_code, candidate.publisher, candidate.page),
                                "asset": asset_from_slot(candidate.slot_code, candidate.slot_name),
                                "days": days,
                                "buyType": buy_type,
                                "rate": rate,
                                "views": planned_views,
                                "cost": round(cost, 2),
                                "phase": phase.name,
                                "brand": req.brand,
                                "stype": stype,
                                "slot_code": candidate.slot_code,
                                "score": round(getattr(candidate, score_name), 4),
                                "available_views": available,
                                "historical_ctr": candidate.ctr,
                                "historical_roas": candidate.roas,
                                "historical_cpm": candidate.cpm,
                                "note": "",
                                "manual": False,
                                "locked": False,
                            }
                        )
                    )

                    line_id += 1
                    spent_total += cost
                    country_spent += cost
                    remaining_target -= cost
                    excluded_slot_keys.add(slot_key)

                    consumed = int(planned_views or 0)
                    inventory_by_slot_phase[exact_inventory_key] = max(
                        inventory_by_slot_phase[exact_inventory_key] - consumed,
                        0,
                    )
                    used_in_phase += 1

    # Enforce minimum line count per country (soft goal, constrained by inventory/candidates).
    per_country_min = _per_country_min_lines(req)
    for country in countries:
        have = len([r for r in rows if r.country == country and (r.slot_code or "").strip()])
        if have >= per_country_min:
            continue

        country_candidates = [c for c in candidates if c.country == country]
        if selected_slot_keys:
            country_candidates = [candidate for candidate in country_candidates if f"{candidate.country}|{candidate.slot_code}" in selected_slot_keys]
        ranked = sorted(country_candidates, key=lambda c: max(c.reach_score, c.conv_score), reverse=True)
        phase = phases[0]
        for candidate in ranked:
            if have >= per_country_min:
                break
            if spent_total >= req.budget - 1:
                break

            slot_key = f"{candidate.country}|{candidate.slot_code}"
            if slot_key in excluded_slot_keys:
                continue

            available = inventory_by_slot_phase.get((candidate.country, candidate.slot_code, phase.name), 0)
            if available < settings.min_slot_views:
                continue

            rate = round(candidate.cpm or settings.default_cpm, 2)
            planned_views = min(available, max(settings.min_slot_views, int((req.budget - spent_total) * 1000 / max(rate, 0.01) / max((per_country_min - have), 1))))
            slot_key = f"{candidate.country}|{candidate.slot_code}"
            cost = 0.0 if slot_key in foc_slot_keys else round(planned_views * rate / 1000, 2)

            rows.append(
                EditablePlanLine.model_validate(
                    {
                        "id": line_id,
                        "from": phase.from_date,
                        "to": phase.to_date,
                        "country": candidate.country,
                        "page": page_from_slot(candidate.slot_code, candidate.publisher, candidate.page),
                        "asset": asset_from_slot(candidate.slot_code, candidate.slot_name),
                        "days": inclusive_days(phase.from_date, phase.to_date),
                        "buyType": "CPM",
                        "rate": rate,
                        "views": int(planned_views),
                        "cost": cost,
                        "phase": phase.name,
                        "brand": req.brand,
                        "stype": "reach" if candidate.reach_score >= candidate.conv_score else "conv",
                        "slot_code": candidate.slot_code,
                        "score": round(max(candidate.reach_score, candidate.conv_score), 4),
                        "available_views": available,
                        "historical_ctr": candidate.ctr,
                        "historical_roas": candidate.roas,
                        "historical_cpm": candidate.cpm,
                        "note": "",
                        "manual": False,
                        "locked": False,
                    }
                )
            )
            line_id += 1
            spent_total += cost
            excluded_slot_keys.add(slot_key)
            have += 1

    # If the user explicitly selected slots in preselection, make sure each selected
    # country-slot appears in the final plan even when ranking/allocation didn't pick it.
    existing_slot_keys = {f"{row.country}|{row.slot_code}" for row in rows if row.slot_code}
    candidate_by_slot_key = {f"{candidate.country}|{candidate.slot_code}": candidate for candidate in candidates}
    missing_selected = [slot_key for slot_key in selected_slot_keys if slot_key not in existing_slot_keys]

    for index, slot_key in enumerate(missing_selected):
        try:
            country, slot_code = slot_key.split("|", 1)
        except ValueError:
            continue

        candidate = candidate_by_slot_key.get(slot_key)
        phase = phases[min(index, len(phases) - 1)]
        available = inventory_by_slot_phase.get((country, slot_code, phase.name), 0)
        if available <= 0:
            continue

        buy_type = "Cost Per Day" if candidate and candidate.pricing_model == "CPD" else "CPM"
        days = inclusive_days(phase.from_date, phase.to_date)
        remaining_slots = max(len(missing_selected) - index, 1)
        budget_share = max(req.budget - spent_total, 0) / remaining_slots

        if buy_type == "CPM":
            rate = round((candidate.cpm if candidate and candidate.cpm else settings.default_cpm), 2)
            planned_views = int(
                min(
                    available,
                    max(settings.min_slot_views, int(budget_share * 1000 / max(rate, 0.01))),
                )
            )
            cost = 0.0 if slot_key in foc_slot_keys else round(planned_views * rate / 1000, 2)
        else:
            rate = round((candidate.cpd if candidate and candidate.cpd else settings.default_cpd), 2)
            max_days = min(days, max(int(max(budget_share, rate) // max(rate, 0.01)), 1))
            planned_views = (
                min(
                    available,
                    int((candidate.views if candidate else settings.min_slot_views) / max((candidate.active_days if candidate else 1), 1) * max_days),
                )
                or settings.min_slot_views
            )
            cost = 0.0 if slot_key in foc_slot_keys else round(rate * max_days, 2)

        slot_info = slot_meta.get((country, slot_code), {})
        selected_stype = "reach"
        if candidate and candidate.conv_score > candidate.reach_score:
            selected_stype = "conv"
        elif req.objective == "roas":
            selected_stype = "conv"

        rows.append(
            EditablePlanLine.model_validate(
                {
                    "id": line_id,
                    "from": phase.from_date,
                    "to": phase.to_date,
                    "country": country,
                    "page": page_from_slot(slot_code, slot_info.get("publisher"), slot_info.get("page")),
                    "asset": asset_from_slot(slot_code, slot_info.get("slot_name")),
                    "days": days,
                    "buyType": buy_type,
                    "rate": rate,
                    "views": int(planned_views),
                    "cost": round(cost, 2),
                    "phase": phase.name,
                    "brand": req.brand,
                    "stype": selected_stype,
                    "slot_code": slot_code,
                    "score": round(_candidate_score(candidate, req.objective), 4) if candidate else 0.0,
                    "available_views": available,
                    "historical_ctr": candidate.ctr if candidate else None,
                    "historical_roas": candidate.roas if candidate else None,
                    "historical_cpm": candidate.cpm if candidate else None,
                    "note": "FOC selected slot" if slot_key in foc_slot_keys else "",
                    "manual": False,
                    "locked": False,
                }
            )
        )
        line_id += 1
        spent_total += cost

    diagnostics = {
        "candidate_count": len(candidates),
        "historical_rows": len(historical_rows),
        "inventory_rows": len(inventory_rows),
        "inventory_total_available": sum(max(int(row.get("available_views") or 0), 0) for row in inventory_rows),
        "allocated": round(sum(row.cost for row in rows), 2),
        "per_country_min": per_country_min,
        "countries": countries,
        "marketplace": req.marketplace,
    }

    return rows, diagnostics
