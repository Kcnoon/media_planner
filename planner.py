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
    cpm: float | None
    cpd: float | None
    ctr: float | None
    roas: float | None
    source_country: str | None = None
    synthetic: bool = False


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
    key = _page_key(*values)
    return key in GENERIC_PAGE_KEYS


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
    best = 0.0
    for value in haystacks:
        if not value:
            continue
        if value in comcat_values or any(comcat in value or value in comcat for comcat in comcat_values):
            best = max(best, 1.0)
    if comcat_tokens and haystack_tokens:
        best = max(best, len(comcat_tokens & haystack_tokens) / len(comcat_tokens))
    return best


def discounted_rate(rate: float, discount_pct: float) -> float:
    return round(rate * max(0.0, 1 - (discount_pct / 100.0)), 4)


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
    gross_fallback = float(meta.get("rate") or candidate.slot_rate or candidate.cpm or default_cpm)
    rate_schedule = meta.get("rate_schedule") or {}
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

        safe_cpm = candidate_slot_rate = float(meta.get("rate") or 0) or None
        if not safe_cpm:
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
                category=meta.get("category") or meta.get("page"),
                zone=meta.get("zone"),
                dimension=meta.get("dimension"),
                marketplace=marketplace_from_slot(slot_code, slot_name),
                publisher=meta.get("publisher") or row.get("publisher"),
                pricing_model=normalize_pricing_model(meta.get("pricing_model") or row.get("pricing_model")),
                slot_rate=candidate_slot_rate,
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
                source_country=country,
                synthetic=False,
            )
        )

    deduped: dict[tuple[str, str, str], Candidate] = {}
    for candidate in sorted(candidates, key=lambda c: (c.brand_specific, c.views), reverse=True):
        deduped.setdefault((candidate.country, candidate.slot_code, candidate.pricing_model), candidate)
    return list(deduped.values())


def expand_candidates_for_countries(
    req: MediaPlanRequest,
    candidates: list[Candidate],
    slot_meta: dict[tuple[str, str], dict],
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
                    score = max(candidate.reach_score, candidate.conv_score)
                    if meta.get("category") and candidate.category and str(meta.get("category")).strip().lower() == str(candidate.category).strip().lower():
                        similarity += 0.1
                    similarity += page_similarity * 0.8
                    similarity += min(target_relevance, 1.0) * 0.15
                    scored_sources.append((page_similarity, similarity, score, candidate))
                scored_sources.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
                source_candidates = [item[3] for item in scored_sources[:3]]
            if not source_candidates:
                continue
            source = max(source_candidates, key=lambda candidate: (candidate.brand_specific, max(candidate.reach_score, candidate.conv_score), candidate.views))
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
                    pricing_model=normalize_pricing_model(meta.get("pricing_model") or source.pricing_model),
                    slot_rate=float(meta.get("rate") or source.slot_rate or 0) or source.slot_rate,
                    views=source.views,
                    clicks=source.clicks,
                    revenue=source.revenue,
                    spends=source.spends,
                    active_days=source.active_days,
                    brand_specific=source.brand_specific,
                    reach_score=source.reach_score,
                    conv_score=source.conv_score,
                    cpm=source.cpm,
                    cpd=source.cpd,
                    ctr=source.ctr,
                    roas=source.roas,
                    source_country=source.country,
                    synthetic=True,
                )
            )

    deduped: dict[tuple[str, str, str], Candidate] = {}
    for candidate in sorted(expanded, key=lambda item: (not item.synthetic, item.brand_specific, max(item.reach_score, item.conv_score), item.views), reverse=True):
        deduped.setdefault((candidate.country, candidate.slot_code, candidate.pricing_model), candidate)
    return list(deduped.values())


