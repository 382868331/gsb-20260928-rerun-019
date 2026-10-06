from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class LedgerError(RuntimeError):
    pass


BATCH_STATES = ("available", "reserved", "delivered", "placed", "lost")
BATCH_SOURCES = ("excavation", "demolition", "stockpile", "external")


@dataclass
class MaterialBatch:
    id: int
    source: str
    volume: float
    stage: int
    state: str = "available"
    remaining: float | None = None
    lost_volume: float = 0.0

    def __post_init__(self) -> None:
        if self.remaining is None:
            self.remaining = self.volume

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "volume": self.volume,
            "stage": self.stage,
            "state": self.state,
            "remaining": self.remaining,
            "lost_volume": self.lost_volume,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "MaterialBatch":
        batch = MaterialBatch(
            id=int(data["id"]),
            source=data["source"],
            volume=float(data["volume"]),
            stage=int(data["stage"]),
            state=data.get("state", "available"),
            remaining=float(data.get("remaining", data["volume"])),
            lost_volume=float(data.get("lost_volume", 0.0)),
        )
        batch.validate()
        return batch

    def validate(self) -> None:
        if not isinstance(self.id, int) or isinstance(self.id, bool) or self.id < 1:
            raise LedgerError("batch id must be a positive integer")
        if self.source not in BATCH_SOURCES:
            raise LedgerError("unknown batch source: " + str(self.source))
        if self.state not in BATCH_STATES:
            raise LedgerError("unknown batch state: " + str(self.state))
        if not isinstance(self.stage, int) or self.stage < 0:
            raise LedgerError("batch stage must be a non-negative integer")
        for value in (self.volume, self.remaining, self.lost_volume):
            if value != value or value in (float("inf"), float("-inf")) or value < 0.0:
                raise LedgerError("batch volumes must be finite and non-negative")
        if self.remaining + self.lost_volume > self.volume + 1e-9:
            raise LedgerError("batch remaining plus losses cannot exceed its volume")


@dataclass
class Placement:
    id: int
    stage: int
    volume: float
    allocations: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "stage": self.stage,
            "volume": self.volume,
            "allocations": [dict(item) for item in self.allocations],
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Placement":
        placement = Placement(
            id=int(data["id"]),
            stage=int(data["stage"]),
            volume=float(data["volume"]),
            allocations=[
                {"batch_id": int(item["batch_id"]), "volume": float(item["volume"])}
                for item in data.get("allocations", [])
            ],
        )
        placement.validate()
        return placement

    def validate(self) -> None:
        if self.id < 1 or self.stage < 1:
            raise LedgerError("placement ids and stages must be positive integers")
        if self.volume <= 0.0:
            raise LedgerError("placement volume must be positive")
        total = 0.0
        for item in self.allocations:
            if item["batch_id"] < 1 or item["volume"] <= 0.0:
                raise LedgerError("placement allocations must reference batches with positive volume")
            total += item["volume"]
        if abs(total - self.volume) > 1e-6:
            raise LedgerError("placement allocations must sum to the placement volume")


