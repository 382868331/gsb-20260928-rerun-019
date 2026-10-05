from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class MaterialError(ValueError):
    pass


def _quantity(value: float) -> float:
    value = float(value)
    if value != value or value in (float("inf"), float("-inf")) or value <= 0.0:
        raise MaterialError("material quantity must be a finite positive number")
    return value


def _stage(stage: int) -> int:
    stage = int(stage)
    if stage < 1:
        raise MaterialError("material stage must be a positive integer")
    return stage


@dataclass(frozen=True)
class MaterialBatch:
    id: int
    source: str
    origin: str
    reserved: float = 0.0
    delivered: float = 0.0
    idle: float = 0.0
    placed: float = 0.0
    lost: float = 0.0
    canceled: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "origin": self.origin,
            "reserved": self.reserved,
            "delivered": self.delivered,
            "idle": self.idle,
            "placed": self.placed,
            "lost": self.lost,
            "canceled": self.canceled,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "MaterialBatch":
        batch = MaterialBatch(
            id=int(data["id"]),
            source=str(data["source"]),
            origin=str(data["origin"]),
            reserved=float(data.get("reserved", 0.0)),
            delivered=float(data.get("delivered", 0.0)),
            idle=float(data.get("idle", 0.0)),
            placed=float(data.get("placed", 0.0)),
            lost=float(data.get("lost", 0.0)),
            canceled=float(data.get("canceled", 0.0)),
        )
        batch.validate()
        return batch

    def validate(self) -> None:
        if self.id < 1:
            raise MaterialError("material batch id must be positive")
        if self.source not in ("external", "stockpile", "earthwork"):
            raise MaterialError("material batch source must be external, stockpile, or earthwork")
        fields = (self.reserved, self.delivered, self.idle, self.placed, self.lost, self.canceled)
        if any(value < 0.0 for value in fields):
            raise MaterialError("material quantities cannot be negative")
        if self.source == "external":
            if abs(self.delivered - self.idle - self.placed - self.lost) > 1e-9:
                raise MaterialError("delivered external material must be idle, placed, or lost")
        else:
            if abs(self.delivered - self.idle - self.placed - self.lost) > 1e-9:
                raise MaterialError("on-site material must be idle, placed, or lost")
        if self.idle > self.delivered + (self.reserved if self.source == "earthwork" else 0.0):
            raise MaterialError("idle material cannot exceed material already available on site")


@dataclass(frozen=True)
class MaterialTransaction:
    stage: int
    batch_id: int
    action: str
    quantity: float
    earthwork_event: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "batch_id": self.batch_id,
            "action": self.action,
            "quantity": self.quantity,
            "earthwork_event": self.earthwork_event,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "MaterialTransaction":
        return MaterialTransaction(
            stage=int(data["stage"]),
            batch_id=int(data["batch_id"]),
            action=str(data["action"]),
            quantity=float(data["quantity"]),
            earthwork_event=data.get("earthwork_event"),
        )


@dataclass(frozen=True)
class EarthworkEvent:
    id: str
    stage: int
    trigger: str
    cut: float
    fill: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "stage": self.stage,
            "trigger": self.trigger,
            "cut": self.cut,
            "fill": self.fill,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "EarthworkEvent":
        return EarthworkEvent(
            id=str(data["id"]),
            stage=int(data["stage"]),
            trigger=str(data["trigger"]),
            cut=float(data["cut"]),
            fill=float(data["fill"]),
        )


