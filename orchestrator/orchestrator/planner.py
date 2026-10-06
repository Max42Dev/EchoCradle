"""The planner: choose the best model for a request on *this* machine.

The planner joins a :class:`~orchestrator.catalog.Catalog` with a
:class:`~orchestrator.probe.ProbeReport`:

* filter to the requested modality,
* drop anything the host cannot fit (peak footprint vs usable VRAM/RAM),
* drop non-shippable weights when ``shippable_only`` is set,
* prefer the highest quality, then the lowest tier (cheapest that is good).

Disk size is deliberately **not** part of the fit test (design doc: disk is
secondary; evict instead of downgrading).
"""

from __future__ import annotations

from dataclasses import dataclass

from orchestrator.catalog import Catalog, ModelDescriptor, Modality
from orchestrator.errors import ModelNotAvailableError
from orchestrator.probe import ProbeReport


@dataclass(frozen=True)
class Plan:
    """The planner's decision, with the reasoning kept for telemetry."""

    model: ModelDescriptor
    reason: str
    candidates_considered: int


class Planner:
    def __init__(self, catalog: Catalog, report: ProbeReport) -> None:
        self.catalog = catalog
        self.report = report

    def plan(
        self,
        modality: Modality,
        *,
        shippable_only: bool = False,
        model_id: str | None = None,
        min_quality: float = 0.0,
    ) -> Plan:
        """Pick a model for ``modality``.

        Raises :class:`ModelNotAvailableError` when nothing fits, which is the
        signal for the caller to fall back to a placeholder.
        """
        if model_id is not None:
            model = self.catalog.get(model_id)
            if model.modality is not modality:
                raise ModelNotAvailableError(
                    f"{model_id} is {model.modality.value}, not {modality.value}"
                )
            if not self.report.fits(model.vram_gb, model.ram_gb):
                raise ModelNotAvailableError(
                    f"{model_id} needs {model.vram_gb:.1f} GB VRAM / "
                    f"{model.ram_gb:.1f} GB RAM; host has "
                    f"{self.report.usable_vram_gb:.1f} GB usable VRAM / "
                    f"{self.report.ram_gb:.1f} GB RAM"
                )
            return Plan(model, "explicitly requested", 1)

        candidates = self.catalog.by_modality(modality)
        considered = len(candidates)

        if shippable_only:
            candidates = [m for m in candidates if m.shippable]
        if min_quality > 0.0:
            candidates = [m for m in candidates if m.quality >= min_quality]

        fitting = [m for m in candidates if self.report.fits(m.vram_gb, m.ram_gb)]
        if not fitting:
            raise ModelNotAvailableError(
                f"no {modality.value} model fits this host "
                f"(tier {self.report.tier}, "
                f"{self.report.usable_vram_gb:.1f} GB usable VRAM, "
                f"{self.report.ram_gb:.1f} GB RAM); "
                f"{considered} candidate(s) considered"
            )

        # by_modality already sorts by (-quality, tier); take the best.
        best = fitting[0]
        reason = (
            f"best of {len(fitting)} fitting {modality.value} model(s) "
            f"on tier {self.report.tier}"
        )
        return Plan(best, reason, considered)
