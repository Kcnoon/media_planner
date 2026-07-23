"""RoAS-efficiency refinement (additive, isolated).

The deterministic allocator (`planner.plan_media`) spends the whole budget, which — when
high-RoAS inventory is scarce — spills most of the money into low-RoAS, high-inventory
slots and drags blended RoAS down. This module offers a RoAS-aware refinement: rank the
already-selected on-deck lines by (guardrailed) RoAS and keep the highest-RoAS subset that
still meets a target budget utilisation, trimming the dilutive tail.

It does NOT touch planner.py/scoring/the allocator. It runs (optionally) on the finished
plan. The cutoff is a *tunable benchmark* (`roas_target_utilization`), deliberately not
hardcoded to a RoAS number — the caller decides how much spend to trade for RoAS. The full
efficient frontier is returned so a UI/analyst can pick the point.
"""
from __future__ import annotations

from typing import Any

from planner import normalize_objective


def _get(row: Any, *names: str, default=None):
    """Read a field from either an EditablePlanLine (attributes) or a plain dict."""
    for name in names:
        if isinstance(row, dict):
            if name in row and row[name] not in (None, ""):
                return row[name]
        else:
            val = getattr(row, name, None)
            if val not in (None, ""):
                return val
    return default


def _is_offdeck(row: Any) -> bool:
    return str(_get(row, "buyType", "buy_type", default="") or "").upper() == "OFF-DECK"


def _net(row: Any) -> float:
    try:
        return float(_get(row, "net_amount", "cost", default=0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _roas(row: Any) -> float:
    try:
        return float(_get(row, "historical_roas", default=0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _trusted(row: Any, settings) -> int:
    """1 if the slot's RoAS is backed by enough delivery to trust; else 0 (ranked last so a
    thin, flukey-high-RoAS slot can't jump the queue)."""
    min_views = getattr(settings, "min_slot_views", 1000)
    return int(_roas(row) > 0 and float(_get(row, "views", default=0) or 0) >= min_views)


def _blended_roas(rows: list[dict]) -> float:
    cost = sum(_net(r) for r in rows)
    if cost <= 0:
        return 0.0
    return sum(_roas(r) * _net(r) for r in rows) / cost


def roas_frontier(rows: list[dict], settings) -> list[dict[str, Any]]:
    """Efficient frontier: keep the top-k on-deck slots by RoAS and report the cumulative
    spend + blended RoAS at each k. Adding lower-RoAS slots raises spend and lowers RoAS."""
    on_deck = [r for r in rows if not _is_offdeck(r)]
    ranked = sorted(on_deck, key=lambda r: (_trusted(r, settings), _roas(r)), reverse=True)
    points: list[dict[str, Any]] = []
    cost = 0.0
    rev = 0.0
    for i, r in enumerate(ranked, 1):
        c = _net(r)
        cost += c
        rev += _roas(r) * c
        points.append({
            "lines": i,
            "spend": round(cost),
            "blended_roas": round(rev / cost, 2) if cost else 0.0,
            "floor_roas": round(_roas(r), 1),
            "slot": _get(r, "slot_name", "asset", "slot_code", default="slot"),
        })
    return points


def refine_plan_for_roas(rows: list[dict], req, settings) -> tuple[list[dict], dict]:
    """Keep the highest-RoAS on-deck lines that together reach the target budget utilisation
    (plus a minimum line count); trim the dilutive tail. Off-deck lines are left untouched.
    Returns (possibly-reduced rows, info). No-op unless objective is RoAS and it's enabled."""
    if normalize_objective(getattr(req, "objective", "")) != "roas":
        return rows, {"applied": False, "reason": "objective is not roas"}
    if not getattr(settings, "roas_refine_enabled", True):
        return rows, {"applied": False, "reason": "disabled"}

    on_deck = [r for r in rows if not _is_offdeck(r)]
    off_deck = [r for r in rows if _is_offdeck(r)]
    min_lines = max(int(getattr(settings, "roas_refine_min_lines", 4)), 1)
    if len(on_deck) <= min_lines:
        return rows, {"applied": False, "reason": "too few on-deck lines to refine"}

    budget = float(getattr(req, "budget", 0) or 0)
    utilization = float(getattr(settings, "roas_target_utilization", 0.6) or 0.6)
    target_spend = budget * max(0.0, min(utilization, 1.0))

    ranked = sorted(on_deck, key=lambda r: (_trusted(r, settings), _roas(r)), reverse=True)
    kept: list[dict] = []
    spend = 0.0
    for r in ranked:
        kept.append(r)
        spend += _net(r)
        if len(kept) >= min_lines and spend >= target_spend:
            break
    dropped = ranked[len(kept):]
    if not dropped:
        return rows, {"applied": False, "reason": "no dilutive tail to trim at this utilisation"}

    info = {
        "applied": True,
        "target_utilization": round(utilization, 2),
        "kept_lines": len(kept),
        "dropped_lines": len(dropped),
        "spend_before": round(sum(_net(r) for r in on_deck)),
        "spend_after": round(spend),
        "blended_roas_before": round(_blended_roas(on_deck), 2),
        "blended_roas_after": round(_blended_roas(kept), 2),
        "dropped": [
            {"slot": _get(r, "slot_name", "slot_code", default="slot"), "roas": round(_roas(r), 1), "net": round(_net(r))}
            for r in dropped
        ],
        "frontier": roas_frontier(rows, settings),
    }
    return kept + off_deck, info