class EarthworkLedger:
    """Persistent material ledger: batches, states, placements and losses."""

    def __init__(
        self,
        batches: list[MaterialBatch] | None = None,
        placements: list[Placement] | None = None,
    ):
        self.batches = list(batches or [])
        self.placements = list(placements or [])

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "batches": [batch.to_dict() for batch in self.batches],
            "placements": [placement.to_dict() for placement in self.placements],
        }

    @staticmethod
    def from_dict(data: dict[str, Any] | None) -> "EarthworkLedger":
        if not data:
            return EarthworkLedger()
        ledger = EarthworkLedger(
            batches=[MaterialBatch.from_dict(item) for item in data.get("batches", [])],
            placements=[Placement.from_dict(item) for item in data.get("placements", [])],
        )
        ledger.validate()
        return ledger

    def validate(self) -> None:
        batch_ids = [batch.id for batch in self.batches]
        if len(batch_ids) != len(set(batch_ids)):
            raise LedgerError("batch ids must be unique")
        placement_ids = [placement.id for placement in self.placements]
        if len(placement_ids) != len(set(placement_ids)):
            raise LedgerError("placement ids must be unique")
        known = set(batch_ids)
        for placement in self.placements:
            missing = [item["batch_id"] for item in placement.allocations if item["batch_id"] not in known]
            if missing:
                raise LedgerError(
                    "placement references missing batch(es): " + ", ".join(str(value) for value in missing)
                )
        for batch in self.batches:
            batch.validate()
        for placement in self.placements:
            placement.validate()

    def _next_batch_id(self) -> int:
        return max((batch.id for batch in self.batches), default=0) + 1

    def _next_placement_id(self) -> int:
        return max((placement.id for placement in self.placements), default=0) + 1

    def batch(self, batch_id: int) -> MaterialBatch:
        found = next((item for item in self.batches if item.id == batch_id), None)
        if found is None:
            raise LedgerError(f"batch not found: {batch_id}")
        return found

    def add_source(self, source: str, volume: float, stage: int) -> MaterialBatch:
        if volume <= 0.0:
            raise LedgerError("source volume must be positive")
        if stage < 0:
            raise LedgerError("source stage must be non-negative")
        batch = MaterialBatch(self._next_batch_id(), source, float(volume), int(stage))
        batch.validate()
        self.batches.append(batch)
        return batch

    def reserve(self, batch_id: int) -> MaterialBatch:
        batch = self.batch(batch_id)
        if batch.state != "available":
            raise LedgerError(f"batch {batch_id} is {batch.state}; only available batches can be reserved")
        batch.state = "reserved"
        return batch

    def deliver(self, batch_id: int) -> MaterialBatch:
        batch = self.batch(batch_id)
        if batch.state != "reserved":
            raise LedgerError(f"batch {batch_id} is {batch.state}; only reserved batches can be delivered")
        batch.state = "delivered"
        return batch

    def cancel(self, batch_id: int) -> MaterialBatch:
        batch = self.batch(batch_id)
        if batch.state != "reserved":
            raise LedgerError(
                f"batch {batch_id} is {batch.state}; cancel only releases reservations that have not happened"
            )
        batch.state = "available"
        return batch

    def record_loss(self, batch_id: int, volume: float) -> MaterialBatch:
        batch = self.batch(batch_id)
        if batch.state in ("placed", "lost"):
            raise LedgerError(f"batch {batch_id} is {batch.state}; losses need remaining material")
        if volume <= 0.0 or volume > batch.remaining + 1e-9:
            raise LedgerError("loss volume must be positive and no more than the batch remainder")
        batch.remaining -= volume
        batch.lost_volume += volume
        if batch.remaining <= 1e-9:
            batch.remaining = 0.0
            batch.state = "lost"
        return batch

    def place(self, stage: int, volume: float, batch_ids: list[int] | None = None) -> Placement:
        if stage < 1:
            raise LedgerError("placement stage must be a positive integer")
        if volume <= 0.0:
            raise LedgerError("placement volume must be positive")
        if batch_ids:
            candidates = [self.batch(batch_id) for batch_id in batch_ids]
        else:
            candidates = sorted(
                (batch for batch in self.batches if batch.state == "delivered" and batch.remaining > 0.0),
                key=lambda batch: batch.id,
            )
        for batch in candidates:
            if batch.state != "delivered":
                raise LedgerError(f"batch {batch.id} is {batch.state}; only delivered batches can be placed")
        supplied = sum(batch.remaining for batch in candidates)
        if supplied + 1e-9 < volume:
            raise LedgerError(
                f"delivered material covers {supplied:.6f} of requested {volume:.6f}; "
                "the shortfall is not balanced automatically"
            )
        allocations = []
        left = volume
        for batch in candidates:
            if left <= 1e-9:
                break
            take = min(batch.remaining, left)
            if take <= 0.0:
                continue
            batch.remaining -= take
            left -= take
            allocations.append({"batch_id": batch.id, "volume": take})
            if batch.remaining <= 1e-9:
                batch.remaining = 0.0
                batch.state = "placed"
        placement = Placement(self._next_placement_id(), int(stage), float(volume), allocations)
        placement.validate()
        self.placements.append(placement)
        return placement

    def trace(self, placement_id: int) -> dict[str, Any]:
        placement = next((item for item in self.placements if item.id == placement_id), None)
        if placement is None:
            raise LedgerError(f"placement not found: {placement_id}")
        sources = []
        for item in placement.allocations:
            batch = self.batch(item["batch_id"])
            sources.append({
                "batch_id": batch.id,
                "source": batch.source,
                "stage": batch.stage,
                "volume": item["volume"],
            })
        return {
            "placement_id": placement.id,
            "stage": placement.stage,
            "volume": placement.volume,
            "sources": sources,
        }

    def state_totals(self) -> dict[str, float]:
        totals = {state: 0.0 for state in BATCH_STATES}
        for batch in self.batches:
            totals[batch.state] += batch.remaining
            totals["lost"] += batch.lost_volume
        return totals

    def placed_by_stage(self) -> dict[int, float]:
        placed: dict[int, float] = {}
        for placement in self.placements:
            placed[placement.stage] = placed.get(placement.stage, 0.0) + placement.volume
        return placed


def scenario_rows(
    ledger: EarthworkLedger,
    balances: list[dict[str, Any]],
    sources: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Per-stage outstanding demand vs remaining supply; gaps are reported, never filled."""
    placed = ledger.placed_by_stage()
    supply_by_stage: dict[int, float] = {}
    for batch in ledger.batches:
        if batch.source in sources and batch.state != "lost":
            supply_by_stage[batch.stage] = supply_by_stage.get(batch.stage, 0.0) + batch.remaining
    rows = []
    cumulative_supply = 0.0
    cumulative_demand = 0.0
    for balance in balances:
        stage = balance["stage"]
        demand = max(0.0, balance["fill"] - placed.get(stage, 0.0))
        cumulative_supply += supply_by_stage.get(stage, 0.0)
        cumulative_demand += demand
        rows.append({
            "stage": stage,
            "outstanding_fill": demand,
            "new_supply": supply_by_stage.get(stage, 0.0),
            "cumulative_supply": cumulative_supply,
            "cumulative_demand": cumulative_demand,
            "gap": max(0.0, cumulative_demand - cumulative_supply),
        })
    return rows