def ensure_selected_slot_candidates(
    req: MediaPlanRequest,
    selected_slot_keys: set[str],
    candidates: list[Candidate],
    slot_meta: dict[tuple[str, str], dict],
    settings,
) -> list[Candidate]:
    if not selected_slot_keys:
        return candidates

    candidate_by_slot_key = {f"{candidate.country}|{candidate.slot_code}": candidate for candidate in candidates}
    ensured = list(candidates)

    for slot_key in selected_slot_keys:
        if slot_key in candidate_by_slot_key:
            continue
        country, _, slot_code = slot_key.partition("|")
        if not country or not slot_code:
            continue
        meta = slot_meta.get((country, slot_code))
        if not meta:
            continue

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
            key=lambda candidate: (candidate.brand_specific, max(candidate.reach_score, candidate.conv_score), candidate.views),
            default=None,
        )

        rate = float(meta.get("rate") or 0) or settings.default_cpm
        reach_score = source.reach_score if source else max(settings.min_slot_views / max(rate, 0.01), 1.0)
        conv_score = source.conv_score if source else reach_score

        ensured.append(
            Candidate(
                country=country,
                slot_code=slot_code,
                slot_name=slot_name,
                page=page,
                category=category,
                zone=meta.get("zone"),
                dimension=meta.get("dimension"),
                marketplace=marketplace,
                publisher=meta.get("publisher"),
                pricing_model=normalize_pricing_model(meta.get("pricing_model") or "cpm"),
                slot_rate=rate,
                views=source.views if source else settings.min_slot_views,
                clicks=source.clicks if source else 0,
                revenue=source.revenue if source else 0.0,
                spends=source.spends if source else 0.0,
                active_days=source.active_days if source else 1,
                brand_specific=source.brand_specific if source else False,
                reach_score=reach_score,
                conv_score=conv_score,
                cpm=source.cpm if source else None,
                cpd=source.cpd if source else None,
                ctr=source.ctr if source else None,
                roas=source.roas if source else None,
                source_country=source.country if source else country,
                synthetic=True,
            )
        )

    deduped: dict[tuple[str, str, str], Candidate] = {}
    for candidate in sorted(ensured, key=lambda item: (not item.synthetic, item.brand_specific, max(item.reach_score, item.conv_score), item.views), reverse=True):
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
    base_candidates = build_candidates(historical_rows, slot_meta, settings, req.marketplace)
    selected_slot_keys = {value for value in req.selected_slot_keys if value}
    candidates = expand_candidates_for_countries(req, base_candidates, slot_meta)
    candidates = ensure_selected_slot_candidates(req, selected_slot_keys, candidates, slot_meta, settings)
    inventory = _inventory_by_slot_phase(req, inventory_rows)
    phases = default_phases(req)
    seen: set[str] = set()
    seen_zone_category: set[tuple[str, str, str, str]] = set()
    suggestions: list[dict] = []

    ranked = sorted(candidates, key=lambda candidate: _candidate_score(candidate, req.objective), reverse=True)
    ordered_candidates: list[Candidate] = []
    for country in [country for country in req.countries if country]:
        country_ranked = [candidate for candidate in ranked if candidate.country == country]
        for candidate in country_ranked:
            available = sum(inventory.get((candidate.country, candidate.slot_code, phase.name), 0) for phase in phases)
            if available > 0:
                ordered_candidates.append(candidate)
                break
    ordered_candidates.extend(ranked)

    for candidate in ordered_candidates:
        slot_key = f"{candidate.country}|{candidate.slot_code}"
        if slot_key in seen:
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
        seen.add(slot_key)
        seen_zone_category.add(zone_category_key)
        suggestions.append(
            {
                "slot_key": slot_key,
                "country": candidate.country,
                "slot_code": candidate.slot_code,
                "slot_name": candidate.slot_name or asset_from_slot(candidate.slot_code, candidate.slot_name),
                "page": page_from_slot(candidate.slot_code, candidate.publisher, candidate.page),
                "marketplace": candidate.marketplace,
                "category": candidate.category or "",
                "zone": candidate.zone or "",
                "dimension": candidate.dimension or "",
                "pricing_model": candidate.pricing_model,
                "gross_cpm": round(candidate.slot_rate or 0.0, 4) if candidate.pricing_model == "CPM" else 0.0,
                "net_cpm": round(discounted_rate(candidate.slot_rate or 0.0, req.discount_pct), 4) if candidate.pricing_model == "CPM" else 0.0,
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
    base_candidates = build_candidates(historical_rows, slot_meta, settings, req.marketplace)
    selected_slot_keys = {value for value in req.selected_slot_keys if value}
    candidates = expand_candidates_for_countries(req, base_candidates, slot_meta)
    candidates = ensure_selected_slot_candidates(req, selected_slot_keys, candidates, slot_meta, settings)
    if not candidates:
        return [], {"reason": "No historical delivery rows found for the selected brand/comcat/countries."}

    inventory_by_slot_phase = _inventory_by_slot_phase(req, inventory_rows)

    phases = default_phases(req)
    reach_weight, roas_weight = objective_weights(req)

    rows = _coerce_rows(req)
    spent_total = sum(row.cost for row in rows)
    line_id = max([row.id for row in rows], default=0) + 1

    excluded_slot_keys = set(req.excluded_slot_keys) | {f"{row.country}|{row.slot_code}" for row in rows if row.slot_code}
    foc_slot_keys = {value for value in req.foc_slot_keys if value}

    countries = [c for c in req.countries if c]
    if not countries:
        return [], {"reason": "No countries selected."}

    country_budget = req.budget / len(countries)
    spent_by_country: dict[str, float] = defaultdict(float)
    for row in rows:
        spent_by_country[row.country] += float(row.cost or row.net_amount or 0)

    def append_row(candidate: Candidate, phase: Phase, stype: str, score_value: float, target_budget: float, force: bool = False) -> bool:
        nonlocal line_id, spent_total
        slot_key = f"{candidate.country}|{candidate.slot_code}"
        if not force and slot_key in excluded_slot_keys:
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

        buy_type = "Cost Per Day" if candidate.pricing_model == "CPD" else "CPM"
        is_foc = slot_key in foc_slot_keys
        meta = slot_meta.get((candidate.country, candidate.slot_code), {})
        if buy_type == "Cost Per Day":
            gross_rate = round(candidate.slot_rate or candidate.cpd or settings.default_cpd, 4)
        else:
            gross_rate = round(float(meta.get("rate") or candidate.slot_rate or candidate.cpm or settings.default_cpm), 4)
        rate = discounted_rate(gross_rate, req.discount_pct)

        remaining_budget = max(req.budget - spent_total, 0)
        working_budget = max(min(target_budget, remaining_budget), 0)
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
        else:
            max_days = min(days, max(int(max(working_budget, max(rate, 0.01)) // max(rate, 0.01)), 1))
            net_amount = 0.0 if is_foc else round(rate * max_days, 2)
            gross_amount = 0.0 if is_foc else gross_from_net(net_amount, req.discount_pct)
            planned_views = min(available, int(candidate.views / max(candidate.active_days, 1) * max_days)) or None
            row_to = row_from.fromordinal(row_from.toordinal() + max_days - 1)
            days = inclusive_days(row_from, row_to)
            gross_rate_avg = gross_rate
            net_rate_avg = rate

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
                net_amount = remaining_budget
                if net_amount <= 1 and not force:
                    return False
                gross_amount = gross_from_net(net_amount, req.discount_pct)

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
                    "dimension": candidate.dimension or slot_meta.get((candidate.country, candidate.slot_code), {}).get("dimension", ""),
                    "asset": asset_from_slot(candidate.slot_code, candidate.slot_name),
                    "slot_name": candidate.slot_name or asset_from_slot(candidate.slot_code, candidate.slot_name),
                    "days": days,
                    "buyType": buy_type,
                    "rate": round(net_rate_avg if buy_type == "CPM" else rate, 4),
                    "gross_cpm": round(gross_rate_avg if buy_type == "CPM" else 0.0, 4),
                    "net_cpm": round(net_rate_avg if buy_type == "CPM" else 0.0, 4),
                    "views": planned_views,
                    "cost": round(net_amount, 2),
                    "gross_amount": round(gross_amount, 2),
                    "net_amount": round(net_amount, 2),
                    "discount_pct": req.discount_pct,
                    "phase": phase.name,
                    "brand": req.brand,
                    "stype": stype,
                    "slot_code": candidate.slot_code,
                    "score": round(score_value, 4),
                    "available_views": available,
                    "historical_ctr": candidate.ctr,
                    "historical_roas": candidate.roas,
                    "historical_cpm": candidate.cpm,
                    "note": "FOC selected slot" if is_foc else "",
                    "manual": False,
                    "locked": False,
                }
            )
        )
        line_id += 1
        spent_total += net_amount
        excluded_slot_keys.add(slot_key)
        inventory_by_slot_phase[exact_inventory_key] = max(inventory_by_slot_phase[exact_inventory_key] - int(planned_views or 0), 0)
        return True

    total_days = sum(inclusive_days(p.from_date, p.to_date) for p in phases)
    for country in countries:
        country_candidates = [c for c in candidates if c.country == country]
        if selected_slot_keys:
            country_candidates = [candidate for candidate in country_candidates if f"{candidate.country}|{candidate.slot_code}" in selected_slot_keys]
        if not country_candidates:
            continue

        for phase in phases:
            phase_days = inclusive_days(phase.from_date, phase.to_date)
            phase_share = phase_days / max(total_days, 1)
            for stype, weight, score_name in (
                ("reach", reach_weight, "reach_score"),
                ("conv", roas_weight, "conv_score"),
            ):
                target = country_budget * phase_share * weight
                if target <= 0:
                    continue
                already = sum(row.cost for row in rows if row.country == country and row.phase == phase.name and row.stype == stype)
                remaining_target = max(target - already, 0)
                ranked = sorted(country_candidates, key=lambda c: getattr(c, score_name), reverse=True)
                phase_goal = max(1, min(settings.max_lines_per_phase, round(_per_country_min_lines(req) * phase_share)))
                used_in_phase = len([row for row in rows if row.country == country and row.phase == phase.name and row.stype == stype])
                for candidate in ranked:
                    if used_in_phase >= phase_goal:
                        break
                    target_budget = remaining_target / max(phase_goal - used_in_phase, 1) if remaining_target > 0 else country_budget / max(_per_country_min_lines(req), 1)
                    if append_row(candidate, phase, stype, getattr(candidate, score_name), target_budget):
                        used_in_phase += 1
                        remaining_target = max(remaining_target - target_budget, 0)

    per_country_min = _per_country_min_lines(req)
    candidate_by_slot_key = {f"{candidate.country}|{candidate.slot_code}": candidate for candidate in candidates}
    for country in countries:
        have = len([r for r in rows if r.country == country and (r.slot_code or "").strip()])
        if have >= per_country_min:
            continue
        country_candidates = [c for c in candidates if c.country == country]
        if selected_slot_keys:
            preferred = [candidate_by_slot_key[key] for key in selected_slot_keys if key in candidate_by_slot_key and key.startswith(f"{country}|")]
            country_candidates = preferred or [candidate for candidate in country_candidates if f"{candidate.country}|{candidate.slot_code}" in selected_slot_keys]
        ranked = sorted(country_candidates, key=lambda c: max(c.reach_score, c.conv_score), reverse=True)
        for idx, candidate in enumerate(ranked):
            if have >= per_country_min or spent_total >= req.budget:
                break
            phase = phases[idx % len(phases)]
            target_budget = max(req.budget - spent_total, 0) / max(per_country_min - have, 1)
            chosen_type = "reach" if candidate.reach_score >= candidate.conv_score else "conv"
            chosen_score = max(candidate.reach_score, candidate.conv_score)
            if append_row(candidate, phase, chosen_type, chosen_score, target_budget, force=slot_key in selected_slot_keys if (slot_key := f"{candidate.country}|{candidate.slot_code}") else False):
                have += 1

    existing_slot_keys = {f"{row.country}|{row.slot_code}" for row in rows if row.slot_code}
    missing_selected = [slot_key for slot_key in selected_slot_keys if slot_key not in existing_slot_keys]
    for index, slot_key in enumerate(missing_selected):
        candidate = candidate_by_slot_key.get(slot_key)
        if not candidate:
            continue
        phase = phases[index % len(phases)]
        selected_stype = "conv" if req.objective == "roas" or candidate.conv_score > candidate.reach_score else "reach"
        selected_score = _candidate_score(candidate, req.objective)
        remaining_slots = max(len(missing_selected) - index, 1)
        target_budget = max(req.budget - spent_total, 0) / remaining_slots
        append_row(candidate, phase, selected_stype, selected_score, target_budget, force=True)

    diagnostics = {
        "candidate_count": len(candidates),
        "base_candidate_count": len(base_candidates),
        "synthetic_candidate_count": len([candidate for candidate in candidates if candidate.synthetic]),
        "historical_rows": len(historical_rows),
        "inventory_rows": len(inventory_rows),
        "inventory_total_available": sum(max(int(row.get("available_views") or 0), 0) for row in inventory_rows),
        "allocated": round(sum(row.cost for row in rows), 2),
        "per_country_min": per_country_min,
        "countries": countries,
        "marketplace": req.marketplace,
        "country_row_counts": {country: len([row for row in rows if row.country == country and (row.slot_code or "").strip()]) for country in countries},
    }

    return rows, diagnostics
