from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from math import isfinite
import re

from models import MediaPlanRequest, Phase, EditablePlanLine


@dataclass(frozen=True)
class Candidate:
    country: str
    slot_code: str
    slot_name: str | None
    page: str | None
    category: str | None
    zone: str | None
    dimension: str | None
    marketplace: str
    publisher: str | None
    pricing_model: str
    slot_rate: float | None
    views: int
    clicks: int
    revenue: float
    spends: float
    active_days: int
    brand_specific: bool
    reach_score: float
    conv_score: float
    visibility_score: float
    ctr_score: float
    roas_score: float
    brand_score: float
    comcat_score: float
    trend_score: float
    confidence_score: float
    final_score: float
    cpm: float | None
    cpd: float | None
    ctr: float | None
    roas: float | None
    source_country: str | None = None
    synthetic: bool = False
    explainability: tuple[str, ...] = ()


def inclusive_days(start: date, end: date) -> int:
    return max((end - start).days + 1, 1)


def normalize_pricing_model(value: str | None) -> str:
    raw = (value or "").strip().lower()
    if raw in {"cpd", "cost per day", "cost_per_day", "day"}:
        return "CPD"
    return "CPM"


def slot_code_key(value: str | None) -> str:
    return str(value or "").strip().lower()


def inferred_slot_pricing_model(slot_code: str | None, slot_name: str | None = None) -> str:
    text = f"{slot_code or ''} {slot_name or ''}".lower()
    if "cpd" in text or "cost per day" in text or "cost_per_day" in text:
        return "CPD"
    return "CPM"


def pricing_options_for_meta(meta: dict | None) -> list[str]:
    meta = meta or {}
    raw_options = meta.get("pricing_options") or []
    options = [normalize_pricing_model(option) for option in raw_options if str(option or "").strip()]
    if not options and meta.get("pricing_model"):
        options = [normalize_pricing_model(meta.get("pricing_model"))]
    if not options:
        if float(meta.get("cpm_rate") or 0) > 0:
            options.append("CPM")
        if float(meta.get("cpd_rate") or 0) > 0:
            options.append("CPD")
    if not options:
        options = [normalize_pricing_model(meta.get("pricing_model"))]
    return list(dict.fromkeys(options))


def slot_key(country: str, slot_code: str) -> str:
    return f"{country}|{slot_code}"


def get_slot_meta(slot_meta: dict[tuple[str, str], dict], country: str, slot_code: str | None) -> dict:
    if not country or not slot_code:
        return {}
    direct = slot_meta.get((country, slot_code))
    if direct:
        return direct
    normalized = slot_code_key(slot_code)
    for (meta_country, meta_slot_code), meta in slot_meta.items():
        if meta_country == country and slot_code_key(meta_slot_code) == normalized:
            return meta
    return {}


def selected_slot_pricing(req: MediaPlanRequest) -> dict[str, str]:
    return {
        str(key): normalize_pricing_model(value)
        for key, value in (req.selected_slot_pricing or {}).items()
        if str(key).strip() and str(value).strip()
    }


def slot_rate_for_model(meta: dict | None, model: str, fallback_rate: float | None = None) -> float | None:
    meta = meta or {}
    model = normalize_pricing_model(model)
    if model == "CPD":
        value = float(meta.get("cpd_rate") or 0) or fallback_rate
    else:
        value = float(meta.get("cpm_rate") or 0) or fallback_rate
    return float(value) if value and value > 0 else None


def slot_has_rate_for_model(
    meta: dict | None,
    model: str,
    start: date | None = None,
    end: date | None = None,
    default_rate: float | None = None,
) -> bool:
    meta = meta or {}
    model = normalize_pricing_model(model)
    schedule = meta.get("cpd_rate_schedule") if model == "CPD" else meta.get("cpm_rate_schedule") or meta.get("rate_schedule")
    if start and end and isinstance(schedule, dict):
        if any(float(schedule.get(dt.isoformat()) or 0) > 0 for dt in iter_dates(start, end)):
            return True
    if slot_rate_for_model(meta, model):
        return True
    return bool(default_rate and default_rate > 0)


def preferred_candidate_for_slot(candidates: list[Candidate], preferred_model: str | None, objective: str) -> Candidate | None:
    if not candidates:
        return None
    normalized_preference = normalize_pricing_model(preferred_model) if preferred_model else None
    if not normalized_preference and candidates:
        normalized_preference = inferred_slot_pricing_model(candidates[0].slot_code, candidates[0].slot_name)
    preferred = [candidate for candidate in candidates if not normalized_preference or candidate.pricing_model == normalized_preference]
    pool = preferred or candidates
    return max(
        pool,
        key=lambda candidate: (
            1 if normalized_preference and candidate.pricing_model == normalized_preference else 0,
            1 if candidate.pricing_model == "CPM" else 0,
            _candidate_score(candidate, objective),
            candidate.brand_specific,
            candidate.views,
        ),
    )


def is_supermall(slot_code: str, slot_name: str | None) -> bool:
    text = f"{slot_name or ''} {slot_code}".lower()
    return "supermall" in text or "super_mall" in text


def page_from_slot(slot_code: str, publisher: str | None, explicit_page: str | None = None) -> str:
    return (explicit_page or publisher or "").strip()


def asset_from_slot(slot_code: str, slot_name: str | None) -> str:
    return slot_name or slot_code.replace("_", " ").title()


def marketplace_from_slot(slot_code: str, slot_name: str | None) -> str:
    return "supermall" if is_supermall(slot_code, slot_name) else "core"


GENERIC_SLOT_TOKENS = {
    "noon", "ae", "sa", "eg", "uae", "ksa", "egypt",
    "clp", "plp", "pdp", "page", "mid", "upper", "lower", "top", "fold",
    "unit", "slot", "hero", "hp", "sfu", "atf", "btf",
}

GENERIC_PAGE_KEYS = {
    "home_page", "homepage", "home page",
    "presearch", "search", "search_page", "search page",
}

# CPD inventory is deliberately reserved for campaigns large enough to absorb a
# meaningful daily placement without distorting the requested allocation splits.
MIN_CPD_BUDGET_USD = 15_000.0


def _slot_tokens(slot_code: str, slot_name: str | None) -> list[str]:
    text = f"{slot_name or ''} {slot_code or ''}".lower()
    tokens = [
        token
        for token in re.split(r"[^a-z0-9]+", text)
        if token and len(token) > 2 and not token.isdigit() and token not in GENERIC_SLOT_TOKENS
    ]
    return tokens


def _slot_signature(slot_code: str, slot_name: str | None) -> str:
    return " ".join(_slot_tokens(slot_code, slot_name))


def _slot_similarity(left_tokens: list[str], right_tokens: list[str]) -> float:
    left_set = set(left_tokens)
    right_set = set(right_tokens)
    if not left_set or not right_set:
        return 0.0
    overlap = len(left_set & right_set)
    return overlap / max(min(len(left_set), len(right_set)), 1)


def _page_key(*values: str | None) -> str:
    for value in values:
        cleaned = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
        if cleaned:
            return cleaned
    return ""


def _is_generic_page(*values: str | None) -> bool:
    keys = {
        re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
        for value in values
        if str(value or "").strip()
    }
    return bool(keys & {re.sub(r"[^a-z0-9]+", "_", value).strip("_") for value in GENERIC_PAGE_KEYS})


def _requested_comcat_tokens(req: MediaPlanRequest) -> tuple[set[str], set[str]]:
    values = {value.strip().lower() for value in req.comcats if value and value.strip()}
    tokens = {
        token
        for value in values
        for token in re.split(r"[^a-z0-9]+", value)
        if token and len(token) > 2
    }
    return values, tokens


def _slot_comcat_relevance(category: str | None, slot_code: str, slot_name: str | None, req: MediaPlanRequest) -> float:
    comcat_values, comcat_tokens = _requested_comcat_tokens(req)
    haystacks = [
        (category or "").strip().lower(),
        (slot_name or "").strip().lower(),
        (slot_code or "").strip().lower(),
    ]
    if not comcat_values:
        return 1.0
    haystack_tokens = set().union(*(_slot_tokens(text, None) for text in haystacks if text))
    if any(_has_comcat_conflict(comcat, haystack_tokens) for comcat in comcat_values):
        return 0.0
    best = 0.0
    for value in haystacks:
        if not value:
            continue
        if value in comcat_values or any(comcat in value or value in comcat for comcat in comcat_values):
            best = max(best, 1.0)
    expanded_tokens = set(comcat_tokens)
    for comcat in comcat_values:
        expanded_tokens |= _expanded_comcat_tokens(comcat)
    if expanded_tokens and haystack_tokens:
        best = max(best, len(expanded_tokens & haystack_tokens) / len(expanded_tokens))
    if _is_category_specific_slot(category, None, slot_code, slot_name) and best <= 0:
        return 0.0
    # Homepage is category-neutral, not category-irrelevant.  Keeping a small
    # positive relevance lets a mobile campaign, for example, retain reach
    # inventory while category-specific slots still rank much higher.
    if _is_generic_page(category, slot_name, slot_code):
        return max(best, 0.2)
    return best


def _comcat_tokens(value: str) -> set[str]:
    return {
        token
        for token in re.split(r"[^a-z0-9]+", str(value or "").strip().lower())
        if token and len(token) > 2
    }


COMCAT_RELATED_TOKENS: dict[str, set[str]] = {
    "apparel": {"apparel", "fashion", "mens", "womens", "clothing"},
    "fashion": {"apparel", "fashion", "mens", "womens", "clothing"},
    "footwear": {"footwear", "shoe", "shoes", "sneaker", "sneakers"},
    "mobiles": {"mobile", "mobiles", "phone", "phones", "smartphone", "smartphones"},
    "mobile_accessories": {"mobile", "mobiles", "accessory", "accessories", "phone", "phones"},
    "camera": {"camera", "cameras"},
    "cameras": {"camera", "cameras"},
    "fragrance": {"fragrance", "perfume", "perfumes"},
    "kitchen_dining": {"kitchen", "dining"},
    "home_kitchen": {"home", "kitchen"},
    "large_appliances": {"appliance", "appliances"},
    "small_appliances": {"appliance", "appliances"},
}


COMCAT_CONFLICT_TOKENS: dict[str, set[str]] = {
    "mobiles": {"camera", "cameras"},
    "mobile_accessories": {"camera", "cameras"},
    "camera": {"mobile", "mobiles", "phone", "phones", "smartphone", "smartphones"},
    "cameras": {"mobile", "mobiles", "phone", "phones", "smartphone", "smartphones"},
}


def _expanded_comcat_tokens(comcat: str) -> set[str]:
    base = _comcat_tokens(comcat)
    expanded = set(base)
    key = str(comcat or "").strip().lower()
    if key in COMCAT_RELATED_TOKENS:
        expanded |= COMCAT_RELATED_TOKENS[key]
    for token in base:
        expanded |= COMCAT_RELATED_TOKENS.get(token, set())
    return expanded


def _has_comcat_conflict(comcat: str, haystack_tokens: set[str]) -> bool:
    comcat_tokens = _comcat_tokens(comcat)
    requested = set()
    for token in comcat_tokens | {str(comcat or "").strip().lower()}:
        if token in COMCAT_CONFLICT_TOKENS:
            requested.add(token)
    return any(haystack_tokens & COMCAT_CONFLICT_TOKENS[token] for token in requested)


def _is_category_specific_slot(category: str | None, page: str | None, slot_code: str | None, slot_name: str | None) -> bool:
    if _is_generic_page(category, page):
        return False
    text = f"{category or ''} {page or ''} {slot_code or ''} {slot_name or ''}".lower()
    if any(marker in text for marker in ("clp", "plp", "salepage")):
        return True
    tokens = set(_slot_tokens(text, None))
    return bool(tokens and not _is_generic_page(category, page))


