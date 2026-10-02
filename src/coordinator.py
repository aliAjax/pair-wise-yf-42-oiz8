"""Recoverable coordination between isolation orders, pairings and transfers.

The coordinator maintains a persistent log of *coordination items*
(``coordination_items`` table). Each item is one side effect of an isolation
order (invalidate an approved pairing, wait for an in-transit transfer, mark
the individual quarantined, ...). Items are processed independently, each in
its own transaction: a failed write only fails that item. Retries and service
restart resume exactly the unfinished items, never redoing finished ones.
"""

from .domain import (
    Actor,
    ConflictError,
    InvalidTransition,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)

SYSTEM_ACTOR = Actor("system", "admin")

# Item lifecycle:
#   waiting  -> prerequisite not yet met (e.g. transfer still in transit)
#   pending  -> ready to be applied
#   failed   -> last attempt failed; a retry resumes from here
#   done     -> finished, never touched again
OPEN_ITEM_STATES = ("pending", "waiting", "failed")

# Order statuses that still coordinate the workflow.
OPEN_ORDER_STATUSES = ("submitted", "active", "blocked", "releasing")

APPLY = "apply"
RELEASE = "release"


class Coordinator:
    def __init__(self, repository, audit):
        self.repository = repository
        self.audit = audit

    # ------------------------------------------------------------------
    # Lookup helpers
    # ------------------------------------------------------------------

    def _pairings_for_animal(self, animal_id, conn=None):
        pairings = self.repository.list_entities(kind="pairing", connection=conn)
        affected = []
        for pairing in pairings:
            data = pairing["data"]
            if data.get("sire_id") == animal_id or data.get("dam_id") == animal_id:
                affected.append(pairing)
        return affected

    def _transfers_for_animal(self, animal_id, conn=None):
        return [
            transfer
            for transfer in self.repository.list_entities(
                kind="transfer", connection=conn
            )
            if transfer["data"].get("animal_id") == animal_id
        ]

    def _open_quarantine_for(self, animal_id, exclude_id=None, conn=None):
        for order in self.repository.list_entities(
            kind="quarantine", connection=conn
        ):
            if order["id"] == exclude_id:
                continue
            if order["status"] in OPEN_ORDER_STATUSES and order["data"].get(
                "animal_id"
            ) == animal_id:
                return order
        return None

    def _affected_objects(self, order, items=None):
        """Objects affected by the order (invalidated pairings, paused
        transfers and the quarantined individual)."""
        if items is None:
            items = self.repository.list_items(quarantine_id=order["id"])
        objects = []
        for item in items:
            if item["kind"] == "invalidate_pairing" and item["target_id"]:
                objects.append({"type": "pairing", "id": item["target_id"]})
            elif item["kind"] == "transfer_in_transit" and item["target_id"]:
                objects.append({"type": "transfer", "id": item["target_id"]})
            elif item["kind"] == "animal_quarantine":
                objects.append(
                    {"type": "animal", "id": order["data"].get("animal_id")}
                )
        seen = set()
        unique = []
        for obj in objects:
            key = (obj["type"], obj["id"])
            if key not in seen:
                seen.add(key)
                unique.append(obj)
        return unique

    def describe_blockages(self, order, items=None):
        """Build the latest blocking list shown to vets and conflict losers."""
        if items is None:
            items = self.repository.list_items(quarantine_id=order["id"])
        blockages = []
        for item in items:
            if item["state"] not in OPEN_ITEM_STATES:
                continue
            entry = {
                "type": item["kind"],
                "target_id": item["target_id"],
                "state": item["state"],
            }
            if item["kind"] == "invalidate_pairing":
                entry["reason"] = "approved pairing pending invalidation"
            elif item["kind"] == "transfer_in_transit":
                entry["reason"] = "individual is in transit; transfer cannot roll back"
            elif item["kind"] == "animal_quarantine":
                entry["reason"] = "individual not yet marked quarantined"
            elif item["kind"] == "reconfirm_pairing":
                entry["reason"] = "pairing awaiting re-confirmation"
            elif item["kind"] == "animal_release":
                entry["reason"] = "individual not yet restored to active"
            if item["last_error"]:
                entry["last_error"] = item["last_error"]
            blockages.append(entry)
        return blockages

    def _conflict_payload(self, order):
        items = self.repository.list_items(quarantine_id=order["id"])
        return {
            "quarantine_id": order["id"],
            "status": order["status"],
            "reason": order["data"].get("reason"),
            "blockages": self.describe_blockages(order, items),
            "affected": self._affected_objects(order, items),
        }

    # ------------------------------------------------------------------
    # Submission
    # ------------------------------------------------------------------

    def submit_quarantine(self, actor, order_id, data):
        animal_id = data["animal_id"]
        reason = str(data.get("reason", "")).strip()
        facility = str(data.get("facility", "")).strip()
        if not reason or not facility:
            raise ValidationError("reason and facility are required")
        animal = self.repository.get_entity(animal_id)
        if not animal:
            raise ValidationError("animal not found: " + animal_id)

        plan = self._plan_apply(animal_id)

        # Phase 1 (durable): first writer wins on reason + animal; the order
        # and its full coordination plan are committed up front, so a later
        # write failure leaves the unfinished items safely in the log.
        def _submit(conn):
            other = self._open_quarantine_for(
                animal_id, exclude_id=order_id, conn=conn
            )
            if other:
                raise ConflictError(
                    "animal %s already has open quarantine %s"
                    % (animal_id, other["id"]),
                    payload=self._conflict_payload(other),
                )
            self.repository.hold_quarantine_key("reason", reason, order_id, conn)
            self.repository.hold_quarantine_key("animal", animal_id, order_id, conn)
            self.repository.create_entity(
                order_id,
                "quarantine",
                "submitted",
                {
                    "animal_id": animal_id,
                    "reason": reason,
                    "facility": facility,
                    "submitted_by": actor.user_id,
                    "affected": [],
                    "blockages": [],
                },
                actor.user_id,
                connection=conn,
            )
            for item in plan:
                self.repository.add_item(
                    order_id,
                    APPLY,
                    item["kind"],
                    item["state"],
                    target_id=item.get("target_id"),
                    detail=item.get("detail"),
                    connection=conn,
                )
            self.repository.append_audit(
                order_id, actor.user_id, actor.role, "submit",
                None, "submitted",
                {"reason": reason, "facility": facility},
                connection=conn,
            )

        try:
            self.repository.run_tx(_submit)
        except ConflictError as exc:
            # Surface the existing order and its latest blocking list.
            existing_id = self.repository.get_quarantine_key("reason", reason)
            if not existing_id:
                existing = self._open_quarantine_for(animal_id)
                existing_id = existing["id"] if existing else None
            existing = (
                self.repository.get_entity(existing_id) if existing_id else None
            )
            if existing:
                raise ConflictError(
                    "open quarantine already exists for reason '%s'" % reason,
                    payload=self._conflict_payload(existing),
                )
            raise exc

        # Phase 2 (recoverable): process the planned items one by one.
        order = self.repository.get_entity(order_id)
        return self._run_apply(order, actor)

    def _plan_apply(self, animal_id):
        """Compute the coordination items needed to open an isolation order."""
        items = []
        # 1) Approved pairings occupying the individual are invalidated.
        for pairing in self._pairings_for_animal(animal_id):
            if pairing["status"] == "approved":
                items.append({
                    "kind": "invalidate_pairing",
                    "state": "pending",
                    "target_id": pairing["id"],
                    "detail": {"version": pairing["version"]},
                })
        # 2) An in-transit transfer cannot roll back: the order pauses until
        #    the individual arrives.
        for transfer in self._transfers_for_animal(animal_id):
            if transfer["status"] == "in_transit":
                items.append({
                    "kind": "transfer_in_transit",
                    "state": "waiting",
                    "target_id": transfer["id"],
                    "detail": {"version": transfer["version"]},
                })
        # 3) Mark the individual quarantined. Held until blockers clear so an
        #    individual awaiting isolation is never sent away.
        items.append({"kind": "animal_quarantine", "state": "waiting", "detail": {}})
        return items

    # ------------------------------------------------------------------
    # Apply phase
    # ------------------------------------------------------------------

    def _run_apply(self, order, actor=SYSTEM_ACTOR):
        """Process every unfinished apply-phase item; settle order status.

        ``waiting`` items are re-evaluated as well, because their prerequisite
        (e.g. transfer arrival) may have cleared since the last attempt.
        """
        items = self.repository.list_items(
            quarantine_id=order["id"], phase=APPLY, states=OPEN_ITEM_STATES
        )
        for item in items:
            self._apply_item(order, item, actor)
        items = self.repository.list_items(
            quarantine_id=order["id"], phase=APPLY, states=OPEN_ITEM_STATES
        )
        return self._finalize_apply(order, actor, items)

    def _apply_item(self, order, item, actor):
        def _work(conn):
            # Re-read under the write lock so concurrent commits are seen.
            current = self.repository.get_entity(order["id"], connection=conn)
            if current["status"] == "released":
                return
            if item["kind"] == "invalidate_pairing":
                self._do_invalidate_pairing(conn, order, item, actor)
            elif item["kind"] == "transfer_in_transit":
                self._do_check_transfer(conn, order, item, actor)
            elif item["kind"] == "animal_quarantine":
                self._do_quarantine_animal(conn, order, item, actor)
            else:
                self.repository.mark_item(item["id"], "done", connection=conn)

        try:
            self.repository.run_tx(_work)
        except Exception as exc:  # failed writes are kept; retry resumes later
            self.repository.mark_item(item["id"], "failed", error=str(exc))

    def _do_invalidate_pairing(self, conn, order, item, actor):
        pairing = self.repository.get_entity(
            item["target_id"], connection=conn
        )
        if not pairing or pairing["status"] in ("invalidated", "completed", "rejected"):
            self.repository.mark_item(item["id"], "done", connection=conn)
            return
        if pairing["status"] != "approved":
            self.repository.mark_item(item["id"], "done", connection=conn)
            return
        data = dict(pairing["data"])
        data.update({
            "invalidated_by_quarantine": order["id"],
            "invalidation_reason": order["data"].get("reason"),
            "invalidation_facility": order["data"].get("facility"),
        })
        self.repository.update_entity_in_tx(
            conn, pairing["id"], pairing["version"], "invalidated", data
        )
        self.repository.append_audit(
            pairing["id"], actor.user_id, actor.role, "invalidate",
            "approved", "invalidated", {"quarantine_id": order["id"]},
            connection=conn,
        )
        self.repository.mark_item(item["id"], "done", connection=conn)

    def _do_check_transfer(self, conn, order, item, actor):
        transfer = self.repository.get_entity(
            item["target_id"], connection=conn
        )
        if not transfer or transfer["status"] in ("completed", "cancelled"):
            self.repository.mark_item(item["id"], "done", connection=conn)
            return
        # Still in transit (a started transport never rolls back): keep
        # waiting for the arrival event. The "waiting" mark is committed.
        self.repository.mark_item(item["id"], "waiting", connection=conn)

    def _do_quarantine_animal(self, conn, order, item, actor):
        animal_id = order["data"]["animal_id"]
        open_items = self.repository.list_items(
            quarantine_id=order["id"], phase=APPLY, states=OPEN_ITEM_STATES,
            connection=conn,
        )
        blockers = [
            other for other in open_items
            if other["id"] != item["id"]
            and other["kind"] in ("invalidate_pairing", "transfer_in_transit")
        ]
        animal = self.repository.get_entity(animal_id, connection=conn)
        in_transit = any(
            transfer["status"] == "in_transit"
            for transfer in self._transfers_for_animal(animal_id, conn=conn)
        )
        if blockers or in_transit or not animal or animal["status"] == "deceased":
            self.repository.mark_item(item["id"], "waiting", connection=conn)
            return
        if animal["status"] == "quarantined":
            self.repository.mark_item(item["id"], "done", connection=conn)
            return
        data = dict(animal["data"])
        data["quarantine_order_id"] = order["id"]
        data["quarantine_facility"] = order["data"].get("facility")
        self.repository.update_entity_in_tx(
            conn, animal_id, animal["version"], "quarantined", data
        )
        self.repository.append_audit(
            animal_id, actor.user_id, actor.role, "quarantine_animal",
            "active", "quarantined", {"quarantine_id": order["id"]},
            connection=conn,
        )
        self.repository.mark_item(item["id"], "done", connection=conn)

    def _finalize_apply(self, order, actor, open_items):
        waiting_animal = any(
            item["kind"] == "animal_quarantine" for item in open_items
        )
        blockers = [item for item in open_items if item["kind"] != "animal_quarantine"]
        blockages = self.describe_blockages(order, open_items)
        affected = self._affected_objects(order)
        next_status = "blocked" if (blockers or waiting_animal) else "active"

        def _settle(conn):
            current = self.repository.get_entity(order["id"], connection=conn)
            data = dict(current["data"])
            data["blockages"] = blockages
            data["affected"] = affected
            self.repository.update_entity_in_tx(
                conn, order["id"], current["version"], next_status, data
            )
            if next_status != current["status"]:
                self.repository.append_audit(
                    order["id"], actor.user_id, actor.role, "coordinate",
                    current["status"], next_status, {"blockages": blockages},
                    connection=conn,
                )

        self.repository.run_tx(_settle)
        return self.repository.get_entity(order["id"])

    # ------------------------------------------------------------------
    # Release
    # ------------------------------------------------------------------

    def release_quarantine(self, actor, order_id):
        order = self.repository.get_entity(order_id)
        if not order:
            raise NotFoundError("quarantine not found: " + order_id)
        if order["status"] not in ("active", "blocked", "releasing", "released"):
            raise InvalidTransition(
                "cannot release quarantine from status %s" % order["status"]
            )
        # Phase 1 (atomic claim): the advisory lock + status flip + release
        # plan are one short transaction. Exactly one vet owns the phase; a
        # concurrent submitter gets the latest blocking list.
        def _claim(conn):
            try:
                self.repository.hold_coordination_lock("release", order_id, conn)
            except ConflictError:
                current = self.repository.get_entity(order_id, connection=conn)
                raise ConflictError(
                    "release already handled for quarantine %s" % order_id,
                    payload=self._conflict_payload(current),
                )
            current = self.repository.get_entity(order_id, connection=conn)
            if current["status"] not in ("active", "blocked"):
                raise ConflictError(
                    "release already handled for quarantine %s" % order_id,
                    payload=self._conflict_payload(current),
                )
            data = dict(current["data"])
            data["released_by"] = actor.user_id
            self.repository.update_entity_in_tx(
                conn, order_id, current["version"], "releasing", data
            )
            self.repository.append_audit(
                order_id, actor.user_id, actor.role, "release_request",
                current["status"], "releasing", {}, connection=conn,
            )
            self._plan_release(conn, current)

        try:
            self.repository.run_tx(_claim)
        except ConflictError:
            latest = self.repository.get_entity(order_id)
            if latest:
                raise ConflictError(
                    "release already handled for quarantine %s" % order_id,
                    payload=self._conflict_payload(latest),
                )
            raise

        # Phase 2 (recoverable): process the planned release items one
        # transaction at a time. A failed write leaves the item unfinished;
        # the advisory lock persists so only retry/restart resumes it.
        order = self.repository.get_entity(order_id)
        return self._run_release(order, actor)

    def _plan_release(self, conn, order):
        animal_id = order["data"]["animal_id"]
        # Pairings this order invalidated return for re-confirmation.
        for pairing in self._pairings_for_animal(animal_id, conn=conn):
            if (
                pairing["status"] == "invalidated"
                and pairing["data"].get("invalidated_by_quarantine") == order["id"]
            ):
                self.repository.add_item(
                    order["id"], RELEASE, "reconfirm_pairing", "pending",
                    target_id=pairing["id"],
                    detail={"version": pairing["version"]}, connection=conn,
                )
        # A transfer still in transit is not rolled back: wait for arrival.
        for transfer in self._transfers_for_animal(animal_id, conn=conn):
            if transfer["status"] == "in_transit":
                self.repository.add_item(
                    order["id"], RELEASE, "transfer_in_transit", "waiting",
                    target_id=transfer["id"], detail={}, connection=conn,
                )
        self.repository.add_item(
            order["id"], RELEASE, "animal_release", "waiting", detail={},
            connection=conn,
        )

    def _run_release(self, order, actor=SYSTEM_ACTOR):
        """Recovery path (retry / service restart): process release items one
        transaction at a time, so a failed write leaves the item unfinished."""
        items = self.repository.list_items(
            quarantine_id=order["id"], phase=RELEASE, states=OPEN_ITEM_STATES
        )
        for item in items:
            self._release_item(order, item, actor)
        open_items = self.repository.list_items(
            quarantine_id=order["id"], phase=RELEASE, states=OPEN_ITEM_STATES
        )

        blockages = self.describe_blockages(order, open_items)
        affected = self._affected_objects(order)
        next_status = "releasing" if open_items else "released"

        def _settle(conn):
            self._settle_release(
                conn,
                self.repository.get_entity(order["id"], connection=conn),
                actor,
                next_status=next_status,
                blockages=blockages,
                affected=affected,
            )

        self.repository.run_tx(_settle)
        return self.repository.get_entity(order["id"])

    def _process_release_items(self, conn, order):
        """Apply every release item inside the caller's transaction.

        Waiting items (in-transit transfers) merely stay "waiting"; they never
        abort the transaction, so a release requested mid-transit is durable.
        """
        items = self.repository.list_items(
            quarantine_id=order["id"], phase=RELEASE, states=OPEN_ITEM_STATES,
            connection=conn,
        )
        for item in items:
            self._apply_release_item(conn, order, item, SYSTEM_ACTOR)

    def _release_item(self, order, item, actor):
        def _work(conn):
            self._apply_release_item(conn, order, item, actor)

        try:
            self.repository.run_tx(_work)
        except Exception as exc:  # item stays unfinished; retry resumes it
            self.repository.mark_item(item["id"], "failed", error=str(exc))

    def _apply_release_item(self, conn, order, item, actor):
        """Idempotent release-item application; uses return values instead of
        exceptions so waiting never rolls back the surrounding transaction."""
        if item["kind"] == "reconfirm_pairing":
            self._do_return_pairing(conn, order, item, actor)
        elif item["kind"] == "transfer_in_transit":
            self._do_release_transfer(conn, order, item, actor)
        elif item["kind"] == "animal_release":
            self._do_release_animal(conn, order, item, actor)
        else:
            self.repository.mark_item(item["id"], "done", connection=conn)

    def _do_return_pairing(self, conn, order, item, actor):
        pairing = self.repository.get_entity(
            item["target_id"], connection=conn
        )
        if not pairing or pairing["status"] != "invalidated":
            # Missing pairing or already re-confirmed/moved on: nothing to do.
            self.repository.mark_item(item["id"], "done", connection=conn)
            return
        data = dict(pairing["data"])
        data["awaiting_reconfirmation"] = True
        self.repository.update_entity_in_tx(
            conn, pairing["id"], pairing["version"], "reconfirm_required", data
        )
        self.repository.append_audit(
            pairing["id"], actor.user_id, actor.role, "release_quarantine",
            "invalidated", "reconfirm_required", {"quarantine_id": order["id"]},
            connection=conn,
        )
        self.repository.mark_item(item["id"], "done", connection=conn)

    def _do_release_transfer(self, conn, order, item, actor):
        transfer = self.repository.get_entity(
            item["target_id"], connection=conn
        )
        if not transfer or transfer["status"] in ("completed", "cancelled"):
            self.repository.mark_item(item["id"], "done", connection=conn)
            return
        # Started transport is never rolled back; the order waits for arrival.
        self.repository.mark_item(item["id"], "waiting", connection=conn)

    def _do_release_animal(self, conn, order, item, actor):
        animal_id = order["data"]["animal_id"]
        in_transit = any(
            transfer["status"] == "in_transit"
            for transfer in self._transfers_for_animal(animal_id, conn=conn)
        )
        if in_transit:
            self.repository.mark_item(item["id"], "waiting", connection=conn)
            return
        animal = self.repository.get_entity(animal_id, connection=conn)
        if animal and animal["status"] == "quarantined":
            data = dict(animal["data"])
            data["quarantine_order_id"] = None
            data["quarantine_facility"] = None
            self.repository.update_entity_in_tx(
                conn, animal_id, animal["version"], "active", data
            )
            self.repository.append_audit(
                animal_id, actor.user_id, actor.role, "release_quarantine",
                "quarantined", "active", {"quarantine_id": order["id"]},
                connection=conn,
            )
        self.repository.mark_item(item["id"], "done", connection=conn)

    def _settle_release(self, conn, current, actor, next_status, blockages, affected):
        data = dict(current["data"])
        data["blockages"] = blockages
        data["affected"] = affected
        if next_status == "released":
            data["released"] = True
        self.repository.update_entity_in_tx(
            conn, current["id"], current["version"], next_status, data
        )
        if next_status == "released":
            self.repository.release_quarantine_keys(
                current["id"], connection=conn
            )
            # Release phase finished: free the mutex. A "releasing" order that
            # survives a crash keeps the lock so only recovery/retry owns it.
            self.repository.delete_coordination_lock(
                "release", current["id"], connection=conn
            )
        self.repository.append_audit(
            current["id"], actor.user_id, actor.role, "coordinate",
            current["status"], next_status, {"blockages": blockages},
            connection=conn,
        )

    def _finalize_release(self, order, actor):
        open_items = self.repository.list_items(
            quarantine_id=order["id"], phase=RELEASE, states=OPEN_ITEM_STATES
        )
        blockages = self.describe_blockages(order, open_items)
        affected = self._affected_objects(order)
        next_status = "releasing" if open_items else "released"

        def _settle(conn):
            current = self.repository.get_entity(order["id"], connection=conn)
            self._settle_release(
                conn, current, actor,
                next_status=next_status, blockages=blockages, affected=affected,
            )

        self.repository.run_tx(_settle)
        return self.repository.get_entity(order["id"])

    # ------------------------------------------------------------------
    # Retry and recovery
    # ------------------------------------------------------------------

    def retry(self, actor, order_id):
        """Re-run only the unfinished objects of one order."""
        if actor.role not in ("admin", "veterinarian"):
            raise PermissionDenied("only veterinarians may retry coordination")
        order = self.repository.get_entity(order_id)
        if not order:
            raise NotFoundError("quarantine not found: " + order_id)
        if order["status"] == "released":
            return order
        if order["status"] in ("submitted", "active", "blocked"):
            return self._run_apply(order, actor)
        if order["status"] == "releasing":
            return self._resume_release(order, actor)
        raise InvalidTransition(
            "cannot retry quarantine from status %s" % order["status"]
        )

    def _resume_release(self, order, actor=SYSTEM_ACTOR):
        """Take over a release phase interrupted by a crash, rebuilding its
        plan if the planning transaction never committed."""
        existing = self.repository.list_items(
            quarantine_id=order["id"], phase=RELEASE
        )
        if not existing:
            # Stale lock from a release transaction that rolled back: the
            # retry/recovery path is the legitimate owner and takes over.
            def _rebuild(conn):
                current = self.repository.get_entity(order["id"], connection=conn)
                if current["status"] != "releasing":
                    return
                self._plan_release(conn, current)
                self._process_release_items(conn, current)
                open_items = self.repository.list_items(
                    quarantine_id=order["id"], phase=RELEASE,
                    states=OPEN_ITEM_STATES, connection=conn,
                )
                blockages = self.describe_blockages(current, open_items)
                affected = self._affected_objects(current, open_items)
                next_status = "releasing" if open_items else "released"
                settled = self.repository.get_entity(order["id"], connection=conn)
                self._settle_release(
                    conn, settled, actor,
                    next_status=next_status, blockages=blockages, affected=affected,
                )

            self.repository.run_tx(_rebuild)
            return self.repository.get_entity(order["id"])
        return self._run_release(order, actor)

    def resume_pending(self):
        """Called at service start: continue every unfinished order.

        Finished items are untouched, so work resumes exactly where it stopped.
        """
        resumed = []
        for order in self.repository.list_entities(kind="quarantine"):
            if order["status"] in ("submitted", "active", "blocked"):
                self._run_apply(order)
                resumed.append(order["id"])
            elif order["status"] == "releasing":
                self._resume_release(order)
                resumed.append(order["id"])
        return resumed

    def transfer_arrived(self, transfer):
        """Hook after a transfer reaches its destination.

        In-transit transport is not rolled back; on arrival every order paused
        on this transfer is resumed automatically.
        """
        advanced = []
        for order in self.repository.list_entities(kind="quarantine"):
            waiting = self.repository.list_items(
                quarantine_id=order["id"], states=OPEN_ITEM_STATES
            )
            if not any(item["target_id"] == transfer["id"] for item in waiting):
                continue
            if order["status"] in ("submitted", "active", "blocked"):
                self._run_apply(order)
                advanced.append(order["id"])
            elif order["status"] == "releasing":
                self._run_release(order)
                advanced.append(order["id"])
        return advanced
