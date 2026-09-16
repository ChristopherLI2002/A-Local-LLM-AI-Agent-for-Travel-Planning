"""Expand trip_examples_50 into planner + refine prompts for distillation."""

from __future__ import annotations

import hashlib
import itertools
from dataclasses import asdict, dataclass
from typing import Any

from tests.trip_examples_50 import EXAMPLES, TripExample
from travel_agent.planner_query import TRAVEL_STYLES, build_plan_query

# Night / budget / style / rent_car expansions around each base example
_NIGHT_DELTAS = (0, -2, 2, 3)
_BUDGET_SCALES = (1.0, 0.75, 1.35)
_REFINE_TEMPLATES = (
    "Refine: more {style} focus, keep the same section headings.",
    "Refine: slower pace, fewer attractions per day, keep Day N: blocks.",
    "Refine: prefer cheaper hotel options within budget; keep exact hk.trip.com links.",
    "Refine: add more named restaurants; no vague 'local dinner' lines.",
    "Refine: emphasize {style}; keep Recommended flight/hotel headings.",
)


@dataclass(frozen=True)
class DistillPrompt:
    key: str
    kind: str  # "plan" | "refine"
    base_example_id: int
    destination: str
    origin: str
    nights: int
    styles: tuple[str, ...]
    budget_hkd: float
    rent_car: bool
    depart: str
    return_date: str
    query: str
    parent_key: str = ""  # for refine turns

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["styles"] = list(self.styles)
        return d


def _key(*parts: Any) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _clamp_nights(n: int) -> int:
    return max(1, min(14, int(n)))


def expand_plan_prompts(
    examples: list[TripExample] | None = None,
    *,
    max_prompts: int | None = 400,
) -> list[DistillPrompt]:
    """Expand base examples across nights / budget / rent_car / style tweaks."""
    examples = examples or list(EXAMPLES)
    out: list[DistillPrompt] = []
    seen: set[str] = set()

    for ex in examples:
        variants: list[tuple[int, float, tuple[str, ...], bool]] = []
        # Base
        variants.append((ex.nights, ex.budget_hkd, ex.styles, ex.rent_car))
        for dn in _NIGHT_DELTAS[1:]:
            variants.append((_clamp_nights(ex.nights + dn), ex.budget_hkd, ex.styles, ex.rent_car))
        for scale in _BUDGET_SCALES[1:]:
            variants.append(
                (ex.nights, round(ex.budget_hkd * scale, 0), ex.styles, ex.rent_car)
            )
        # Flip rent_car when base is False (and vice versa once)
        variants.append((ex.nights, ex.budget_hkd, ex.styles, not ex.rent_car))
        # Alternate single style from TRAVEL_STYLES
        for style in TRAVEL_STYLES:
            if style not in ex.styles:
                variants.append((ex.nights, ex.budget_hkd, (style,), ex.rent_car))
                break

        for nights, budget, styles, rent_car in variants:
            depart = ex.depart()
            # Recompute return from nights (TripExample.ret uses self.nights)
            from datetime import date, timedelta

            ret = (date.fromisoformat(depart) + timedelta(days=nights)).isoformat()
            query = build_plan_query(
                destination=ex.destination,
                depart_date=depart,
                return_date=ret,
                budget_hkd=float(budget),
                origin=ex.origin,
                rent_car=rent_car,
                travel_styles=list(styles),
                nights=nights,
            )
            k = _key("plan", ex.id, nights, budget, ",".join(styles), rent_car, depart)
            if k in seen:
                continue
            seen.add(k)
            out.append(
                DistillPrompt(
                    key=k,
                    kind="plan",
                    base_example_id=ex.id,
                    destination=ex.destination,
                    origin=ex.origin,
                    nights=nights,
                    styles=styles,
                    budget_hkd=float(budget),
                    rent_car=rent_car,
                    depart=depart,
                    return_date=ret,
                    query=query,
                )
            )
            if max_prompts is not None and len(out) >= max_prompts:
                return out

    return out


def expand_refine_prompts(
    plan_prompts: list[DistillPrompt] | None = None,
    *,
    per_plan: int = 1,
    max_refine: int = 80,
) -> list[DistillPrompt]:
    """Attach refine-turn prompts (GUI refine path) to a subset of plan prompts."""
    plans = plan_prompts or expand_plan_prompts(max_prompts=120)
    out: list[DistillPrompt] = []
    cycle = itertools.cycle(_REFINE_TEMPLATES)
    for p in plans:
        if len(out) >= max_refine:
            break
        for _ in range(per_plan):
            if len(out) >= max_refine:
                break
            tmpl = next(cycle)
            style = p.styles[0] if p.styles else "First-time"
            refine_body = tmpl.format(style=style)
            query = (
                f"{refine_body}\n"
                "Keep Recommended flight / Recommended hotel / Day-by-day itinerary / "
                "Budget snapshot headings. Only use https://hk.trip.com/... URLs from tools."
            )
            k = _key("refine", p.key, refine_body[:40])
            out.append(
                DistillPrompt(
                    key=k,
                    kind="refine",
                    base_example_id=p.base_example_id,
                    destination=p.destination,
                    origin=p.origin,
                    nights=p.nights,
                    styles=p.styles,
                    budget_hkd=p.budget_hkd,
                    rent_car=p.rent_car,
                    depart=p.depart,
                    return_date=p.return_date,
                    query=query,
                    parent_key=p.key,
                )
            )
    return out


def all_prompts(*, max_plan: int = 400, max_refine: int = 80) -> list[DistillPrompt]:
    plans = expand_plan_prompts(max_prompts=max_plan)
    refines = expand_refine_prompts(plans, max_refine=max_refine)
    return plans + refines


def unique_destinations(examples: list[TripExample] | None = None) -> list[str]:
    examples = examples or list(EXAMPLES)
    seen: set[str] = set()
    out: list[str] = []
    for ex in examples:
        d = ex.destination.strip()
        key = d.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
    return out


def destination_units(examples: list[TripExample] | None = None) -> list[dict[str, Any]]:
    """One scrape unit per unique destination (canonical example + nights)."""
    examples = examples or list(EXAMPLES)
    by_dest: dict[str, TripExample] = {}
    for ex in examples:
        k = ex.destination.strip().lower()
        # Prefer longer / regional examples as the scrape template
        prev = by_dest.get(k)
        if prev is None or (ex.expect_regional and not prev.expect_regional) or ex.nights > prev.nights:
            by_dest[k] = ex
    units: list[dict[str, Any]] = []
    for dest_key, ex in sorted(by_dest.items(), key=lambda x: x[1].id):
        units.append(
            {
                "key": f"dest:{ex.destination.strip().lower()}",
                "destination": ex.destination,
                "example_id": ex.id,
                "origin": ex.origin,
                "nights": ex.nights,
                "styles": list(ex.styles),
                "budget_hkd": ex.budget_hkd,
                "rent_car": ex.rent_car,
                "depart": ex.depart(),
                "return": ex.ret(),
                "expect_regional": ex.expect_regional,
            }
        )
    return units


if __name__ == "__main__":
    plans = expand_plan_prompts()
    refines = expand_refine_prompts(plans)
    dests = unique_destinations()
    print(f"plan_prompts={len(plans)} refine_prompts={len(refines)} destinations={len(dests)}")
    print("sample plan key:", plans[0].key)
    print(plans[0].query[:200], "...")