def _slot_values_relevance_for_comcat(category: str | None, page: str | None, slot_code: str | None, slot_name: str | None, comcat: str) -> float:
    comcat_value = str(comcat or "").strip().lower()
    if not comcat_value:
        return 1.0
    haystacks = [
        (category or "").strip().lower(),
        (page or "").strip().lower(),
        (slot_name or "").strip().lower(),
        (slot_code or "").strip().lower(),
    ]
    haystack_tokens = set().union(*(_slot_tokens(text, None) for text in haystacks if text))
    if _has_comcat_conflict(comcat_value, haystack_tokens):
        return 0.0
    best = 0.0
    for value in haystacks:
        if not value:
            continue
        if value == comcat_value or comcat_value in value or value in comcat_value:
            best = max(best, 1.0)
    comcat_tokens = _expanded_comcat_tokens(comcat_value)
    if comcat_tokens and haystack_tokens:
        best = max(best, len(comcat_tokens & haystack_tokens) / len(comcat_tokens))
    if _is_category_specific_slot(category, page, slot_code, slot_name) and best <= 0:
        return 0.0
    if _is_generic_page(category, page, slot_name, slot_code):
        return max(best, 0.2)
    return best


def _slot_relevance_for_comcat(candidate: Candidate, comcat: str) -> float:
    return _slot_values_relevance_for_comcat(candidate.category, candidate.page, candidate.slot_code, candidate.slot_name, comcat)


def _row_relevance_for_comcat(row: EditablePlanLine, comcat: str) -> float:
    return _slot_values_relevance_for_comcat(row.category, row.page, row.slot_code, row.slot_name, comcat)


def discounted_rate(rate: float, discount_pct: float) -> float:
    return round(rate * max(0.0, 1 - (discount_pct / 100.0)), 4)


def normalize_objective(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"reach", "visibility", "viewability"}:
        return "visibility"
    if raw in {"ctr", "engagement"}:
        return "ctr"
    if raw in {"roas", "conversion", "conversions"}:
        return "roas"
    return "both"


def clamp01(value: float) -> float:
    return max(0.0, min(value, 1.0))


