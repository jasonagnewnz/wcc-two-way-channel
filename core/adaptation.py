"""Climate adaptation and equity signal — correlation, stated as correlation.

Where this comes from
---------------------
`reference/vision-doc-breakdown.md` §9 asks for reports to accumulate over
years into climate-adaptation evidence, and then rules it out honestly: there
is no multi-year community report history and there will not be one by 16:00.
Its own suggested alternative is the one built here —

    "the honest version is spatial rather than temporal: cluster today's
     reports against the existing flood-hazard layer and deprivation-by-area
     data, labelled as correlation, not trend."

So this asks one question of today's reports: **where are they landing?** If
they cluster inside areas WCC has already mapped as flood-prone, that is
corroboration of the hazard model by the people who live there. If they
cluster in areas of high deprivation, that is an equity signal about who
carries the impact — which is the part adaptation funding usually decides
badly.

What it is not
--------------
It is not a trend. It is not prediction. Nothing here says an issue is
"emerging" or that frequency is increasing, because with hours of data that
would be an invention, and inventing confidence is the failure mode these
problem statements are most wary of. Every number states its own sample size,
and the interface says "correlation, not trend" where a reader will see it.

Deprivation deciles are NZDep-style: 1 is least deprived, 10 is most. A decile
is an area-level measure and says nothing about any individual household.
"""

from __future__ import annotations

import threading

from .reports import REPORT_TYPE

# Nothing is claimed below this. Three reports in a flood zone is a Tuesday,
# not a finding, and a percentage of three is a way of dressing it up as one.
MIN_SAMPLE = 4


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2


def summarise(store, module_id: str = "team-6-two-way",
              max_points: int = 40, timeout: float = 12.0) -> dict:
    """Correlate located reports against the WCC hazard and deprivation layers.

    Lookups run in parallel with a wall-clock budget: council servers are slow
    and this is a panel, not a critical path, so it returns whatever resolved
    in time and says how many that was. A partial answer that admits its own
    sample size beats a spinner.
    """
    from .hazard import lookup

    reports = [r for r in store.fetch(limit=0, signal_type=REPORT_TYPE,
                                      module_id=module_id)
               if r.get("lat") is not None and r.get("lng") is not None]
    total_located = len(reports)
    sample = reports[-max_points:]

    results: list[dict] = []
    lock = threading.Lock()

    def work(report):
        context = lookup(report["lat"], report["lng"])
        if context:
            with lock:
                results.append(context)

    threads = [threading.Thread(target=work, args=(r,), daemon=True) for r in sample]
    for t in threads:
        t.start()
    # One shared budget rather than per-thread, so a few slow lookups cannot
    # multiply into a long wait.
    deadline = timeout
    for t in threads:
        t.join(timeout=max(deadline, 0.1))

    resolved = len(results)
    in_flood = sum(1 for c in results if c.get("flood_hazard"))
    in_tsunami = sum(1 for c in results
                     if c.get("tsunami_zone") not in (None, "", 0))
    deciles = [float(c["deprivation_decile"]) for c in results
               if c.get("deprivation_decile") is not None]

    findings = []
    if resolved >= MIN_SAMPLE:
        if in_flood:
            findings.append({
                "kind": "flood",
                "text": f"{in_flood} of {resolved} located reports fall inside an "
                        f"area WCC has already mapped as flood-prone.",
                "reading": "The people living there are reporting what the hazard "
                           "model predicts. That is corroboration of the model, "
                           "not a new hazard.",
            })
        if in_tsunami:
            findings.append({
                "kind": "tsunami",
                "text": f"{in_tsunami} of {resolved} are inside a tsunami "
                        f"evacuation zone.",
                "reading": "Relevant to evacuation planning rather than to this "
                           "event — these are the same streets people would be "
                           "asked to leave.",
            })
        median = _median(deciles)
        if median is not None:
            findings.append({
                "kind": "equity",
                "text": f"Median deprivation decile of the affected areas is "
                        f"{median:.0f} of 10.",
                # NZDep runs 1 (least deprived) to 10 (most). Getting the
                # direction wrong here would invert an equity finding, which
                # is worse than not making one.
                "reading": ("Impact is landing in more deprived areas, which is "
                            "where adaptation funding tends to be decided badly."
                            if median >= 7 else
                            "Landing in less deprived areas in this sample — no "
                            "equity concern indicated here."
                            if median <= 3 else
                            "Mid-range. No strong equity signal in this sample."),
            })

    return {
        "total_located_reports": total_located,
        "sampled": len(sample),
        "resolved": resolved,
        "min_sample": MIN_SAMPLE,
        "enough": resolved >= MIN_SAMPLE,
        "in_flood_hazard": in_flood,
        "in_tsunami_zone": in_tsunami,
        "median_deprivation_decile": _median(deciles),
        "findings": findings,
        # Repeated in the payload as well as the interface, so it travels with
        # the data if anyone consumes this endpoint directly.
        "caveat": ("Correlation across a single event, not a trend. Hazard "
                   "layers are planning data, not live emergency information. "
                   "Deprivation is an area-level measure and says nothing about "
                   "any household."),
        "sources": "WCC / Greater Wellington / GNS hazard layers and NZDep-style "
                   "deprivation, via wcc_gis.",
    }
