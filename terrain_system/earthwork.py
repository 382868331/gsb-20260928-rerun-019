from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from .edits import EditError


EARTH_VERSION = 1


class EarthworkError(EditError):
    pass


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EarthworkError(f"{name} must be a number")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise EarthworkError(f"{name} must be finite")
    return number


def _quantity(value: Any, name: str = "quantity") -> float:
    number = _number(value, name)
    if number < 0.0:
        raise EarthworkError(f"{name} must be non-negative")
    return number


@dataclass(frozen=True)
class EarthworkEvent:
    """One auditable earthwork decision; stage > 0 means already executed.

    Stages are the single ordering axis. Events with stage <= current_stage
    describe terrain / material facts that have happened; future events are
    plans (reservations, scheduled moves) that only reserve capacity and can
    still be released. Material never flows backwards: placed fill and
    recovered demolition material are separate batches, never re-opened
    inventory of an existing batch.
    """

    seq: int
    kind: str
    stage: int
    quantity: float
    batch: str | None = None
    reservation: str | None = None
    source: str | None = None
    target: str | None = None
    work_id: int | None = None
    start: float | None = None
    end: float | None = None
    note: str = ""
    dedup_key: str | None = None

    _KINDS = {
        "cut",       # excavation creates a batch of soil (from terrain change)
        "supply",    # explicit external make-up material
        "loss",      # explicit disposal / loss from a batch
        "reserve",   # reserve soil of a batch for a future placement
        "arrive",    # reserved material actually arrives at a fill
        "place",     # arrived / free material becomes permanent fill
        "recover",   # demolition or expired ramp creates a new batch
        "cancel",    # cancel a reservation (release reason recorded)
        "demand",    # measured fill requirement produced by terrain change
    }

    def validate(self) -> None:
        if not isinstance(self.seq, int) or isinstance(self.seq, bool) or self.seq < 1:
            raise EarthworkError("event sequence must be a positive integer")
        if self.kind not in self._KINDS:
            raise EarthworkError(f"unknown earthwork event kind: {self.kind}")
        if not isinstance(self.stage, int) or isinstance(self.stage, bool) or self.stage < 1:
            raise EarthworkError("earthwork stage must be a positive integer")
        _quantity(self.quantity, "quantity")
        if self.work_id is not None and (
            not isinstance(self.work_id, int) or isinstance(self.work_id, bool) or self.work_id < 1
        ):
            raise EarthworkError("work reference must be a positive integer")
        for bound, name in ((self.start, "start"), (self.end, "end")):
            if bound is not None:
                bound = _number(bound, name)
                if not 0.0 <= bound <= 1.0:
                    raise EarthworkError(f"{name} must lie within 0..1")
        if self.start is not None and self.end is not None and self.start > self.end:
            raise EarthworkError("start must not exceed end")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "seq": self.seq,
            "kind": self.kind,
            "stage": self.stage,
            "quantity": self.quantity,
            "batch": self.batch,
            "reservation": self.reservation,
            "source": self.source,
            "target": self.target,
            "work_id": self.work_id,
            "start": self.start,
            "end": self.end,
            "note": self.note,
            "dedup_key": self.dedup_key,
        }
        return {key: value for key, value in payload.items() if value is not None}

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "EarthworkEvent":
        try:
            event = EarthworkEvent(
                seq=int(data["seq"]),
                kind=str(data["kind"]),
                stage=int(data["stage"]),
                quantity=float(data["quantity"]),
                batch=data.get("batch"),
                reservation=data.get("reservation"),
                source=data.get("source"),
                target=data.get("target"),
                work_id=data.get("work_id"),
                start=data.get("start"),
                end=data.get("end"),
                note=data.get("note", ""),
                dedup_key=data.get("dedup_key"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise EarthworkError(f"invalid earthwork event: {exc}") from exc
        event.validate()
        return event


@dataclass(frozen=True)
class Reservation:
    id: str
    batch: str
    target: str
    quantity: float
    stage: int                 # stage at which the move was planned
    arrive_stage: int | None   # stage when material arrived (None = pending)
    cancelled: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "batch": self.batch,
            "target": self.target,
            "quantity": self.quantity,
            "stage": self.stage,
            "arrive_stage": self.arrive_stage,
            "cancelled": self.cancelled,
            "reason": self.reason,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Reservation":
        return Reservation(
            id=str(data["id"]),
            batch=str(data["batch"]),
            target=str(data["target"]),
            quantity=_quantity(data.get("quantity"), "quantity"),
            stage=int(data["stage"]),
            arrive_stage=None if data.get("arrive_stage") is None else int(data["arrive_stage"]),
            cancelled=bool(data.get("cancelled", False)),
            reason=str(data.get("reason", "")),
        )


def stable_dedup_key(parts: list[Any]) -> str:
    text = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class FillRequirement:
    id: str
    target: str
    work_id: int
    start: float
    end: float
    stage: int
    required: float
    blocked: bool = False
    note: str = ""

    def validate(self) -> None:
        if not isinstance(self.work_id, int) or isinstance(self.work_id, bool) or self.work_id < 1:
            raise EarthworkError("fill requirement must reference a positive work id")
        if not isinstance(self.stage, int) or isinstance(self.stage, bool) or self.stage < 1:
            raise EarthworkError("fill requirement stage must be positive")
        if not 0.0 <= self.start <= self.end <= 1.0:
            raise EarthworkError("fill requirement span must satisfy 0 <= start <= end <= 1")
        if self.required < 0.0:
            raise EarthworkError("fill requirement quantity must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "target": self.target,
            "work_id": self.work_id,
            "start": self.start,
            "end": self.end,
            "stage": self.stage,
            "required": self.required,
            "blocked": self.blocked,
            "note": self.note,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "FillRequirement":
        try:
            requirement = FillRequirement(
                id=str(data["id"]),
                target=str(data["target"]),
                work_id=int(data["work_id"]),
                start=float(data["start"]),
                end=float(data["end"]),
                stage=int(data["stage"]),
                required=float(data["required"]),
                blocked=bool(data.get("blocked", False)),
                note=str(data.get("note", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise EarthworkError(f"invalid fill requirement: {exc}") from exc
        requirement.validate()
        return requirement


class EarthworkLedger:
    """Append-only earthwork record with batch identities and reservations.

    The ledger state at any stage is computed purely by replaying events with
    stage <= requested stage in (stage, seq) order, so chunk generation order
    and the order in which records were loaded never change balances.
    """

    def __init__(
        self,
        events: list[EarthworkEvent] | None = None,
        reservations: list[Reservation] | None = None,
        requirements: list["FillRequirement"] | None = None,
        counters: dict[str, int] | None = None,
        current_stage: int = 0,
    ):
        self.events = list(events or [])
        self.reservations = {item.id: item for item in (reservations or [])}
        self.requirements = {item.id: item for item in (requirements or [])}
        self.counters = dict(counters or {})
        self.current_stage = current_stage
        self._reindex()

    @property
    def enabled(self) -> bool:
        return bool(self.events) or bool(self.reservations) or self.current_stage > 0

    def _reindex(self) -> None:
        seqs = [event.seq for event in self.events]
        if len(seqs) != len(set(seqs)):
            raise EarthworkError("earthwork event sequences must be unique")
        keys = [
            event.dedup_key
            for event in self.events
            if event.dedup_key is not None
        ]
        if len(keys) != len(set(keys)):
            raise EarthworkError("duplicate earthwork records detected")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": EARTH_VERSION,
            "current_stage": self.current_stage,
            "counters": dict(sorted(self.counters.items())),
            "events": [event.to_dict() for event in sorted(self.events, key=lambda item: item.seq)],
            "reservations": [
                self.reservations[key].to_dict()
                for key in sorted(self.reservations)
            ],
            "requirements": [
                self.requirements[key].to_dict()
                for key in sorted(self.requirements)
            ],
        }

    @staticmethod
    def from_dict(data: dict[str, Any] | None) -> "EarthworkLedger":
        if not data:
            return EarthworkLedger()
        if data.get("version") != EARTH_VERSION:
            raise EarthworkError("unsupported earthwork ledger version")
        events = [EarthworkEvent.from_dict(item) for item in data.get("events", [])]
        reservations = [
            Reservation.from_dict(item) for item in data.get("reservations", [])
        ]
        requirements = [
            FillRequirement.from_dict(item) for item in data.get("requirements", [])
        ]
        counters = {str(key): int(value) for key, value in data.get("counters", {}).items()}
        stage = int(data.get("current_stage", 0))
        ledger = EarthworkLedger(events, reservations, requirements, counters, stage)
        ledger.validate()
        return ledger

    def validate(self) -> None:
        if not isinstance(self.current_stage, int) or self.current_stage < 0:
            raise EarthworkError("current_stage must be a non-negative integer")
        for event in self.events:
            event.validate()
        for reservation in self.reservations.values():
            if reservation.quantity <= 0.0:
                raise EarthworkError("reservations must reserve a positive quantity")
        for requirement in self.requirements.values():
            requirement.validate()

    def add_demand(
        self,
        target: str,
        required: float,
        stage: int,
        work_id: int,
        start: float,
        end: float,
        requirement_id: str | None = None,
        blocked: bool = False,
        note: str = "",
        dedup_key: str | None = None,
    ) -> tuple[EarthworkEvent, "FillRequirement"]:
        required = _quantity(required, "required")
        requirement_id = requirement_id or self._new_id("F")
        if requirement_id in self.requirements:
            raise EarthworkError(f"fill requirement already exists: {requirement_id}")
        requirement = FillRequirement(
            id=requirement_id,
            target=target,
            work_id=work_id,
            start=start,
            end=end,
            stage=stage,
            required=required,
            blocked=blocked,
            note=note,
        )
        self.requirements[requirement_id] = requirement
        event = self._record(
            "demand", stage, required, target=target, reservation=requirement_id,
            work_id=work_id, start=start, end=end, note=note, dedup_key=dedup_key,
        )
        return event, requirement

    def requirement_gap(self, requirement_id: str, stage: int | None = None) -> float:
        requirement = self.requirements[requirement_id]
        end = self.current_stage if stage is None else stage
        placed = sum(
            event.quantity for event in self._events_through(end)
            if event.kind == "place" and event.target == requirement.target
        )
        return max(requirement.required - placed, 0.0)

    def requirements_view(self, stage: int | None = None) -> list[dict[str, Any]]:
        end = self.current_stage if stage is None else stage
        view = []
        for requirement in sorted(self.requirements.values(), key=lambda item: item.id):
            if requirement.stage > end:
                continue
            placed = sum(
                event.quantity for event in self._events_through(end)
                if event.kind == "place" and event.target == requirement.target
            )
            view.append({
                **requirement.to_dict(),
                "placed": placed,
                "gap": max(requirement.required - placed, 0.0),
                "sources": [event.to_dict() for event in self.target_placements(requirement.target, end)],
            })
        return view

    def _next_seq(self) -> int:
        return max((event.seq for event in self.events), default=0) + 1

    def _new_id(self, prefix: str) -> str:
        value = self.counters.get(prefix, 0) + 1
        self.counters[prefix] = value
        return f"{prefix}{value}"

    def _append(self, event: EarthworkEvent) -> EarthworkEvent:
        if event.dedup_key is not None:
            duplicate = next(
                (
                    existing
                    for existing in self.events
                    if existing.dedup_key == event.dedup_key
                ),
                None,
            )
            if duplicate is not None:
                raise EarthworkError(
                    f"duplicate earthwork record: {event.dedup_key} (event {duplicate.seq})"
                )
        self.events.append(event)
        self.current_stage = max(self.current_stage, event.stage)
        return event

    def _record(
        self,
        kind: str,
        stage: int,
        quantity: float,
        **fields: Any,
    ) -> EarthworkEvent:
        event = EarthworkEvent(
            seq=self._next_seq(),
            kind=kind,
            stage=stage,
            quantity=_quantity(quantity),
            **fields,
        )
        event.validate()
        return self._append(event)

    # -- batch creation -------------------------------------------------

    def record_cut(
        self,
        quantity: float,
        stage: int,
        work_id: int | None = None,
        start: float | None = None,
        end: float | None = None,
        batch: str | None = None,
        note: str = "",
        dedup_key: str | None = None,
    ) -> tuple[EarthworkEvent, str]:
        batch_id = batch or self._new_id("C")
        event = self._record(
            "cut", stage, quantity, batch=batch_id,
            work_id=work_id, start=start, end=end, note=note, dedup_key=dedup_key,
        )
        return event, batch_id

    def record_supply(
        self,
        quantity: float,
        stage: int,
        target: str | None = None,
        note: str = "",
        dedup_key: str | None = None,
    ) -> tuple[EarthworkEvent, str]:
        batch_id = self._new_id("S")
        event = self._record(
            "supply", stage, quantity, batch=batch_id,
            target=target, note=note, dedup_key=dedup_key,
        )
        return event, batch_id

    def record_recovery(
        self,
        quantity: float,
        stage: int,
        origin: str,
        work_id: int | None = None,
        start: float | None = None,
        end: float | None = None,
        note: str = "",
        dedup_key: str | None = None,
    ) -> tuple[EarthworkEvent, str]:
        batch_id = self._new_id("R")
        event = self._record(
            "recover", stage, quantity, batch=batch_id, source=origin,
            work_id=work_id, start=start, end=end, note=note, dedup_key=dedup_key,
        )
        return event, batch_id

    def record_loss(
        self,
        batch: str,
        quantity: float,
        stage: int,
        note: str = "",
        dedup_key: str | None = None,
    ) -> EarthworkEvent:
        if quantity <= 0.0:
            raise EarthworkError("loss quantity must be positive")
        return self._record(
            "loss", stage, quantity, batch=batch, note=note, dedup_key=dedup_key,
        )

    # -- reservations ---------------------------------------------------

    def reserve(
        self,
        batch: str,
        target: str,
        quantity: float,
        stage: int,
        reservation_id: str | None = None,
        work_id: int | None = None,
        start: float | None = None,
        end: float | None = None,
        note: str = "",
        dedup_key: str | None = None,
    ) -> tuple[EarthworkEvent, Reservation]:
        if quantity <= 0.0:
            raise EarthworkError("reserved quantity must be positive")
        free = self.free_quantity(batch, stage)
        if free + 1e-9 < quantity:
            raise EarthworkError(
                f"only {free:.6f} free soil available in batch {batch} at stage {stage}"
            )
        reservation_id = reservation_id or self._new_id("P")
        if reservation_id in self.reservations:
            raise EarthworkError(f"reservation already exists: {reservation_id}")
        reservation = Reservation(
            id=reservation_id,
            batch=batch,
            target=target,
            quantity=quantity,
            stage=stage,
            arrive_stage=None,
        )
        self.reservations[reservation_id] = reservation
        event = self._record(
            "reserve", stage, quantity, batch=batch, target=target,
            reservation=reservation_id, work_id=work_id, start=start, end=end,
            note=note, dedup_key=dedup_key,
        )
        return event, reservation

    def mark_arrived(
        self,
        reservation_id: str,
        stage: int,
        quantity: float | None = None,
        note: str = "",
        dedup_key: str | None = None,
    ) -> EarthworkEvent:
        reservation = self._require_open_reservation(reservation_id)
        if stage < reservation.stage:
            raise EarthworkError("material cannot arrive before it was reserved")
        amount = reservation.quantity if quantity is None else _quantity(quantity)
        if amount > reservation.quantity + 1e-9:
            raise EarthworkError("arrival cannot exceed the reserved quantity")
        arrived = reservation.arrive_stage or 0.0
        free = self.free_quantity(reservation.batch, stage)
        if free + 1e-9 < amount - arrived:
            raise EarthworkError("not enough remaining free soil to fulfil this arrival")
        updated = Reservation(
            **{**reservation.to_dict(), "arrive_stage": max(stage, reservation.arrive_stage or stage)}
        ) if amount + 1e-9 >= reservation.quantity else reservation
        if amount + 1e-9 >= reservation.quantity:
            self.reservations[reservation_id] = updated
        event = self._record(
            "arrive", stage, amount, batch=reservation.batch, target=reservation.target,
            reservation=reservation_id, note=note, dedup_key=dedup_key,
        )
        return event

    def place(
        self,
        reservation_id: str | None,
        target: str,
        quantity: float,
        stage: int,
        batch: str | None = None,
        work_id: int | None = None,
        start: float | None = None,
        end: float | None = None,
        note: str = "",
        dedup_key: str | None = None,
    ) -> EarthworkEvent:
        if quantity <= 0.0:
            raise EarthworkError("placed quantity must be positive")
        source_batch = batch
        if reservation_id is not None:
            reservation = self._require_open_reservation(reservation_id)
            if reservation.target != target:
                raise EarthworkError("reservation target does not match this placement")
            if stage < reservation.stage:
                raise EarthworkError("cannot place reserved soil before the reservation stage")
            if quantity > reservation.quantity + 1e-9:
                raise EarthworkError("placement cannot exceed the reserved quantity")
            if reservation.arrive_stage is None or reservation.arrive_stage > stage:
                raise EarthworkError("reserved soil has not arrived yet")
            source_batch = reservation.batch
        if source_batch is None:
            raise EarthworkError("placement requires a reservation or an explicit batch")
        if reservation_id is None:
            free = self.free_quantity(source_batch, stage)
            if free + 1e-9 < quantity:
                raise EarthworkError(
                    f"only {free:.6f} free soil available in batch {source_batch}"
                )
        event = self._record(
            "place", stage, quantity, batch=source_batch, target=target,
            reservation=reservation_id, work_id=work_id, start=start, end=end,
            note=note, dedup_key=dedup_key,
        )
        if reservation_id is not None and abs(quantity - self.reservations[reservation_id].quantity) <= 1e-9:
            self.reservations[reservation_id] = Reservation(
                **{**self.reservations[reservation_id].to_dict(), "cancelled": True,
                   "reason": "consumed by placement"}
            )
        return event

    def cancel_reservation(
        self,
        reservation_id: str,
        stage: int,
        reason: str,
        quantity: float | None = None,
        note: str = "",
        dedup_key: str | None = None,
    ) -> EarthworkEvent:
        reservation = self._require_open_reservation(reservation_id)
        amount = reservation.quantity if quantity is None else _quantity(quantity)
        if amount > reservation.quantity + 1e-9:
            raise EarthworkError("cancellation cannot exceed the reserved quantity")
        arrived = 0.0
        for event in self.events:
            if (
                event.kind == "arrive"
                and event.reservation == reservation_id
                and event.stage <= stage
            ):
                arrived += event.quantity
        pending = max(reservation.quantity - arrived, 0.0)
        if amount > pending + 1e-9:
            raise EarthworkError(
                "arrived material cannot be released by cancellation; record demolition recovery instead"
            )
        event = self._record(
            "cancel", stage, amount, batch=reservation.batch, target=reservation.target,
            reservation=reservation_id, note=(reason or note), dedup_key=dedup_key,
        )
        if amount + 1e-9 >= pending:
            self.reservations[reservation_id] = Reservation(
                **{**reservation.to_dict(), "cancelled": True, "reason": reason}
            )
        return event

    def _require_open_reservation(self, reservation_id: str) -> Reservation:
        reservation = self.reservations.get(reservation_id)
        if reservation is None:
            raise EarthworkError(f"reservation not found: {reservation_id}")
        if reservation.cancelled:
            raise EarthworkError(f"reservation is already closed: {reservation_id}")
        return reservation

    # -- replay / balances ---------------------------------------------

    def _events_through(self, stage: int) -> list[EarthworkEvent]:
        return sorted(
            (event for event in self.events if event.stage <= stage),
            key=lambda event: (event.stage, event.seq),
        )

    def batch_inputs(self, stage: int | None = None) -> dict[str, float]:
        end = self.current_stage if stage is None else stage
        totals: dict[str, float] = {}
        for event in self._events_through(end):
            if event.kind in ("cut", "supply", "recover") and event.batch:
                totals[event.batch] = totals.get(event.batch, 0.0) + event.quantity
        return totals

    def open_reservations(self, stage: int) -> dict[str, Reservation]:
        """Reservations still holding capacity at the end of ``stage``.

        Reservations and arrivals are distinct. An arrival consumes the batch
        and is tracked via events until the matching placement; it is no
        longer free stockpile material even though the fill is not built yet.
        """
        arrivals: dict[str, float] = {}
        placements: dict[str, float] = {}
        cancellations: dict[str, float] = {}
        for event in self._events_through(stage):
            if event.reservation is None:
                continue
            if event.kind == "arrive":
                arrivals[event.reservation] = arrivals.get(event.reservation, 0.0) + event.quantity
            elif event.kind == "place":
                placements[event.reservation] = placements.get(event.reservation, 0.0) + event.quantity
            elif event.kind == "cancel":
                cancellations[event.reservation] = cancellations.get(event.reservation, 0.0) + event.quantity
        open_items: dict[str, Reservation] = {}
        for reservation_id, reservation in self.reservations.items():
            reserved = reservation.quantity
            arrived = arrivals.get(reservation_id, 0.0)
            placed = placements.get(reservation_id, 0.0)
            cancelled = cancellations.get(reservation_id, 0.0)
            pending = reserved - arrived - cancelled
            in_transit = arrived - placed
            if reservation.stage <= stage and (pending > 1e-9 or in_transit > 1e-9):
                open_items[reservation_id] = reservation
        return open_items

    def free_quantity(self, batch: str, stage: int | None = None) -> float:
        end = self.current_stage if stage is None else stage
        produced = 0.0
        reserved = 0.0
        in_transit = 0.0
        placed = 0.0
        lost = 0.0
        for event in self._events_through(end):
            if event.batch != batch:
                continue
            if event.kind in ("cut", "supply", "recover"):
                produced += event.quantity
            elif event.kind == "reserve":
                reserved += event.quantity
            elif event.kind == "cancel":
                reserved -= event.quantity
            elif event.kind == "arrive":
                reserved -= event.quantity
                in_transit += event.quantity
            elif event.kind == "place":
                if event.reservation is not None:
                    in_transit -= event.quantity
                placed += event.quantity
            elif event.kind == "loss":
                lost += event.quantity
        return produced - reserved - in_transit - placed - lost

    def balances(self, stage: int | None = None) -> dict[str, Any]:
        end = self.current_stage if stage is None else stage
        batches = self.batch_inputs(end)
        result = {}
        totals = {"produced": 0.0, "reserved": 0.0, "arrived": 0.0,
                  "placed": 0.0, "lost": 0.0, "free": 0.0}
        arrivals = self._arrivals_through(end)
        reservation_placements = self._placements_through(end)
        direct_placements = self._direct_placements_through(end)
        losses = self._losses_through(end)
        for batch_id in sorted(batches):
            reserved_events = {
                res_id: res for res_id, res in self.reservations.items() if res.batch == batch_id
            }
            arrived_gross = sum(amount for res_id, amount in arrivals.items() if res_id in reserved_events)
            reservation_placed = sum(
                amount for res_id, amount in reservation_placements.items() if res_id in reserved_events
            )
            cancelled = sum(
                event.quantity for event in self._events_through(end)
                if event.kind == "cancel" and event.reservation in reserved_events
            )
            reserved_gross = sum(res.quantity for res in reserved_events.values() if res.stage <= end)
            in_transit = max(arrived_gross - reservation_placed, 0.0)
            reserved = max(reserved_gross - cancelled - arrived_gross, 0.0)
            placed = reservation_placed + direct_placements.get(batch_id, 0.0)
            lost = losses.get(batch_id, 0.0)
            free = batches[batch_id] - reserved - in_transit - placed - lost
            result[batch_id] = {
                "produced": batches[batch_id],
                "reserved_pending": reserved,
                "arrived": in_transit,
                "placed": placed,
                "lost": lost,
                "free": free,
                "origin": self._batch_origin(batch_id, end),
            }
            totals["produced"] += batches[batch_id]
            totals["reserved"] += reserved
            totals["arrived"] += in_transit
            totals["placed"] += placed
            totals["lost"] += lost
            totals["free"] += free
        return {"stage": end, "batches": result, "totals": totals,
                "open_reservations": sorted(self.open_reservations(end))}

    def _arrivals_through(self, stage: int) -> dict[str, float]:
        totals: dict[str, float] = {}
        for event in self._events_through(stage):
            if event.kind == "arrive" and event.reservation:
                totals[event.reservation] = totals.get(event.reservation, 0.0) + event.quantity
        return totals

    def _placements_through(self, stage: int) -> dict[str, float]:
        totals: dict[str, float] = {}
        for event in self._events_through(stage):
            if event.kind == "place" and event.reservation:
                totals[event.reservation] = totals.get(event.reservation, 0.0) + event.quantity
        return totals

    def _direct_placements_through(self, stage: int) -> dict[str, float]:
        totals: dict[str, float] = {}
        for event in self._events_through(stage):
            if event.kind == "place" and event.reservation is None and event.batch:
                totals[event.batch] = totals.get(event.batch, 0.0) + event.quantity
        return totals

    def _pending_reserved_through(self, stage: int) -> dict[str, float]:
        totals: dict[str, float] = {}
        cancelled: dict[str, float] = {}
        arrived: dict[str, float] = {}
        for event in self._events_through(stage):
            if event.reservation is None:
                continue
            if event.kind == "reserve":
                totals[event.reservation] = totals.get(event.reservation, 0.0) + event.quantity
            elif event.kind == "cancel":
                cancelled[event.reservation] = cancelled.get(event.reservation, 0.0) + event.quantity
            elif event.kind == "arrive":
                arrived[event.reservation] = arrived.get(event.reservation, 0.0) + event.quantity
        return {
            res_id: max(amount - cancelled.get(res_id, 0.0) - arrived.get(res_id, 0.0), 0.0)
            for res_id, amount in totals.items()
        }

    def _losses_through(self, stage: int) -> dict[str, float]:
        totals: dict[str, float] = {}
        for event in self._events_through(stage):
            if event.kind == "loss" and event.batch:
                totals[event.batch] = totals.get(event.batch, 0.0) + event.quantity
        return totals

    def _batch_origin(self, batch: str, stage: int) -> dict[str, Any]:
        event = next(
            (item for item in self._events_through(stage)
             if item.batch == batch and item.kind in ("cut", "supply", "recover")),
            None,
        )
        if event is None:
            return {"kind": "unknown"}
        return {
            "kind": event.kind,
            "stage": event.stage,
            "source": event.source,
            "work_id": event.work_id,
            "span": None if event.start is None else [event.start, event.end],
            "note": event.note,
        }

    # -- traceability ---------------------------------------------------

    def target_placements(self, target: str, stage: int | None = None) -> list[EarthworkEvent]:
        end = self.current_stage if stage is None else stage
        return [
            event for event in self._events_through(end)
            if event.kind == "place" and event.target == target
        ]

    def trace_target(self, target: str, stage: int | None = None) -> dict[str, Any]:
        """Follow final fill at ``target`` back to source cut, recovery or supply."""
        chain = []
        total = 0.0
        for placed in self.target_placements(target, stage):
            total += placed.quantity
            origin = self.trace_batch(placed.batch)
            chain.append({
                "place_seq": placed.seq,
                "stage": placed.stage,
                "quantity": placed.quantity,
                "batch": placed.batch,
                "reservation": placed.reservation,
                "origin": origin,
            })
        return {"target": target, "quantity": total, "chain": chain}

    def trace_batch(self, batch: str) -> dict[str, Any]:
        produced = next(
            (event for event in sorted(self.events, key=lambda item: item.seq)
             if event.batch == batch and event.kind in ("cut", "supply", "recover")),
            None,
        )
        if produced is None:
            return {"batch": batch, "kind": "unknown"}
        lineage: list[dict[str, Any]] = []
        current = produced
        while current is not None and current.kind == "recover":
            lineage.append({
                "batch": current.batch,
                "kind": "recover",
                "stage": current.stage,
                "source": current.source,
                "work_id": current.work_id,
                "span": None if current.start is None else [current.start, current.end],
            })
            source_name = current.source
            source_event = None
            if source_name:
                source_event = next(
                    (item for item in self.target_placements(source_name)
                     if item.seq < current.seq),
                    None,
                )
            if source_event is None:
                break
            current = next(
                (event for event in self.events
                 if event.batch == source_event.batch
                 and event.kind in ("cut", "supply", "recover")
                 and event.seq <= source_event.seq),
                None,
            )
        root = None if current is None else {
            "batch": current.batch,
            "kind": current.kind,
            "stage": current.stage,
            "work_id": current.work_id,
            "span": None if current.start is None else [current.start, current.end],
            "note": current.note,
        }
        return {"batch": batch, "root": root, "recovered_via": lineage}

    def events_for_view(self, stage: int) -> list[dict[str, Any]]:
        return [event.to_dict() for event in self._events_through(stage)]

    @staticmethod
    def _spans_overlap(first: tuple[float, float], second: tuple[float, float]) -> bool:
        return first[0] < second[1] - 1e-12 and second[0] < first[1] - 1e-12

    def release_reservations_for_work(
        self,
        work_id: int,
        stage: int,
        reason: str,
        start: float | None = None,
        end: float | None = None,
        facts_through: int | None = None,
    ) -> list[EarthworkEvent]:
        """Release un-arrived reservations aimed at work spans being revised.

        Already arrived material is real material at site and is not silently
        re-stocked: only pending reservations are released.
        """
        released = []
        candidates = {
            res_id: res for res_id, res in self.reservations.items()
            if not res.cancelled
        }
        facts_stage = stage if facts_through is None else facts_through
        for reservation_id, reservation in list(candidates.items()):
            reserve_event = next(
                event for event in self.events
                if event.kind == "reserve" and event.reservation == reservation_id
            )
            if reserve_event.work_id != work_id:
                continue
            if start is not None and reserve_event.start is not None and (
                reserve_event.end is not None
                and not self._spans_overlap(
                    (reserve_event.start, reserve_event.end), (start, end or 1.0)
                )
            ):
                continue
            pending = max(
                reservation.quantity
                - sum(
                    e.quantity for e in self._events_through(stage)
                    if e.kind == "arrive"
                    and e.reservation == reservation_id
                    and e.stage <= facts_stage
                ),
                0.0,
            )
            if pending > 1e-9:
                released.append(
                    self.cancel_reservation(reservation_id, stage, reason, quantity=pending)
                )
        return released