def _safe_ratio(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def _confidence_score(views: int, clicks: int, active_days: int, settings) -> float:
    view_signal = clamp01(_safe_ratio(views, max(settings.min_slot_views * 20, 1)))
    click_signal = clamp01(_safe_ratio(clicks, 1000))
    day_signal = clamp01(_safe_ratio(active_days, 30))
    return round((view_signal * 0.45) + (click_signal * 0.35) + (day_signal * 0.20), 4)


def _visibility_metric(views: int, spends: float, default_cpm: float) -> float:
    cpm = spends * 1000 / views if views > 0 and spends > 0 else default_cpm
    return views / max(cpm, 0.01)


def _objective_metric(objective: str, views: int, clicks: int, revenue: float, spends: float, default_cpm: float) -> float:
    objective_key = normalize_objective(objective)
    if objective_key == "ctr":
        return _safe_ratio(clicks, views)
    if objective_key == "roas":
        return _safe_ratio(revenue, spends)
    return _visibility_metric(views, spends, default_cpm)


def gross_from_net(net_amount: float, discount_pct: float) -> float:
    factor = max(0.0, 1 - (discount_pct / 100.0))
    if factor <= 0:
        return round(net_amount, 2)
    return round(net_amount / factor, 2)


def net_and_gross_amount(gross_amount: float, discount_pct: float) -> tuple[float, float]:
    net_amount = round(gross_amount * max(0.0, 1 - (discount_pct / 100.0)), 2)
    return net_amount, round(gross_amount, 2)


def round_views_to_block(views: int, block_size: int = 100) -> int:
    if views <= 0:
        return 0
    return int(round(views / block_size) * block_size)


def floor_views_to_block(views: int, block_size: int = 100) -> int:
    if views <= 0:
        return 0
    return int(views // block_size) * block_size


def iter_dates(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _distributed_daily_views(total_views: int, day_count: int) -> list[int]:
    if day_count <= 0:
        return []
    base = total_views // day_count
    remainder = total_views % day_count
    return [base + (1 if idx < remainder else 0) for idx in range(day_count)]


def _cpm_row_pricing(
    meta: dict,
    candidate: Candidate,
    start: date,
    end: date,
    planned_views: int,
    discount_pct: float,
    default_cpm: float,
) -> tuple[float, float, float, float]:
    gross_fallback = float(meta.get("cpm_rate") or candidate.slot_rate or 0)
    if gross_fallback <= 0:
        return 0.0, 0.0, 0.0, 0.0
    rate_schedule = meta.get("cpm_rate_schedule") or meta.get("rate_schedule") or {}
    days = list(iter_dates(start, end))
    daily_views = _distributed_daily_views(planned_views, len(days))
    gross_total = 0.0
    net_total = 0.0
    for idx, dt in enumerate(days):
        daily_rate = float(rate_schedule.get(dt.isoformat()) or gross_fallback)
        daily_net_rate = discounted_rate(daily_rate, discount_pct)
        views = daily_views[idx]
        gross_total += views * daily_rate / 1000
        net_total += views * daily_net_rate / 1000
    if planned_views <= 0:
        gross_avg = gross_fallback
        net_avg = discounted_rate(gross_fallback, discount_pct)
    else:
        gross_avg = gross_total * 1000 / planned_views
        net_avg = net_total * 1000 / planned_views
    return round(gross_total, 2), round(net_total, 2), round(gross_avg, 4), round(net_avg, 4)


def _cpd_daily_rates(
    meta: dict,
    candidate: Candidate,
    start: date,
    end: date,
    default_cpd: float,
) -> list[tuple[date, float, float]]:
    schedule = meta.get("cpd_rate_schedule") or {}
    gross_fallback = float(meta.get("cpd_rate") or candidate.slot_rate or 0)
    if gross_fallback <= 0 and not schedule:
        return []
    rates = []
    for dt in iter_dates(start, end):
        gross_rate = float(schedule.get(dt.isoformat()) or gross_fallback)
        rates.append((dt, gross_rate, gross_rate))
    return rates


def default_phases(req: MediaPlanRequest) -> list[Phase]:
    if req.phases:
        return req.phases
    return [Phase.model_validate({"name": "Full flight", "from": req.start_date, "to": req.end_date})]


def objective_weights(req: MediaPlanRequest) -> tuple[float, float]:
    objective = normalize_objective(req.objective)
    if objective == "visibility":
        return 1.0, 0.0
    if objective in {"roas", "ctr"}:
        return 0.0, 1.0
    reach = max(req.reach_weight, 0)
    roas = max(req.roas_weight, 0)
    total = reach + roas
    if total <= 0:
        return 0.6, 0.4
    return reach / total, roas / total


def _allocation_tracks(req: MediaPlanRequest) -> list[tuple[str, float, str]]:
    normalized = normalize_objective(req.objective)
    if normalized == "visibility":
        return [("reach", 1.0, "visibility_score")]
    if normalized == "ctr":
        return [("conv", 1.0, "ctr_score")]
    if normalized == "roas":
        return [("conv", 1.0, "roas_score")]
    reach_weight, roas_weight = objective_weights(req)
    return [("reach", reach_weight, "visibility_score"), ("conv", roas_weight, "roas_score")]


def _allocation_type(objective: str) -> str:
    return "reach" if normalize_objective(objective) == "visibility" else "conv"


def _portfolio_score(candidate: Candidate) -> float:
    return candidate.final_score or max(candidate.visibility_score, candidate.ctr_score, candidate.roas_score, candidate.reach_score, candidate.conv_score)


def _marketplace_splits(req: MediaPlanRequest) -> list[tuple[str, float]]:
    if req.marketplace == "core":
        return [("core", 1.0)]
    if req.marketplace == "supermall":
        return [("supermall", 1.0)]
    core_pct = max(float(getattr(req, "marketplace_core_pct", 70) or 0), 0.0)
    supermall_pct = max(float(getattr(req, "marketplace_supermall_pct", 30) or 0), 0.0)
    total = core_pct + supermall_pct
    if total <= 0:
        return [("core", 0.7), ("supermall", 0.3)]
    return [("core", core_pct / total), ("supermall", supermall_pct / total)]


def _comcat_splits(req: MediaPlanRequest) -> list[tuple[str, float]]:
    selected = [str(value or "").strip() for value in req.comcats if str(value or "").strip()]
    if not selected:
        return [("", 1.0)]
    raw_splits = getattr(req, "comcat_budget_splits", {}) or {}
    weights = []
    for comcat in selected:
        raw_value = raw_splits.get(comcat)
        if raw_value is None:
            raw_value = raw_splits.get(comcat.lower())
        try:
            weight = max(float(raw_value), 0.0)
        except (TypeError, ValueError):
            weight = 0.0
        weights.append((comcat, weight))
    total = sum(weight for _, weight in weights)
    if total <= 0:
        equal_share = 1.0 / len(selected)
        return [(comcat, equal_share) for comcat in selected]
    return [(comcat, weight / total) for comcat, weight in weights]


def _phase_splits(req: MediaPlanRequest, phases: list[Phase]) -> dict[str, float]:
    if not phases:
        return {}
    raw_splits = getattr(req, "phase_budget_splits", {}) or {}
    weights: dict[str, float] = {}
    for phase in phases:
        raw_value = raw_splits.get(phase.name)
        if raw_value is None:
            raw_value = raw_splits.get(str(phase.name or "").lower())
        try:
            weights[phase.name] = max(float(raw_value), 0.0)
        except (TypeError, ValueError):
            weights[phase.name] = 0.0
    total = sum(weights.values())
    if total > 0:
        return {name: weight / total for name, weight in weights.items()}
    total_days = sum(inclusive_days(phase.from_date, phase.to_date) for phase in phases)
    if total_days <= 0:
        equal_share = 1.0 / len(phases)
        return {phase.name: equal_share for phase in phases}
    return {phase.name: inclusive_days(phase.from_date, phase.to_date) / total_days for phase in phases}


def _weighted_phase_sequence(phases: list[Phase], phase_splits: dict[str, float], count: int) -> list[Phase]:
    if not phases or count <= 0:
        return []
    quotas = []
    assigned = 0
    for index, phase in enumerate(phases):
        raw_quota = max(float(phase_splits.get(phase.name, 0) or 0), 0.0) * count
        floor_quota = int(raw_quota)
        assigned += floor_quota
        quotas.append([phase, floor_quota, raw_quota - floor_quota, index])
    for phase_quota in sorted(quotas, key=lambda item: (item[2], -item[3]), reverse=True):
        if assigned >= count:
            break
        phase_quota[1] += 1
        assigned += 1
    sequence: list[Phase] = []
    for phase, quota, _fraction, _index in quotas:
        sequence.extend([phase] * quota)
    while len(sequence) < count:
        sequence.append(phases[len(sequence) % len(phases)])
    return sequence[:count]


def _brand_splits(req: MediaPlanRequest) -> list[tuple[str, float]]:
    selected = [str(value or "").strip() for value in (getattr(req, "brands", []) or []) if str(value or "").strip()]
    if not selected and str(req.brand or "").strip():
        selected = [str(req.brand).strip()]
    if not selected:
        return [("", 1.0)]
    raw_splits = getattr(req, "brand_budget_splits", {}) or {}
    weights = []
    for brand in selected:
        raw_value = raw_splits.get(brand)
        if raw_value is None:
            raw_value = raw_splits.get(brand.lower())
        try:
            weight = max(float(raw_value), 0.0)
        except (TypeError, ValueError):
            weight = 0.0
        weights.append((brand, weight))
    total = sum(weight for _, weight in weights)
    if total <= 0:
        equal_share = 1.0 / len(selected)
        return [(brand, equal_share) for brand in selected]
    return [(brand, weight / total) for brand, weight in weights]


def build_candidates(historical_rows: list[dict], slot_meta: dict[tuple[str, str], dict], settings, marketplace: str | None) -> list[Candidate]:
    prepared_rows: list[dict] = []
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

        meta = get_slot_meta(slot_meta, country, slot_code)
        if not meta:
            continue
        slot_code = meta.get("slot_code") or slot_code
        slot_name = meta.get("slot_name")
        if marketplace and marketplace != "both":
            sm = is_supermall(slot_code, slot_name)
            if marketplace == "supermall" and not sm:
                continue
            if marketplace == "core" and sm:
                continue

        cpm = spends * 1000 / views if views > 0 and spends > 0 else None
        ctr = clicks / views if views > 0 else None
        # RoAS uses the pooled (page × comcat) weighted figure, not this slot's own thin
        # ratio; falls back to slot-level only if the pooled value is unavailable.
        roas_pagecomcat = row.get("roas_pagecomcat")
        roas = roas_pagecomcat if roas_pagecomcat is not None else (revenue / spends if spends > 0 else None)
        cpd = spends / active_days if active_days > 0 and spends > 0 else None
        confidence_score = _confidence_score(views, clicks, active_days, settings)
        visibility_metric = _objective_metric("visibility", views, clicks, revenue, spends, settings.default_cpm)
        ctr_metric = _objective_metric("ctr", views, clicks, revenue, spends, settings.default_cpm)
        roas_metric = roas_pagecomcat if roas_pagecomcat is not None else _objective_metric("roas", views, clicks, revenue, spends, settings.default_cpm)
        trend_visibility = (
            _objective_metric("visibility", int(row.get("views_30d") or 0), int(row.get("clicks_30d") or 0), float(row.get("revenue_30d") or 0.0), float(row.get("spends_30d") or 0.0), settings.default_cpm) * 0.5
            + _objective_metric("visibility", int(row.get("views_90d") or 0), int(row.get("clicks_90d") or 0), float(row.get("revenue_90d") or 0.0), float(row.get("spends_90d") or 0.0), settings.default_cpm) * 0.3
            + _objective_metric("visibility", int(row.get("views_180d") or 0), int(row.get("clicks_180d") or 0), float(row.get("revenue_180d") or 0.0), float(row.get("spends_180d") or 0.0), settings.default_cpm) * 0.2
        )
        trend_ctr = (
            _objective_metric("ctr", int(row.get("views_30d") or 0), int(row.get("clicks_30d") or 0), float(row.get("revenue_30d") or 0.0), float(row.get("spends_30d") or 0.0), settings.default_cpm) * 0.5
            + _objective_metric("ctr", int(row.get("views_90d") or 0), int(row.get("clicks_90d") or 0), float(row.get("revenue_90d") or 0.0), float(row.get("spends_90d") or 0.0), settings.default_cpm) * 0.3
            + _objective_metric("ctr", int(row.get("views_180d") or 0), int(row.get("clicks_180d") or 0), float(row.get("revenue_180d") or 0.0), float(row.get("spends_180d") or 0.0), settings.default_cpm) * 0.2
        )
        trend_roas = (
            _objective_metric("roas", int(row.get("views_30d") or 0), int(row.get("clicks_30d") or 0), float(row.get("revenue_30d") or 0.0), float(row.get("spends_30d") or 0.0), settings.default_cpm) * 0.5
            + _objective_metric("roas", int(row.get("views_90d") or 0), int(row.get("clicks_90d") or 0), float(row.get("revenue_90d") or 0.0), float(row.get("spends_90d") or 0.0), settings.default_cpm) * 0.3
            + _objective_metric("roas", int(row.get("views_180d") or 0), int(row.get("clicks_180d") or 0), float(row.get("revenue_180d") or 0.0), float(row.get("spends_180d") or 0.0), settings.default_cpm) * 0.2
        )
        prepared_rows.append(
            {
                "row": row,
                "country": country,
                "slot_code": slot_code,
                "slot_name": slot_name,
                "meta": meta,
                "views": views,
                "clicks": clicks,
                "revenue": revenue,
                "spends": spends,
                "active_days": active_days,
                "cpm": cpm,
                "ctr": ctr,
                "roas": roas,
                "cpd": cpd,
                "brand_specific": bool(row.get("brand_specific")),
                "confidence_score": confidence_score,
                "visibility_metric": visibility_metric,
                "ctr_metric": ctr_metric,
                "roas_metric": roas_metric,
                "trend_visibility": trend_visibility,
                "trend_ctr": trend_ctr,
                "trend_roas": trend_roas,
            }
        )

    brand_visibility_best = max([item["visibility_metric"] for item in prepared_rows if item["brand_specific"]], default=0.0)
    brand_ctr_best = max([item["ctr_metric"] for item in prepared_rows if item["brand_specific"]], default=0.0)
    brand_roas_best = max([item["roas_metric"] for item in prepared_rows if item["brand_specific"]], default=0.0)
    comcat_visibility_best = max([item["visibility_metric"] for item in prepared_rows], default=0.0)
    comcat_ctr_best = max([item["ctr_metric"] for item in prepared_rows], default=0.0)
    comcat_roas_best = max([item["roas_metric"] for item in prepared_rows], default=0.0)
    trend_visibility_best = max([item["trend_visibility"] for item in prepared_rows], default=0.0)
    trend_ctr_best = max([item["trend_ctr"] for item in prepared_rows], default=0.0)
    trend_roas_best = max([item["trend_roas"] for item in prepared_rows], default=0.0)
    has_brand_history = any(item["brand_specific"] for item in prepared_rows)

    candidates: list[Candidate] = []
    for item in prepared_rows:
        row = item["row"]
        country = item["country"]
        slot_code = item["slot_code"]
        slot_name = item["slot_name"]
        meta = item["meta"]
        views = item["views"]
        clicks = item["clicks"]
        revenue = item["revenue"]
        spends = item["spends"]
        active_days = item["active_days"]
        cpm = item["cpm"]
        ctr = item["ctr"]
        roas = item["roas"]
        cpd = item["cpd"]
        confidence_score = item["confidence_score"]

        brand_visibility_score = clamp01(_safe_ratio(item["visibility_metric"], brand_visibility_best)) * confidence_score if item["brand_specific"] and brand_visibility_best > 0 else 0.0
        brand_ctr_score = clamp01(_safe_ratio(item["ctr_metric"], brand_ctr_best)) * confidence_score if item["brand_specific"] and brand_ctr_best > 0 else 0.0
        brand_roas_score = clamp01(_safe_ratio(item["roas_metric"], brand_roas_best)) * confidence_score if item["brand_specific"] and brand_roas_best > 0 else 0.0
        comcat_visibility_score = clamp01(_safe_ratio(item["visibility_metric"], comcat_visibility_best)) * confidence_score if comcat_visibility_best > 0 else 0.0
        comcat_ctr_score = clamp01(_safe_ratio(item["ctr_metric"], comcat_ctr_best)) * confidence_score if comcat_ctr_best > 0 else 0.0
        comcat_roas_score = clamp01(_safe_ratio(item["roas_metric"], comcat_roas_best)) * confidence_score if comcat_roas_best > 0 else 0.0
        trend_visibility_score = clamp01(_safe_ratio(item["trend_visibility"], trend_visibility_best))
        trend_ctr_score = clamp01(_safe_ratio(item["trend_ctr"], trend_ctr_best))
        trend_roas_score = clamp01(_safe_ratio(item["trend_roas"], trend_roas_best))

        brand_weight = 0.4 if has_brand_history else 0.0
        comcat_weight = 0.4 if has_brand_history else 0.8
        trend_weight = 0.2

        visibility_score = round((brand_visibility_score * brand_weight) + (comcat_visibility_score * comcat_weight) + (trend_visibility_score * trend_weight), 4)
        ctr_score = round((brand_ctr_score * brand_weight) + (comcat_ctr_score * comcat_weight) + (trend_ctr_score * trend_weight), 4)
        roas_score = round((brand_roas_score * brand_weight) + (comcat_roas_score * comcat_weight) + (trend_roas_score * trend_weight), 4)
        final_score = round(max(visibility_score, ctr_score, roas_score), 4)
        explainability = []
        if item["brand_specific"] and max(brand_visibility_score, brand_ctr_score, brand_roas_score) > 0:
            explainability.append("Historical advertiser performance is strong on this slot.")
        if max(comcat_visibility_score, comcat_ctr_score, comcat_roas_score) > 0:
            explainability.append("Comparable ComCat performance supports this placement.")
        if max(trend_visibility_score, trend_ctr_score, trend_roas_score) > 0:
            explainability.append("Recent 30/90/180 day trend signals remain supportive.")
        explainability.append(f"Confidence score {round(confidence_score * 100)}% based on delivery volume and campaign depth.")

        pricing_options = [
            option
            for option in pricing_options_for_meta(meta)
            if slot_has_rate_for_model(
                meta,
                option,
                default_rate=settings.default_cpd if option == "CPD" else settings.default_cpm,
            )
        ]
        if not pricing_options:
            fallback_model = normalize_pricing_model(meta.get("pricing_model") or row.get("pricing_model"))
            pricing_options = [
                fallback_model
            ] if slot_has_rate_for_model(
                meta,
                fallback_model,
                default_rate=settings.default_cpd if fallback_model == "CPD" else settings.default_cpm,
            ) else []

        for pricing_model in pricing_options:
            candidate_slot_rate = slot_rate_for_model(
                meta,
                pricing_model,
                settings.default_cpd if pricing_model == "CPD" else settings.default_cpm,
            )
            if not candidate_slot_rate:
                continue
            pricing_bias = 1.02 if pricing_model == "CPM" else 1.0
            candidates.append(
                Candidate(
                    country=country,
                    slot_code=slot_code,
                    slot_name=slot_name,
                    page=meta.get("page"),
                    category=meta.get("category") or meta.get("page"),
                    zone=meta.get("zone"),
                    dimension=meta.get("dimension"),
                    marketplace=marketplace_from_slot(slot_code, slot_name),
                    publisher=meta.get("publisher") or row.get("publisher"),
                    pricing_model=pricing_model,
                    slot_rate=candidate_slot_rate,
                    views=views,
                    clicks=clicks,
                    revenue=revenue,
                    spends=spends,
                    active_days=active_days,
                    brand_specific=bool(row.get("brand_specific")),
                    reach_score=visibility_score * pricing_bias,
                    conv_score=max(ctr_score, roas_score) * pricing_bias,
                    visibility_score=visibility_score * pricing_bias,
                    ctr_score=ctr_score * pricing_bias,
                    roas_score=roas_score * pricing_bias,
                    brand_score=max(brand_visibility_score, brand_ctr_score, brand_roas_score),
                    comcat_score=max(comcat_visibility_score, comcat_ctr_score, comcat_roas_score),
                    trend_score=max(trend_visibility_score, trend_ctr_score, trend_roas_score),
                    confidence_score=confidence_score,
                    final_score=final_score * pricing_bias,
                    cpm=cpm,
                    cpd=cpd,
                    ctr=ctr,
                    roas=roas,
                    source_country=country,
                    synthetic=False,
                    explainability=tuple(explainability),
                )
            )

    deduped: dict[tuple[str, str, str], Candidate] = {}
    for candidate in sorted(candidates, key=lambda c: (c.final_score, c.brand_specific, c.views), reverse=True):
        deduped.setdefault((candidate.country, candidate.slot_code, candidate.pricing_model), candidate)
    return list(deduped.values())


def expand_candidates_for_countries(
    req: MediaPlanRequest,
    candidates: list[Candidate],
    slot_meta: dict[tuple[str, str], dict],
    settings,
) -> list[Candidate]:
    selected_countries = {country for country in req.countries if country}
    if not selected_countries:
        return []

    direct_candidates = [candidate for candidate in candidates if candidate.country in selected_countries]
    direct_by_key = {(candidate.country, candidate.slot_code): candidate for candidate in direct_candidates}
    relevant_pages = {
        _page_key(candidate.category, candidate.page)
        for candidate in candidates
        if _slot_comcat_relevance(candidate.category, candidate.slot_code, candidate.slot_name, req) > 0
    }

    source_groups: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        signature = _slot_signature(candidate.slot_code, candidate.slot_name)
        if signature:
            source_groups[signature].append(candidate)

    slot_meta_by_country: dict[str, list[dict]] = defaultdict(list)
    for (country, _slot_code), meta in slot_meta.items():
        if country in selected_countries:
            slot_meta_by_country[country].append(meta)

    expanded = list(direct_candidates)
    for country in selected_countries:
        for meta in slot_meta_by_country.get(country, []):
            slot_code = meta.get("slot_code") or ""
            if not slot_code or (country, slot_code) in direct_by_key:
                continue
            slot_name = meta.get("slot_name")
            target_page = _page_key(meta.get("category"), meta.get("page"))
            target_is_generic = _is_generic_page(meta.get("category"), meta.get("page"))
            target_relevance = _slot_comcat_relevance(meta.get("category") or meta.get("page"), slot_code, slot_name, req)
            target_page_is_relevant = bool(target_page and target_page in relevant_pages)
            if target_relevance <= 0 and not target_is_generic and not target_page_is_relevant:
                continue
            signature = _slot_signature(slot_code, slot_name)
            source_candidates = [
                candidate
                for candidate in source_groups.get(signature, [])
                if candidate.country != country
                and candidate.marketplace == marketplace_from_slot(slot_code, slot_name)
                and (
                    _slot_comcat_relevance(candidate.category, candidate.slot_code, candidate.slot_name, req) > 0
                    or (target_page_is_relevant and _page_key(candidate.category, candidate.page) == target_page)
                    or (target_is_generic and _page_key(candidate.category, candidate.page) == target_page)
                )
            ]
            if not source_candidates:
                target_tokens = _slot_tokens(slot_code, slot_name)
                scored_sources: list[tuple[float, float, Candidate]] = []
                for candidate in candidates:
                    if candidate.country == country:
                        continue
                    if candidate.marketplace != marketplace_from_slot(slot_code, slot_name):
                        continue
                    candidate_page = _page_key(candidate.category, candidate.page)
                    candidate_relevance = _slot_comcat_relevance(candidate.category, candidate.slot_code, candidate.slot_name, req)
                    if candidate_relevance <= 0 and not ((target_is_generic or target_page_is_relevant) and candidate_page == target_page):
                        continue
                    similarity = _slot_similarity(target_tokens, _slot_tokens(candidate.slot_code, candidate.slot_name))
                    page_similarity = 1.0 if target_page and candidate_page == target_page else 0.0
                    if page_similarity <= 0 and similarity < 0.55:
                        continue
                    score = _portfolio_score(candidate)
                    if meta.get("category") and candidate.category and str(meta.get("category")).strip().lower() == str(candidate.category).strip().lower():
                        similarity += 0.1
                    similarity += page_similarity * 0.8
                    similarity += min(target_relevance, 1.0) * 0.15
                    scored_sources.append((page_similarity, similarity, score, candidate))
                scored_sources.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
                source_candidates = [item[3] for item in scored_sources[:3]]
            if not source_candidates:
                continue
            source = max(source_candidates, key=lambda candidate: (candidate.brand_specific, _portfolio_score(candidate), candidate.views))
            for pricing_model in [
                option
                for option in pricing_options_for_meta(meta)
                if slot_has_rate_for_model(
                    meta,
                    option,
                    default_rate=settings.default_cpd if option == "CPD" else settings.default_cpm,
                )
            ]:
                expanded.append(
                    Candidate(
                        country=country,
                        slot_code=slot_code,
                        slot_name=slot_name,
                        page=meta.get("page"),
                        category=meta.get("category") or meta.get("page"),
                        zone=meta.get("zone"),
                        dimension=meta.get("dimension"),
                        marketplace=marketplace_from_slot(slot_code, slot_name),
                        publisher=meta.get("publisher"),
                        pricing_model=pricing_model,
                        slot_rate=slot_rate_for_model(
                            meta,
                            pricing_model,
                            settings.default_cpd if pricing_model == "CPD" else settings.default_cpm,
                        ),
                        views=source.views,
                        clicks=source.clicks,
                        revenue=source.revenue,
                        spends=source.spends,
                    active_days=source.active_days,
                    brand_specific=source.brand_specific,
                    reach_score=source.reach_score * (1.02 if pricing_model == "CPM" else 1.0),
                    conv_score=source.conv_score * (1.02 if pricing_model == "CPM" else 1.0),
                    visibility_score=source.visibility_score * (1.02 if pricing_model == "CPM" else 1.0),
                    ctr_score=source.ctr_score * (1.02 if pricing_model == "CPM" else 1.0),
                    roas_score=source.roas_score * (1.02 if pricing_model == "CPM" else 1.0),
                    brand_score=source.brand_score,
                    comcat_score=source.comcat_score,
                    trend_score=source.trend_score,
                    confidence_score=source.confidence_score,
                    final_score=source.final_score * (1.02 if pricing_model == "CPM" else 1.0),
                    cpm=source.cpm,
                    cpd=source.cpd,
                    ctr=source.ctr,
                    roas=source.roas,
                    source_country=source.country,
                    synthetic=True,
                    explainability=source.explainability,
                )
            )

    deduped: dict[tuple[str, str, str], Candidate] = {}
    for candidate in sorted(expanded, key=lambda item: (not item.synthetic, item.brand_specific, _portfolio_score(item), item.views), reverse=True):
        deduped.setdefault((candidate.country, candidate.slot_code, candidate.pricing_model), candidate)
    return list(deduped.values())


def ensure_selected_slot_candidates(
    req: MediaPlanRequest,
    selected_slot_keys: set[str],
    selected_slot_pricing_map: dict[str, str],
    candidates: list[Candidate],
    slot_meta: dict[tuple[str, str], dict],
    settings,
) -> list[Candidate]:
    if not selected_slot_keys:
        return candidates

    candidate_by_slot_model = {
        (slot_key(candidate.country, candidate.slot_code), candidate.pricing_model): candidate
        for candidate in candidates
    }
    ensured = list(candidates)

    for selected_key in selected_slot_keys:
        country, _, slot_code = selected_key.partition("|")
        if not country or not slot_code:
            continue
        meta = get_slot_meta(slot_meta, country, slot_code)
        if not meta:
            continue
        slot_code = meta.get("slot_code") or slot_code

        page = meta.get("page")
        category = meta.get("category") or page
        slot_name = meta.get("slot_name")
        marketplace = marketplace_from_slot(slot_code, slot_name)

        source_candidates = [
            candidate
            for candidate in candidates
            if candidate.marketplace == marketplace
            and (
                candidate.slot_code == slot_code
                or _page_key(candidate.category, candidate.page) == _page_key(category, page)
                or _slot_signature(candidate.slot_code, candidate.slot_name) == _slot_signature(slot_code, slot_name)
            )
        ]
        source = max(
            source_candidates,
            key=lambda candidate: (candidate.brand_specific, _portfolio_score(candidate), candidate.views),
            default=None,
        )
        allowed_models = pricing_options_for_meta(meta)
        requested_model = selected_slot_pricing_map.get(selected_key)
        pricing_models = [requested_model] if requested_model in allowed_models else allowed_models
        for pricing_model in pricing_models:
            normalized_model = normalize_pricing_model(pricing_model)
            if (selected_key, normalized_model) in candidate_by_slot_model:
                continue
            if not slot_has_rate_for_model(
                meta,
                normalized_model,
                req.start_date,
                req.end_date,
                settings.default_cpd if normalized_model == "CPD" else settings.default_cpm,
            ):
                continue
            rate = slot_rate_for_model(
                meta,
                normalized_model,
                settings.default_cpd if normalized_model == "CPD" else settings.default_cpm,
            )
            if not rate:
                continue
            reach_score = source.reach_score if source else max(settings.min_slot_views / max(rate, 0.01), 1.0)
            conv_score = source.conv_score if source else reach_score
            visibility_score = source.visibility_score if source else reach_score
            ctr_score = source.ctr_score if source else 0.0
            roas_score = source.roas_score if source else reach_score
            brand_score = source.brand_score if source else 0.0
            comcat_score = source.comcat_score if source else reach_score
            trend_score = source.trend_score if source else 0.0
            confidence_score = source.confidence_score if source else 0.35
            final_score = source.final_score if source else max(visibility_score, ctr_score, roas_score)
            candidate = Candidate(
                country=country,
                slot_code=slot_code,
                slot_name=slot_name,
                page=page,
                category=category,
                zone=meta.get("zone"),
                dimension=meta.get("dimension"),
                marketplace=marketplace,
                publisher=meta.get("publisher"),
                pricing_model=normalized_model,
                slot_rate=rate,
                views=source.views if source else settings.min_slot_views,
                clicks=source.clicks if source else 0,
                revenue=source.revenue if source else 0.0,
                spends=source.spends if source else 0.0,
                active_days=source.active_days if source else 1,
                brand_specific=source.brand_specific if source else False,
                reach_score=reach_score * (1.02 if normalized_model == "CPM" else 1.0),
                conv_score=conv_score * (1.02 if normalized_model == "CPM" else 1.0),
                visibility_score=visibility_score * (1.02 if normalized_model == "CPM" else 1.0),
                ctr_score=ctr_score * (1.02 if normalized_model == "CPM" else 1.0),
                roas_score=roas_score * (1.02 if normalized_model == "CPM" else 1.0),
                brand_score=brand_score,
                comcat_score=comcat_score,
                trend_score=trend_score,
                confidence_score=confidence_score,
                final_score=final_score * (1.02 if normalized_model == "CPM" else 1.0),
                cpm=source.cpm if source else None,
                cpd=source.cpd if source else None,
                ctr=source.ctr if source else None,
                roas=source.roas if source else None,
                source_country=source.country if source else country,
                synthetic=True,
                explainability=source.explainability if source else ("Synthesized from similar slot and page performance.",),
            )
            ensured.append(candidate)
            candidate_by_slot_model[(selected_key, normalized_model)] = candidate

    deduped: dict[tuple[str, str, str], Candidate] = {}
    for candidate in sorted(ensured, key=lambda item: (not item.synthetic, item.brand_specific, _portfolio_score(item), item.views), reverse=True):
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
    return 6 if req.budget <= 10000 else 10


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


def _phase_existing_dates(rows: list[EditablePlanLine]) -> list[tuple[date, date]]:
    return [(row.from_date, row.to_date) for row in rows]


def _slot_existing_dates(rows: list[EditablePlanLine], country: str, slot_code: str) -> list[tuple[date, date]]:
    return [
        (row.from_date, row.to_date)
        for row in rows
        if row.country == country and row.slot_code == slot_code
    ]


def _category_zone_key(country: str, marketplace: str, category: str | None, zone: str | None) -> tuple[str, str, str, str]:
    return (
        country,
        marketplace or "",
        (category or "").strip().lower(),
        (zone or "").strip().lower(),
    )


def _phase_category_zone_used(rows: list[EditablePlanLine], country: str, phase_name: str) -> set[tuple[str, str, str, str]]:
    return {
        _category_zone_key(row.country, row.marketplace, row.category, row.zone)
        for row in rows
        if row.country == country and row.phase == phase_name and (row.category or row.zone)
    }


def _slot_window(phase: Phase, phase_rows: list[EditablePlanLine], pricing_model: str, sequence: int) -> tuple[date, date, int]:
    total_days = inclusive_days(phase.from_date, phase.to_date)
    if total_days <= 2:
        return phase.from_date, phase.to_date, total_days

    if pricing_model == "CPD":
        window_days = max(1, min(total_days, round(total_days * (0.45 + ((sequence % 3) * 0.15)))))
    else:
        window_days = max(1, min(total_days, round(total_days * (0.3 + ((sequence % 4) * 0.12)))))

    occupied = _phase_existing_dates(phase_rows)
    step = max(1, total_days // max(len(phase_rows) + 2, 2))
    start_offset = min(sequence * step, max(total_days - window_days, 0))
    start = phase.from_date.fromordinal(phase.from_date.toordinal() + start_offset)
    end = start.fromordinal(start.toordinal() + window_days - 1)

    if end > phase.to_date:
        end = phase.to_date
        start = end.fromordinal(end.toordinal() - window_days + 1)

    if occupied:
        candidate_offsets = list(range(0, max(total_days - window_days, 0) + 1))
        best_pair = (start, end)
        best_overlap = None
        for offset in candidate_offsets:
            cand_start = phase.from_date.fromordinal(phase.from_date.toordinal() + offset)
            cand_end = cand_start.fromordinal(cand_start.toordinal() + window_days - 1)
            overlap = 0
            for occ_start, occ_end in occupied:
                overlap += max(0, min(cand_end, occ_end).toordinal() - max(cand_start, occ_start).toordinal() + 1)
            if best_overlap is None or overlap < best_overlap:
                best_overlap = overlap
                best_pair = (cand_start, cand_end)
                if overlap == 0:
                    break
        start, end = best_pair

    return start, end, inclusive_days(start, end)


def _windows_overlap(start_a: date, end_a: date, start_b: date, end_b: date) -> bool:
    return max(start_a, start_b) <= min(end_a, end_b)


def _find_non_overlapping_slot_window(
    phase: Phase,
    desired_days: int,
    blocked_ranges: list[tuple[date, date]],
    preferred_start: date,
) -> tuple[date, date, int] | None:
    total_days = inclusive_days(phase.from_date, phase.to_date)
    if total_days <= 0:
        return None

    preferred_offset = max(0, min((preferred_start - phase.from_date).days, total_days - 1))
    offsets = sorted(range(total_days), key=lambda offset: abs(offset - preferred_offset))

    for window_days in range(min(desired_days, total_days), 0, -1):
        latest_start_offset = total_days - window_days
        for offset in offsets:
            if offset > latest_start_offset:
                continue
            cand_start = phase.from_date.fromordinal(phase.from_date.toordinal() + offset)
            cand_end = cand_start.fromordinal(cand_start.toordinal() + window_days - 1)
            if any(_windows_overlap(cand_start, cand_end, blocked_start, blocked_end) for blocked_start, blocked_end in blocked_ranges):
                continue
            return cand_start, cand_end, window_days
    return None


def _candidate_score(candidate: Candidate, objective: str) -> float:
    normalized = normalize_objective(objective)
    if normalized == "visibility":
        base_score = candidate.visibility_score
    elif normalized == "ctr":
        base_score = candidate.ctr_score
    elif normalized == "roas":
        base_score = candidate.roas_score
    else:
        base_score = candidate.final_score

    placement = _placement_kind(candidate)
    if normalized == "visibility":
        placement_bonus = 0.30 if placement == "homepage" else 0.04 if placement == "clp" else 0.0
    elif normalized in {"roas", "ctr"}:
        placement_bonus = 0.30 if placement == "clp" else 0.04 if placement == "homepage" else 0.0
    else:
        placement_bonus = 0.14 if placement in {"homepage", "clp"} else 0.0
    return base_score + placement_bonus


def _placement_kind(candidate: Candidate) -> str:
    """Classify the two placement families that campaign objective controls."""
    values = [candidate.page, candidate.category, candidate.slot_name, candidate.slot_code]
    normalized_values = {
        re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
        for value in values
        if str(value or "").strip()
    }
    if any(value in GENERIC_PAGE_KEYS or value in {"hp", "home"} for value in normalized_values):
        return "homepage"
    if any(re.search(r"(^|_)(clp|plp|category|salepage)($|_)", value) for value in normalized_values):
        return "clp"
    return "other"


def _objective_diverse_order(candidates: list[Candidate], objective: str) -> list[Candidate]:
    """Rank by objective while retaining some inventory from the secondary family."""
    ranked = sorted(
        candidates,
        key=lambda candidate: (_candidate_score(candidate, objective), _placement_priority(candidate), candidate.views),
        reverse=True,
    )
    normalized = normalize_objective(objective)
    primary = "homepage" if normalized == "visibility" else "clp" if normalized in {"roas", "ctr"} else ""
    secondary = "clp" if primary == "homepage" else "homepage" if primary == "clp" else ""
    if not primary:
        return ranked

    primary_rows = [candidate for candidate in ranked if _placement_kind(candidate) == primary]
    secondary_rows = [candidate for candidate in ranked if _placement_kind(candidate) == secondary]
    other_rows = [candidate for candidate in ranked if _placement_kind(candidate) not in {primary, secondary}]
    ordered: list[Candidate] = []
    while primary_rows or secondary_rows:
        ordered.extend(primary_rows[:4])
        primary_rows = primary_rows[4:]
        if secondary_rows:
            ordered.append(secondary_rows.pop(0))
    ordered.extend(other_rows)
    return ordered


def _placement_priority(candidate: Candidate) -> float:
    text = f"{candidate.slot_code or ''} {candidate.slot_name or ''} {candidate.page or ''}".lower()
    priority = 0.0
    if any(marker in text for marker in ("top_fold", "top fold", "tfu", "hero", "sfu")):
        priority += 0.18
    if "presearch" in text:
        priority += 0.08
    if "mid_page" in text or "mid page" in text or "_mid_" in text:
        priority -= 0.14
    if "lower_mid" in text or "lower mid" in text:
        priority -= 0.18
    return priority


def suggest_slots(
    req: MediaPlanRequest,
    historical_rows: list[dict],
    inventory_rows: list[dict],
    slot_meta: dict[tuple[str, str], dict],
    settings,
    limit: int = 10,
) -> list[dict]:
    base_candidates = build_candidates(historical_rows, slot_meta, settings, req.marketplace)
    selected_slot_keys = {value for value in req.selected_slot_keys if value}
    selected_slot_pricing_map = selected_slot_pricing(req)
    candidates = expand_candidates_for_countries(req, base_candidates, slot_meta, settings)
    candidates = ensure_selected_slot_candidates(req, selected_slot_keys, selected_slot_pricing_map, candidates, slot_meta, settings)
    candidates = [
        candidate
        for candidate in candidates
        if (req.budget >= MIN_CPD_BUDGET_USD or candidate.pricing_model != "CPD")
        and (not req.comcats or any(_slot_relevance_for_comcat(candidate, comcat) > 0 for comcat in req.comcats))
    ]
    inventory = _inventory_by_slot_phase(req, inventory_rows)
    phases = default_phases(req)
    seen: set[str] = set()
    seen_zone_category: set[tuple[str, str, str, str]] = set()
    suggestions: list[dict] = []

    candidates_by_slot: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        candidates_by_slot[slot_key(candidate.country, candidate.slot_code)].append(candidate)

    representative_candidates = [
        candidate
        for key in candidates_by_slot
        for candidate in [preferred_candidate_for_slot(candidates_by_slot[key], selected_slot_pricing_map.get(key), req.objective)]
        if candidate
    ]
    ranked_slot_keys = [
        slot_key(candidate.country, candidate.slot_code)
        for candidate in _objective_diverse_order(representative_candidates, req.objective)
    ]
    ordered_slot_keys: list[str] = []
    for country in [country for country in req.countries if country]:
        country_slot_keys = [key for key in ranked_slot_keys if key.startswith(f"{country}|")]
        for key in country_slot_keys:
            candidate = preferred_candidate_for_slot(candidates_by_slot[key], selected_slot_pricing_map.get(key), req.objective)
            available = sum(inventory.get((candidate.country, candidate.slot_code, phase.name), 0) for phase in phases) if candidate else 0
            if available > 0:
                ordered_slot_keys.append(key)
                break
    ordered_slot_keys.extend(ranked_slot_keys)

    for key in ordered_slot_keys:
        candidate = preferred_candidate_for_slot(candidates_by_slot.get(key, []), selected_slot_pricing_map.get(key), req.objective)
        if not candidate:
            continue
        slot_key_value = slot_key(candidate.country, candidate.slot_code)
        slot_options = candidates_by_slot.get(slot_key_value, [])
        cpm_candidate = preferred_candidate_for_slot([option for option in slot_options if option.pricing_model == "CPM"], "CPM", req.objective)
        cpd_candidate = preferred_candidate_for_slot([option for option in slot_options if option.pricing_model == "CPD"], "CPD", req.objective)
        if slot_key_value in seen:
            continue
        available = sum(inventory.get((candidate.country, candidate.slot_code, phase.name), 0) for phase in phases)
        if available <= 0:
            continue
        zone_category_key = _category_zone_key(
            candidate.country,
            candidate.marketplace,
            candidate.category,
            candidate.zone,
        )
        if (candidate.category or candidate.zone) and zone_category_key in seen_zone_category:
            continue
        seen.add(slot_key_value)
        seen_zone_category.add(zone_category_key)
        suggestions.append(
            {
                "slot_key": slot_key_value,
                "country": candidate.country,
                "slot_code": candidate.slot_code,
                "slot_name": candidate.slot_name or asset_from_slot(candidate.slot_code, candidate.slot_name),
                "page": page_from_slot(candidate.slot_code, candidate.publisher, candidate.page),
                "marketplace": candidate.marketplace,
                "category": candidate.category or "",
                "zone": candidate.zone or "",
                "dimension": candidate.dimension or "",
                "pricing_options": list(dict.fromkeys([option.pricing_model for option in slot_options] or [candidate.pricing_model])),
                "pricing_model": candidate.pricing_model,
                "cpm_rate": round(cpm_candidate.slot_rate or 0.0, 4) if cpm_candidate else 0.0,
                "net_cpm": round(discounted_rate(cpm_candidate.slot_rate or 0.0, req.discount_pct), 4) if cpm_candidate else 0.0,
                "gross_cpm": round(cpm_candidate.slot_rate or 0.0, 4) if cpm_candidate else 0.0,
                "cpd_rate": round(cpd_candidate.slot_rate or 0.0, 4) if cpd_candidate else 0.0,
                "net_cpd": round(discounted_rate(cpd_candidate.slot_rate or 0.0, req.discount_pct), 4) if cpd_candidate else 0.0,
                "available_views": available,
                "historical_cpm": candidate.cpm,
                "historical_ctr": candidate.ctr,
                "historical_roas": candidate.roas,
                "score": round(_candidate_score(candidate, req.objective), 4),
                "brand_score": round(candidate.brand_score, 4),
                "comcat_score": round(candidate.comcat_score, 4),
                "trend_score": round(candidate.trend_score, 4),
                "confidence_score": round(candidate.confidence_score, 4),
                "final_score": round(candidate.final_score, 4),
                "explainability": list(candidate.explainability),
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
    base_candidates = build_candidates(historical_rows, slot_meta, settings, req.marketplace)
    selected_slot_pricing_map = selected_slot_pricing(req)
    selected_slot_key_list = [value for value in req.selected_slot_keys if value]
    selected_slot_key_list.sort(
        key=lambda key: (
            0 if selected_slot_pricing_map.get(key) == "CPD" else 1,
            req.selected_slot_keys.index(key) if key in req.selected_slot_keys else 0,
        )
    )
    selected_slot_keys = set(selected_slot_key_list)
    candidates = expand_candidates_for_countries(req, base_candidates, slot_meta, settings)
    candidates = ensure_selected_slot_candidates(req, selected_slot_keys, selected_slot_pricing_map, candidates, slot_meta, settings)
    candidates = [
        candidate
        for candidate in candidates
        if (req.budget >= MIN_CPD_BUDGET_USD or candidate.pricing_model != "CPD")
        and (not req.comcats or any(_slot_relevance_for_comcat(candidate, comcat) > 0 for comcat in req.comcats))
    ]
    if selected_slot_keys:
        candidates = [
            candidate
            for candidate in candidates
            if slot_key(candidate.country, candidate.slot_code) in selected_slot_keys
            and (
                slot_key(candidate.country, candidate.slot_code) not in selected_slot_pricing_map
                or candidate.pricing_model == selected_slot_pricing_map[slot_key(candidate.country, candidate.slot_code)]
            )
        ]
    if not candidates:
        return [], {"reason": "No historical delivery rows found for the selected brand/comcat/countries."}

    inventory_by_slot_phase = _inventory_by_slot_phase(req, inventory_rows)

    phases = default_phases(req)
    rows = _coerce_rows(req)
    spent_total = sum(row.cost for row in rows)
    line_id = max([row.id for row in rows], default=0) + 1

    # User exclusions are global; generated reuse is phase-scoped.  The same slot
    # may legitimately run in two non-overlapping phases, which is essential for
    # matching phase budgets without substituting a weaker placement.
    excluded_slot_keys = set(req.excluded_slot_keys)
    used_slot_phases = {
        (slot_key(row.country, row.slot_code), row.phase)
        for row in rows
        if row.slot_code
    }
    foc_slot_keys = {value for value in req.foc_slot_keys if value}

    countries = [c for c in req.countries if c]
    if not countries:
        return [], {"reason": "No countries selected."}

    country_budget = req.budget / len(countries)
    brand_splits = _brand_splits(req)
    marketplace_splits = _marketplace_splits(req)
    comcat_splits = _comcat_splits(req)
    phase_splits = _phase_splits(req, phases)
    spent_by_country: dict[str, float] = defaultdict(float)
    for row in rows:
        spent_by_country[row.country] += float(row.cost or row.net_amount or 0)

    def append_row(candidate: Candidate, phase: Phase, stype: str, score_value: float, target_budget: float, force: bool = False, brand_name: str | None = None) -> bool:
        nonlocal line_id, spent_total
        slot_key_value = slot_key(candidate.country, candidate.slot_code)
        requested_model = selected_slot_pricing_map.get(slot_key_value)
        if requested_model and candidate.pricing_model != requested_model:
            return False
        if not force and slot_key_value in excluded_slot_keys:
            return False
        if not force and (slot_key_value, phase.name) in used_slot_phases:
            return False

        exact_inventory_key = (candidate.country, candidate.slot_code, phase.name)
        available = inventory_by_slot_phase.get(exact_inventory_key, 0)
        if available <= 0:
            return False

        used_zone_category = _phase_category_zone_used(rows, candidate.country, phase.name)
        zone_category_key = _category_zone_key(candidate.country, candidate.marketplace, candidate.category, candidate.zone)
        if (candidate.category or candidate.zone) and zone_category_key in used_zone_category and not force:
            return False

        phase_rows = [row for row in rows if row.country == candidate.country and row.phase == phase.name]
        slot_rows = [row for row in rows if row.country == candidate.country and row.slot_code == candidate.slot_code]
        row_from, row_to, days = _slot_window(phase, phase_rows, candidate.pricing_model, len(phase_rows))
        slot_window = _find_non_overlapping_slot_window(
            phase,
            days,
            _slot_existing_dates(rows, candidate.country, candidate.slot_code),
            row_from,
        )
        if not slot_window:
            return False
        row_from, row_to, days = slot_window

        buy_type = candidate.pricing_model
        is_foc = slot_key_value in foc_slot_keys
        meta = get_slot_meta(slot_meta, candidate.country, candidate.slot_code)
        if buy_type == "CPD":
            if not slot_has_rate_for_model(meta, "CPD", row_from, row_to, settings.default_cpd):
                return False
            gross_rate = round(float(meta.get("cpd_rate") or candidate.slot_rate or settings.default_cpd), 4)
        else:
            if not slot_has_rate_for_model(meta, "CPM", row_from, row_to, settings.default_cpm):
                return False
            gross_rate = round(float(meta.get("cpm_rate") or candidate.slot_rate or settings.default_cpm), 4)
        if gross_rate <= 0:
            return False
        rate = discounted_rate(gross_rate, req.discount_pct)

        remaining_budget = max(req.budget - spent_total, 0)
        working_budget = max(min(target_budget, remaining_budget), 0)
        if force and buy_type == "CPD":
            one_day_net_rate = discounted_rate(gross_rate, req.discount_pct)
            working_budget = max(working_budget, min(one_day_net_rate, remaining_budget))
        if not is_foc and working_budget <= 0 and not force:
            return False

        if buy_type == "CPM":
            planned_views = int(
                min(
                    available,
                    max(
                        settings.min_slot_views,
                        int(max(working_budget, max(rate, 0.01)) * 1000 / max(rate, 0.01)),
                    ),
                )
            )
            planned_views = min(available, round_views_to_block(planned_views))
            if planned_views > available:
                planned_views = floor_views_to_block(available)
            if planned_views < settings.min_slot_views:
                return False
            if is_foc:
                net_amount = 0.0
                gross_amount = 0.0
                gross_rate_avg = gross_rate
                net_rate_avg = rate
            else:
                gross_amount, net_amount, gross_rate_avg, net_rate_avg = _cpm_row_pricing(
                    meta,
                    candidate,
                    row_from,
                    row_to,
                    planned_views,
                    req.discount_pct,
                    settings.default_cpm,
                )
                if gross_amount <= 0 and net_amount <= 0:
                    return False
        else:
            cpd_days = list(iter_dates(row_from, row_to))
            scheduled_daily_rates = [
                float((meta.get("cpd_rate_schedule") or {}).get(day.isoformat()) or gross_rate)
                for day in cpd_days
            ]
            if is_foc:
                max_days = len(cpd_days)
                gross_amount = 0.0
                net_amount = 0.0
            else:
                gross_amount = 0.0
                net_amount = 0.0
                max_days = 0
                for daily_rate in scheduled_daily_rates:
                    daily_net_rate = discounted_rate(daily_rate, req.discount_pct)
                    if net_amount + daily_net_rate > working_budget + 1e-9:
                        break
                    gross_amount += daily_rate
                    net_amount += daily_net_rate
                    max_days += 1
                gross_amount = round(gross_amount, 2)
                net_amount = round(net_amount, 2)
                if max_days <= 0:
                    return False
            planned_views = min(available, int(candidate.views / max(candidate.active_days, 1) * max_days)) or None
            row_to = row_from.fromordinal(row_from.toordinal() + max_days - 1)
            days = inclusive_days(row_from, row_to)
            gross_rate_avg = round(gross_amount / max(days, 1), 4) if not is_foc else gross_rate
            net_rate_avg = round(net_amount / max(days, 1), 4) if not is_foc else rate

        if not is_foc and spent_total + net_amount > req.budget:
            remaining_budget = round(max(req.budget - spent_total, 0), 2)
            if remaining_budget <= 0 and not force:
                return False
            if buy_type == "CPM":
                capped_views = floor_views_to_block(int(remaining_budget * 1000 / max(rate, 0.01)))
                capped_views = min(capped_views, floor_views_to_block(available))
                if capped_views < settings.min_slot_views:
                    return False
                planned_views = capped_views
                gross_amount, net_amount, gross_rate_avg, net_rate_avg = _cpm_row_pricing(
                    meta,
                    candidate,
                    row_from,
                    row_to,
                    planned_views,
                    req.discount_pct,
                    settings.default_cpm,
                )
                if gross_amount <= 0 and net_amount <= 0:
                    return False
                while planned_views >= settings.min_slot_views and spent_total + net_amount > req.budget:
                    planned_views -= 100
                    gross_amount, net_amount, gross_rate_avg, net_rate_avg = _cpm_row_pricing(
                        meta,
                        candidate,
                        row_from,
                        row_to,
                        planned_views,
                        req.discount_pct,
                        settings.default_cpm,
                    )
                if planned_views < settings.min_slot_views or spent_total + net_amount > req.budget:
                    return False
            else:
                cpd_days = list(iter_dates(row_from, row_to))
                gross_amount = 0.0
                net_amount = 0.0
                fitted_days = 0
                for day in cpd_days:
                    daily_gross_rate = float((meta.get("cpd_rate_schedule") or {}).get(day.isoformat()) or gross_rate)
                    daily_net_rate = discounted_rate(daily_gross_rate, req.discount_pct)
                    if net_amount + daily_net_rate > remaining_budget + 1e-9:
                        break
                    gross_amount += daily_gross_rate
                    net_amount += daily_net_rate
                    fitted_days += 1
                if fitted_days <= 0:
                    return False
                row_to = row_from.fromordinal(row_from.toordinal() + fitted_days - 1)
                days = inclusive_days(row_from, row_to)
                planned_views = min(available, int(candidate.views / max(candidate.active_days, 1) * fitted_days)) or None
                gross_amount = round(gross_amount, 2)
                net_amount = round(net_amount, 2)
                gross_rate_avg = round(gross_amount / max(days, 1), 4)
                net_rate_avg = round(net_amount / max(days, 1), 4)

        rows.append(
            EditablePlanLine.model_validate(
                {
                    "id": line_id,
                    "from": row_from,
                    "to": row_to,
                    "country": candidate.country,
                    "page": page_from_slot(candidate.slot_code, candidate.publisher, candidate.page),
                    "marketplace": candidate.marketplace,
                    "category": candidate.category or "",
                    "zone": candidate.zone or "",
                    "dimension": candidate.dimension or get_slot_meta(slot_meta, candidate.country, candidate.slot_code).get("dimension", ""),
                    "asset": asset_from_slot(candidate.slot_code, candidate.slot_name),
                    "slot_name": candidate.slot_name or asset_from_slot(candidate.slot_code, candidate.slot_name),
                    "days": days,
                    "buyType": buy_type,
                    "rate": round(net_rate_avg, 4),
                    "gross_cpm": round(gross_rate_avg if buy_type == "CPM" else 0.0, 4),
                    "net_cpm": round(net_rate_avg if buy_type == "CPM" else 0.0, 4),
                    "views": planned_views,
                    "cost": round(net_amount, 2),
                    "gross_amount": round(gross_amount, 2),
                    "net_amount": round(net_amount, 2),
                    "discount_pct": req.discount_pct,
                    "phase": phase.name,
                    "brand": brand_name or req.brand,
                    "stype": stype,
                    "slot_code": candidate.slot_code,
                    "score": round(score_value, 4),
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
        spent_total += net_amount
        used_slot_phases.add((slot_key_value, phase.name))
        inventory_by_slot_phase[exact_inventory_key] = max(inventory_by_slot_phase[exact_inventory_key] - int(planned_views or 0), 0)
        return True

    candidates_by_slot_key: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        candidates_by_slot_key[slot_key(candidate.country, candidate.slot_code)].append(candidate)

    def selected_candidate_for_key(selected_key: str) -> Candidate | None:
        return preferred_candidate_for_slot(
            candidates_by_slot_key.get(selected_key, []),
            selected_slot_pricing_map.get(selected_key),
            req.objective,
        )

    candidate_by_slot_key = {
        key: selected_candidate_for_key(key)
        for key in candidates_by_slot_key.keys()
    }

    def marketplace_share_for(candidate: Candidate) -> float:
        shares = {name: share for name, share in marketplace_splits}
        if candidate.marketplace in shares:
            return shares[candidate.marketplace]
        return 1.0

    def comcat_bucket_for(candidate: Candidate) -> tuple[str, float]:
        matches = [
            (comcat_name, share, _slot_relevance_for_comcat(candidate, comcat_name))
            for comcat_name, share in comcat_splits
            if not comcat_name or _slot_relevance_for_comcat(candidate, comcat_name) > 0
        ]
        if not matches:
            return ("", 1.0 / max(len(comcat_splits), 1))
        comcat_name, share, _relevance = max(matches, key=lambda item: (item[2], item[1]))
        return (comcat_name, share)

    selected_phase_sequence = _weighted_phase_sequence(phases, phase_splits, len(selected_slot_key_list))
    selected_allocations: list[dict] = []
    selected_bucket_counts: dict[tuple[str, str, str, str, str], int] = defaultdict(int)
    selected_bucket_targets: dict[tuple[str, str, str, str, str], float] = defaultdict(float)
    for index, selected_key in enumerate(selected_slot_key_list):
        candidate = selected_candidate_for_key(selected_key)
        if not candidate:
            continue
        brand_name, brand_share = brand_splits[index % len(brand_splits)]
        phase = selected_phase_sequence[index] if index < len(selected_phase_sequence) else phases[index % len(phases)]
        country_share = 1.0 / max(len(countries), 1)
        comcat_name, comcat_share = comcat_bucket_for(candidate)
        bucket_key = (candidate.country, str(brand_name or ""), phase.name, candidate.marketplace, comcat_name)
        bucket_target = req.budget * (
            max(country_share, 0.01)
            * max(brand_share, 0.01)
            * max(phase_splits.get(phase.name, 0), 0.01)
            * max(marketplace_share_for(candidate), 0.01)
            * max(comcat_share, 0.01)
        )
        selected_bucket_counts[bucket_key] += 1
        selected_bucket_targets[bucket_key] = max(selected_bucket_targets[bucket_key], bucket_target)
        selected_allocations.append(
            {
                "key": selected_key,
                "candidate": candidate,
                "brand_name": brand_name,
                "brand_share": brand_share,
                "phase": phase,
                "bucket_key": bucket_key,
            }
        )
    selected_target_total = sum(selected_bucket_targets.values())
    # Preselected slots must appear, but committing the entire budget in this
    # first pass can lock in coarse phase quotas (for example, 1 of 4 selected
    # slots in a 30% launch phase).  Reserve 20% for the split-aware allocator
    # and CPM balancing pass below.
    selected_scale = ((req.budget * 0.8) / selected_target_total) if selected_target_total > 0 else 1.0
    selected_budget_by_key: dict[tuple[str, str, str], float] = {}
    for item in selected_allocations:
        bucket_key = item["bucket_key"]
        bucket_count = max(selected_bucket_counts.get(bucket_key, 1), 1)
        selected_budget_by_key[
            (str(item["key"]), item["phase"].name, str(item["brand_name"] or ""))
        ] = (selected_bucket_targets.get(bucket_key, 0.0) * selected_scale / bucket_count) if selected_target_total > 0 else req.budget / max(len(selected_allocations), 1)

    def row_matches_selected_pricing(row: EditablePlanLine, selected_key: str) -> bool:
        requested_model = selected_slot_pricing_map.get(selected_key)
        if not requested_model:
            return True
        row_model = normalize_pricing_model(getattr(row, "buyType", ""))
        return row_model == requested_model

    selected_type = _allocation_type(req.objective)
    for item in selected_allocations:
        selected_key = str(item["key"])
        if any(
            slot_key(row.country, row.slot_code) == selected_key and row_matches_selected_pricing(row, selected_key)
            for row in rows
            if row.slot_code
        ):
            continue
        candidate = item["candidate"]
        if not candidate:
            continue
        brand_name = str(item["brand_name"] or req.brand)
        phase = item["phase"]
        target_budget = selected_budget_by_key.get((selected_key, phase.name, brand_name), req.budget / max(len(selected_allocations), 1))
        selected_score = _candidate_score(candidate, req.objective)
        append_row(candidate, phase, selected_type, selected_score, target_budget, force=True, brand_name=brand_name)

    for country in countries:
        country_candidates = [c for c in candidates if c.country == country]
        if selected_slot_keys:
            country_candidates = [
                candidate
                for candidate in country_candidates
                if slot_key(candidate.country, candidate.slot_code) in selected_slot_keys
                and (
                    slot_key(candidate.country, candidate.slot_code) not in selected_slot_pricing_map
                    or candidate.pricing_model == selected_slot_pricing_map[slot_key(candidate.country, candidate.slot_code)]
                )
            ]
        if not country_candidates:
            continue

        for phase in phases:
            phase_share = phase_splits.get(phase.name, 0)
            for brand_name, brand_share in brand_splits:
                for marketplace_name, marketplace_share in marketplace_splits:
                    marketplace_candidates = [candidate for candidate in country_candidates if candidate.marketplace == marketplace_name]
                    if not marketplace_candidates:
                        continue
                    for comcat_name, comcat_share in comcat_splits:
                        comcat_candidates = [
                            candidate
                            for candidate in marketplace_candidates
                            if not comcat_name or _slot_relevance_for_comcat(candidate, comcat_name) > 0
                        ]
                        if not comcat_candidates:
                            continue
                        for stype, weight, score_name in _allocation_tracks(req):
                            target = country_budget * brand_share * marketplace_share * phase_share * comcat_share * weight
                            if target <= 0:
                                continue
                            already = sum(
                                row.cost
                                for row in rows
                                if row.country == country
                                and row.marketplace == marketplace_name
                                and row.brand == brand_name
                                and row.phase == phase.name
                                and row.stype == stype
                                and (not comcat_name or _row_relevance_for_comcat(row, comcat_name) > 0)
                            )
                            remaining_target = max(target - already, 0)
                            ranked = _objective_diverse_order(comcat_candidates, req.objective)
                            base_goal = _per_country_min_lines(req) * brand_share * phase_share * marketplace_share * comcat_share
                            phase_goal = max(1, min(settings.max_lines_per_phase, round(base_goal)))
                            used_in_phase = len([
                                row
                                for row in rows
                                if row.country == country
                                and row.marketplace == marketplace_name
                                and row.brand == brand_name
                                and row.phase == phase.name
                                and row.stype == stype
                                and (not comcat_name or _row_relevance_for_comcat(row, comcat_name) > 0)
                            ])
                            for candidate in ranked:
                                if used_in_phase >= phase_goal:
                                    break
                                target_budget = remaining_target / max(phase_goal - used_in_phase, 1) if remaining_target > 0 else country_budget * brand_share * marketplace_share * comcat_share / max(_per_country_min_lines(req), 1)
                                if append_row(candidate, phase, stype, getattr(candidate, score_name), target_budget, brand_name=brand_name):
                                    used_in_phase += 1
                                    remaining_target = max(remaining_target - target_budget, 0)

    per_country_min = _per_country_min_lines(req)
    for country in countries:
        have = len([r for r in rows if r.country == country and (r.slot_code or "").strip()])
        if have >= per_country_min:
            continue
        country_candidates = [c for c in candidates if c.country == country]
        if selected_slot_keys:
            preferred = [
                candidate
                for key in selected_slot_keys
                if key.startswith(f"{country}|")
                for candidate in [selected_candidate_for_key(key)]
                if candidate
            ]
            country_candidates = preferred or [
                candidate
                for candidate in country_candidates
                if slot_key(candidate.country, candidate.slot_code) in selected_slot_keys
                and (
                    slot_key(candidate.country, candidate.slot_code) not in selected_slot_pricing_map
                    or candidate.pricing_model == selected_slot_pricing_map[slot_key(candidate.country, candidate.slot_code)]
                )
            ]
        ranked = _objective_diverse_order(country_candidates, req.objective)
        for idx, candidate in enumerate(ranked):
            if have >= per_country_min or spent_total >= req.budget:
                break
            phase = phases[idx % len(phases)]
            brand_name, brand_share = brand_splits[idx % len(brand_splits)]
            target_budget = (max(req.budget - spent_total, 0) / max(per_country_min - have, 1)) * max(brand_share, 0.01)
            chosen_type = _allocation_type(req.objective)
            chosen_score = _candidate_score(candidate, req.objective)
            key = slot_key(candidate.country, candidate.slot_code)
            if append_row(candidate, phase, chosen_type, chosen_score, target_budget, force=key in selected_slot_keys, brand_name=brand_name):
                have += 1

    existing_selected_keys = {
        selected_key
        for selected_key in selected_slot_key_list
        if any(
            slot_key(row.country, row.slot_code) == selected_key and row_matches_selected_pricing(row, selected_key)
            for row in rows
            if row.slot_code
        )
    }
    missing_selected = [selected_key for selected_key in selected_slot_key_list if selected_key not in existing_selected_keys]
    for index, selected_key in enumerate(missing_selected):
        candidate = selected_candidate_for_key(selected_key)
        if not candidate:
            continue
        phase = phases[index % len(phases)]
        brand_name, brand_share = brand_splits[index % len(brand_splits)]
        selected_stype = _allocation_type(req.objective)
        selected_score = _candidate_score(candidate, req.objective)
        remaining_slots = max(len(missing_selected) - index, 1)
        target_budget = (max(req.budget - spent_total, 0) / remaining_slots) * max(brand_share, 0.01)
        append_row(candidate, phase, selected_stype, selected_score, target_budget, force=True, brand_name=brand_name)

    # CPM is continuously scalable, so use remaining inventory to close phase and
    # marketplace deficits before reporting the plan.  This also avoids leaving a
    # material budget remainder merely because the initial line quotas were met.
    topup_added = 0.0
    topup_iterations = 0
    target_utilization = 0.995
    while spent_total < req.budget * target_utilization and topup_iterations < 500:
        remaining_budget = round(max(req.budget - spent_total, 0), 2)
        if remaining_budget <= 0:
            break
        phase_spend = defaultdict(float)
        marketplace_spend = defaultdict(float)
        for existing_row in rows:
            if str(existing_row.buyType or "").upper() == "OFF-DECK":
                continue
            row_spend = float(existing_row.cost or existing_row.net_amount or 0)
            phase_spend[existing_row.phase] += row_spend
            marketplace_spend[existing_row.marketplace] += row_spend

        eligible_rows: list[tuple[float, EditablePlanLine, int, float]] = []
        marketplace_share_map = dict(marketplace_splits)
        for existing_row in rows:
            if normalize_pricing_model(existing_row.buyType) != "CPM" or existing_row.manual or existing_row.locked:
                continue
            inventory_key = (existing_row.country, existing_row.slot_code, existing_row.phase)
            remaining_views = floor_views_to_block(inventory_by_slot_phase.get(inventory_key, 0))
            net_cpm = float(existing_row.net_cpm or existing_row.rate or 0)
            if remaining_views < 100 or net_cpm <= 0 or remaining_budget + 1e-9 < net_cpm * 0.1:
                continue
            phase_target = req.budget * phase_splits.get(existing_row.phase, 0)
            marketplace_target = req.budget * marketplace_share_map.get(existing_row.marketplace, 0)
            phase_gap = phase_target - phase_spend[existing_row.phase]
            marketplace_gap = marketplace_target - marketplace_spend[existing_row.marketplace]
            deficit_score = (
                phase_gap / max(phase_target, 1.0)
                + marketplace_gap / max(marketplace_target, 1.0)
            )
            eligible_rows.append((deficit_score, existing_row, remaining_views, net_cpm))
        if not eligible_rows:
            break

        _deficit_score, row_to_grow, capacity_views, net_cpm = max(
            eligible_rows,
            key=lambda item: (item[0], item[1].score, item[2]),
        )
        phase_target = req.budget * phase_splits.get(row_to_grow.phase, 0)
        marketplace_target = req.budget * marketplace_share_map.get(row_to_grow.marketplace, 0)
        positive_gaps = [
            gap
            for gap in (
                phase_target - phase_spend[row_to_grow.phase],
                marketplace_target - marketplace_spend[row_to_grow.marketplace],
            )
            if gap > 0.01
        ]
        desired_spend = min(positive_gaps) if positive_gaps else remaining_budget
        desired_spend = min(max(desired_spend, net_cpm * 0.1), remaining_budget)
        affordable_views = floor_views_to_block(int(desired_spend * 1000 / net_cpm))
        added_views = min(capacity_views, affordable_views)
        if added_views < 100:
            # Mark this cell exhausted for the balancing pass and try another.
            inventory_by_slot_phase[(row_to_grow.country, row_to_grow.slot_code, row_to_grow.phase)] = 0
            topup_iterations += 1
            continue
        added_net = round(added_views * net_cpm / 1000, 2)
        if added_net <= 0 or added_net > remaining_budget + 0.01:
            break
        gross_cpm = float(row_to_grow.gross_cpm or 0)
        added_gross = round(added_views * gross_cpm / 1000, 2) if gross_cpm > 0 else added_net
        row_to_grow.views = int(row_to_grow.views or 0) + added_views
        row_to_grow.cost = round(float(row_to_grow.cost or 0) + added_net, 2)
        row_to_grow.net_amount = round(float(row_to_grow.net_amount or 0) + added_net, 2)
        row_to_grow.gross_amount = round(float(row_to_grow.gross_amount or 0) + added_gross, 2)
        inventory_key = (row_to_grow.country, row_to_grow.slot_code, row_to_grow.phase)
        inventory_by_slot_phase[inventory_key] = max(inventory_by_slot_phase.get(inventory_key, 0) - added_views, 0)
        spent_total = round(spent_total + added_net, 2)
        topup_added = round(topup_added + added_net, 2)
        topup_iterations += 1

    final_selected_keys = {
        selected_key
        for selected_key in selected_slot_key_list
        if any(
            slot_key(row.country, row.slot_code) == selected_key and row_matches_selected_pricing(row, selected_key)
            for row in rows
            if row.slot_code
        )
    }
    omitted_selected_slots = []
    remaining_budget_after_plan = round(max(req.budget - spent_total, 0), 2)
    for selected_key in selected_slot_key_list:
        if selected_key in final_selected_keys:
            continue
        candidate = selected_candidate_for_key(selected_key)
        country, _, selected_slot_code = selected_key.partition("|")
        meta = get_slot_meta(slot_meta, country, selected_slot_code)
        allowed_models = pricing_options_for_meta(meta)
        requested_model = selected_slot_pricing_map.get(selected_key)
        if requested_model not in allowed_models:
            requested_model = allowed_models[0] if allowed_models else "CPM"
        if not candidate:
            reason = "No matching candidate was available for the selected slot and buy type."
        else:
            phase_inventory = sum(inventory_by_slot_phase.get((candidate.country, candidate.slot_code, phase.name), 0) for phase in phases)
            if phase_inventory <= 0:
                reason = "No inventory was available for this slot in the selected phases."
            elif requested_model == "CPD":
                one_day_rate = discounted_rate(
                    float(meta.get("cpd_rate") or candidate.slot_rate or settings.default_cpd),
                    req.discount_pct,
                )
                reason = (
                    f"Selected CPD slot needs at least USD {round(one_day_rate, 2):,.2f} net for one day, "
                    f"but only USD {remaining_budget_after_plan:,.2f} remained."
                )
            else:
                min_block_cost = discounted_rate(
                    float(meta.get("cpm_rate") or candidate.slot_rate or settings.default_cpm),
                    req.discount_pct,
                ) * settings.min_slot_views / 1000
                reason = (
                    f"Selected CPM slot needs at least USD {round(min_block_cost, 2):,.2f} net for the minimum view block, "
                    f"but only USD {remaining_budget_after_plan:,.2f} remained."
                )
        omitted_selected_slots.append(
            {
                "slot_key": selected_key,
                "slot_name": candidate.slot_name if candidate else (meta.get("slot_name") or selected_slot_code),
                "buy_type": requested_model,
                "reason": reason,
            }
        )

    selected_offdeck_slots = [
        slot for slot in (getattr(req, "selected_offdeck_slots", []) or [])
        if isinstance(slot, dict) and str(slot.get("slot_key") or slot.get("slot_code") or slot.get("slot_name") or "").strip()
    ]
    offdeck_budget = round(max(float(getattr(req, "offdeck_budget", 0.0) or 0.0), 0.0), 2)
    if selected_offdeck_slots and offdeck_budget > 0:
        allocated_offdeck = 0.0
        for index, slot in enumerate(selected_offdeck_slots):
            slot_value = round(max(float(slot.get("value") or 0.0), 0.0), 2)
            if slot_value <= 0 or allocated_offdeck >= offdeck_budget:
                continue
            slot_total = min(slot_value, round(offdeck_budget - allocated_offdeck, 2))
            slot_code_value = str(slot.get("slot_code") or slot.get("slot_key") or slot.get("slot_name") or f"offdeck_{index + 1}").strip()
            slot_name_value = str(slot.get("slot_name") or slot_code_value).strip()
            country_value = str(slot.get("country") or "").strip().lower() or (req.countries[0] if req.countries else "")
            page_value = str(slot.get("page") or slot.get("category") or "Off-deck").strip() or "Off-deck"
            selected_phases = [
                Phase.model_validate({"name": phase.get("name") or f"Phase {phase_index + 1}", "from": phase.get("from"), "to": phase.get("to")})
                for phase_index, phase in enumerate(slot.get("phases") or [])
                if isinstance(phase, dict) and phase.get("from") and phase.get("to")
            ]
            row_phases = selected_phases or [Phase.model_validate({"name": "All phases", "from": req.start_date, "to": req.end_date})]
            per_phase_value = round(slot_total / len(row_phases), 2)
            allocated_slot = 0.0
            for phase_index, phase in enumerate(row_phases):
                net_amount = round(slot_total - allocated_slot, 2) if phase_index == len(row_phases) - 1 else per_phase_value
                discount_factor = max(1 - (float(req.discount_pct or 0) / 100), 0.0001)
                gross_amount = round(net_amount / discount_factor, 2) if req.discount_pct < 100 else net_amount
                allocated_slot = round(allocated_slot + net_amount, 2)
                rows.append(
                    EditablePlanLine.model_validate(
                        {
                            "id": line_id,
                            "from": phase.from_date,
                            "to": phase.to_date,
                            "country": country_value,
                            "page": page_value,
                            "marketplace": "OFF-DECK",
                            "category": page_value,
                            "zone": "",
                            "dimension": str(slot.get("dimension") or "").strip(),
                            "asset": slot_name_value,
                            "slot_name": slot_name_value,
                            "days": inclusive_days(phase.from_date, phase.to_date),
                            "buyType": "OFF-DECK",
                            "rate": 0,
                            "gross_cpm": 0,
                            "net_cpm": 0,
                            "views": 0,
                            "cost": net_amount,
                            "gross_amount": gross_amount,
                            "net_amount": net_amount,
                            "discount_pct": req.discount_pct,
                            "phase": phase.name,
                            "brand": req.brand,
                            "stype": "reach",
                            "slot_code": slot_code_value,
                            "score": 0,
                            "available_views": 0,
                            "historical_ctr": None,
                            "historical_roas": None,
                            "historical_cpm": None,
                            "note": "",
                            "manual": False,
                            "locked": False,
                        }
                    )
                )
                line_id += 1
            allocated_offdeck = round(allocated_offdeck + slot_total, 2)

    on_deck_rows = [row for row in rows if str(row.buyType or "").upper() != "OFF-DECK"]
    on_deck_total = sum(float(row.cost or row.net_amount or 0) for row in on_deck_rows)

    def actual_pct_split(values: dict[str, float]) -> dict[str, float]:
        if on_deck_total <= 0:
            return {key: 0.0 for key in values}
        return {key: round((value / on_deck_total) * 100, 2) for key, value in values.items()}

    actual_marketplace_spend: dict[str, float] = defaultdict(float)
    actual_phase_spend: dict[str, float] = defaultdict(float)
    actual_country_spend: dict[str, float] = defaultdict(float)
    actual_comcat_spend: dict[str, float] = defaultdict(float)
    for row in on_deck_rows:
        row_spend = float(row.cost or row.net_amount or 0)
        actual_marketplace_spend[row.marketplace or "unknown"] += row_spend
        actual_phase_spend[row.phase or "unknown"] += row_spend
        actual_country_spend[row.country or "unknown"] += row_spend
        matched_comcats = [
            (comcat_name, _row_relevance_for_comcat(row, comcat_name))
            for comcat_name, _share in comcat_splits
            if comcat_name and _row_relevance_for_comcat(row, comcat_name) > 0
        ]
        if matched_comcats:
            comcat_name, _relevance = max(matched_comcats, key=lambda item: item[1])
            actual_comcat_spend[comcat_name] += row_spend
        else:
            actual_comcat_spend["unmapped"] += row_spend

    diagnostics = {
        "candidate_count": len(candidates),
        "base_candidate_count": len(base_candidates),
        "synthetic_candidate_count": len([candidate for candidate in candidates if candidate.synthetic]),
        "historical_rows": len(historical_rows),
        "inventory_rows": len(inventory_rows),
        "inventory_total_available": sum(max(int(row.get("available_views") or 0), 0) for row in inventory_rows),
        "allocated": round(sum(row.cost for row in rows), 2),
        "on_deck_allocated": round(sum(row.cost for row in rows if str(row.buyType or "").upper() != "OFF-DECK"), 2),
        "offdeck_allocated": round(sum(row.cost for row in rows if str(row.buyType or "").upper() == "OFF-DECK"), 2),
        "offdeck_slot_count": len(selected_offdeck_slots),
        "budget_utilization_pct": round((on_deck_total / req.budget) * 100, 2) if req.budget > 0 else 0.0,
        "budget_topup_added": topup_added,
        "budget_utilization_target_pct": 95.0,
        "cpd_minimum_budget_usd": MIN_CPD_BUDGET_USD,
        "per_country_min": per_country_min,
        "countries": countries,
        "marketplace": req.marketplace,
        "brand_budget_split": {name: round(share * 100, 2) for name, share in brand_splits if name},
        "marketplace_budget_split": {name: round(share * 100, 2) for name, share in marketplace_splits},
        "comcat_budget_split": {name: round(share * 100, 2) for name, share in comcat_splits if name},
        "phase_budget_split": {name: round(share * 100, 2) for name, share in phase_splits.items()},
        "actual_country_budget_split": actual_pct_split(dict(actual_country_spend)),
        "actual_marketplace_budget_split": actual_pct_split(dict(actual_marketplace_spend)),
        "actual_comcat_budget_split": actual_pct_split(dict(actual_comcat_spend)),
        "actual_phase_budget_split": actual_pct_split(dict(actual_phase_spend)),
        "country_row_counts": {country: len([row for row in rows if row.country == country and (row.slot_code or "").strip()]) for country in countries},
        "omitted_selected_slots": omitted_selected_slots,
    }

    return rows, diagnostics