class MaterialLedger:
    def __init__(
        self,
        batches: list[MaterialBatch] | None = None,
        transactions: list[MaterialTransaction] | None = None,
        earthwork_events: list[EarthworkEvent] | None = None,
    ):
        self.batches = list(batches or [])
        self.transactions = list(transactions or [])
        self.earthwork_events = list(earthwork_events or [])

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "batches": [batch.to_dict() for batch in self.batches],
            "transactions": [item.to_dict() for item in self.transactions],
            "earthwork_events": [item.to_dict() for item in self.earthwork_events],
        }

    @staticmethod
    def from_dict(data: dict[str, Any] | None) -> "MaterialLedger":
        if not data:
            return MaterialLedger()
        ledger = MaterialLedger(
            batches=[MaterialBatch.from_dict(item) for item in data.get("batches", [])],
            transactions=[MaterialTransaction.from_dict(item) for item in data.get("transactions", [])],
            earthwork_events=[EarthworkEvent.from_dict(item) for item in data.get("earthwork_events", [])],
        )
        ledger.validate()
        return ledger

    def validate(self) -> None:
        ids = [batch.id for batch in self.batches]
        if len(ids) != len(set(ids)) or any(batch_id < 1 for batch_id in ids):
            raise MaterialError("material batch ids must be unique positive integers")
        for batch in self.batches:
            batch.validate()
        event_ids = {event.id for event in self.earthwork_events}
        if len(event_ids) != len(self.earthwork_events):
            raise MaterialError("earthwork event ids must be unique")
        batch_ids = set(ids)
        for item in self.transactions:
            if item.stage < 0 or item.quantity < 0.0:
                raise MaterialError("invalid material transaction")
            if item.batch_id not in batch_ids:
                raise MaterialError(f"material transaction references missing batch: {item.batch_id}")
            if item.earthwork_event is not None and item.earthwork_event not in event_ids:
                raise MaterialError("material placement references a missing earthwork event")

    def _next_batch_id(self) -> int:
        return max((batch.id for batch in self.batches), default=0) + 1

    def _batch(self, batch_id: int) -> MaterialBatch:
        batch = next((item for item in self.batches if item.id == batch_id), None)
        if batch is None:
            raise MaterialError(f"material batch not found: {batch_id}")
        return batch

    def _replace(self, batch: MaterialBatch, **changes) -> MaterialBatch:
        updated = MaterialBatch(**{**batch.to_dict(), **changes})
        index = next(index for index, item in enumerate(self.batches) if item.id == batch.id)
        self.batches[index] = updated
        updated.validate()
        return updated

    def reserve_external(self, quantity: float, stage: int) -> MaterialBatch:
        quantity = _quantity(quantity)
        stage = _stage(stage)
        batch = MaterialBatch(
            id=self._next_batch_id(),
            source="external",
            origin=f"external-reserved-stage-{stage}",
            reserved=quantity,
        )
        batch.validate()
        self.batches.append(batch)
        self.transactions.append(MaterialTransaction(stage, batch.id, "reserve", quantity))
        return batch

    def register_stockpile(self, quantity: float, stage: int, origin: str = "existing-stockpile") -> MaterialBatch:
        quantity = _quantity(quantity)
        stage = _stage(stage)
        batch = MaterialBatch(
            id=self._next_batch_id(),
            source="stockpile",
            origin=origin,
            delivered=quantity,
            idle=quantity,
        )
        batch.validate()
        self.batches.append(batch)
        self.transactions.append(MaterialTransaction(stage, batch.id, "register", quantity))
        return batch

    def record_earthwork(self, stage: int, trigger: str, cut: float, fill: float) -> EarthworkEvent:
        stage = _stage(stage)
        cut = max(0.0, float(cut))
        fill = max(0.0, float(fill))
        cut = 0.0 if cut < 1e-12 else cut
        fill = 0.0 if fill < 1e-12 else fill
        event = EarthworkEvent(f"E{len(self.earthwork_events) + 1}", stage, trigger, cut, fill)
        self.earthwork_events.append(event)
        if cut > 0.0:
            batch = MaterialBatch(
                id=self._next_batch_id(),
                source="earthwork",
                origin=event.id,
                delivered=cut,
                idle=cut,
            )
            batch.validate()
            self.batches.append(batch)
            self.transactions.append(MaterialTransaction(stage, batch.id, "recover", cut, event.id))
        self.validate()
        return event

    def deliver(self, batch_id: int, quantity: float, stage: int) -> MaterialBatch:
        quantity = _quantity(quantity)
        stage = _stage(stage)
        batch = self._batch(batch_id)
        if batch.source != "external" or batch.reserved < quantity:
            raise MaterialError("delivery quantity cannot exceed the external reservation that has not occurred")
        updated = self._replace(
            batch,
            reserved=batch.reserved - quantity,
            delivered=batch.delivered + quantity,
            idle=batch.idle + quantity,
        )
        self.transactions.append(MaterialTransaction(stage, batch_id, "deliver", quantity))
        return updated

    def cancel_reservation(self, batch_id: int, quantity: float, stage: int) -> MaterialBatch:
        quantity = _quantity(quantity)
        stage = _stage(stage)
        batch = self._batch(batch_id)
        if batch.source != "external" or batch.reserved < quantity:
            raise MaterialError("cancellation can release only an external reservation that has not occurred")
        updated = self._replace(
            batch,
            reserved=batch.reserved - quantity,
            canceled=batch.canceled + quantity,
        )
        self.transactions.append(MaterialTransaction(stage, batch_id, "cancel", quantity))
        return updated

    def record_loss(self, batch_id: int, quantity: float, stage: int) -> MaterialBatch:
        quantity = _quantity(quantity)
        stage = _stage(stage)
        batch = self._batch(batch_id)
        if batch.idle < quantity:
            raise MaterialError("loss cannot reuse or exceed idle on-site material")
        updated = self._replace(batch, idle=batch.idle - quantity, lost=batch.lost + quantity)
        self.transactions.append(MaterialTransaction(stage, batch_id, "lose", quantity))
        return updated

    def place(self, batch_id: int, quantity: float, stage: int, event_id: str) -> MaterialBatch:
        quantity = _quantity(quantity)
        stage = _stage(stage)
        batch = self._batch(batch_id)
        event = next((item for item in self.earthwork_events if item.id == event_id), None)
        if event is None:
            raise MaterialError(f"earthwork event not found: {event_id}")
        if event.stage != stage:
            raise MaterialError("material placement stage must match its fill event")
        already = sum(
            item.quantity
            for item in self.transactions
            if item.action == "place" and item.earthwork_event == event_id
        )
        if already + quantity > event.fill + 1e-9:
            raise MaterialError("placement cannot exceed the actual fill volume for that stage")
        if batch.idle < quantity:
            raise MaterialError("placement cannot reuse material; idle batch quantity is insufficient")
        updated = self._replace(batch, idle=batch.idle - quantity, placed=batch.placed + quantity)
        self.transactions.append(MaterialTransaction(stage, batch_id, "place", quantity, event_id))
        return updated

    def event(self, event_id: str) -> EarthworkEvent:
        event = next((item for item in self.earthwork_events if item.id == event_id), None)
        if event is None:
            raise MaterialError(f"earthwork event not found: {event_id}")
        return event

    def fill_sources(self, event_id: str) -> dict[str, Any]:
        event = self.event(event_id)
        placements = [
            item.to_dict()
            for item in self.transactions
            if item.action == "place" and item.earthwork_event == event_id
        ]
        placed = sum(item["quantity"] for item in placements)
        return {
            "earthwork_event": event.to_dict(),
            "placed": placed,
            "unfilled_trace_gap": max(0.0, event.fill - placed),
            "sources": placements,
        }

    def stage_plan_comparison(self) -> list[dict[str, Any]]:
        rows = []
        events_seen = set()
        event_by_id = {event.id: event for event in self.earthwork_events}
        stages = sorted({event.stage for event in self.earthwork_events})
        for stage in stages:
            stage_events = [event for event in self.earthwork_events if event.stage == stage]
            stockpile_available = sum(
                transaction.quantity
                for transaction in self.transactions
                if transaction.action == "register" and transaction.stage <= stage
            )
            prior_recycled = 0.0
            for event in stage_events:
                events_seen.add(event.id)
            for transaction in self.transactions:
                linked = event_by_id.get(transaction.earthwork_event or "")
                if transaction.action == "recover" and linked is not None and linked.stage < stage:
                    prior_recycled += transaction.quantity
            cut = sum(event.cut for event in stage_events)
            fill = sum(event.fill for event in stage_events)
            existing_before = stockpile_available + prior_recycled
            rows.append({
                "stage": stage,
                "events": [event.id for event in stage_events],
                "cut": cut,
                "fill": fill,
                "existing_stockpile_before": existing_before,
                "recycled_cut": cut,
                "stockpile_plan_gap": max(0.0, fill - existing_before),
                "external_supplement_gap": max(0.0, fill - cut),
                "balanced_site_gap": max(0.0, fill - existing_before - cut),
            })
        return rows
