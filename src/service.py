from uuid import uuid4

from .audit import AuditTrail
from .domain import (
    Actor,
    ConflictError,
    CoordinationConflict,
    InvalidTransition,
    NotFoundError,
)
from .repository import utcnow
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        # A completed transport is the one blocker that can never be rolled
        # back; once it arrives, any paused quarantine for the animal can
        # resume. This is the link that turns the three flows into one
        # recoverable coordination process.
        if entity["kind"] == "transfer" and action == "arrive":
            animal_id = merged.get("animal_id")
            if animal_id:
                self._auto_resume_quarantine(animal_id, actor)
        return updated

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)

    # ------------------------------------------------------------------
    # 隔离协调流程（quarantine coordination process）
    #
    # 隔离单是协调流程的状态载体：它持久化原因、场所、受影响对象清单和
    # 每对象的处理进度。写入失败后只续做未完成对象；进程重启后通过
    # recover_paused() 接着处理。
    # ------------------------------------------------------------------

    def _require_quarantine(self, quarantine_id):
        order = self.repository.get_entity(quarantine_id)
        if not order or order["kind"] != "quarantine":
            raise NotFoundError("quarantine not found: " + str(quarantine_id))
        return order

    def _find_quarantine(self, animal_id, reason):
        rows = self.repository.find_entities("quarantine", "animal_id", animal_id) or []
        for row in rows:
            if row["data"].get("reason") == reason:
                return row
        return None

    def _pairings_for_animal(self, animal_id):
        result = []
        for pairing in self.repository.list_entities(kind="pairing"):
            data = pairing["data"]
            if data.get("sire_id") == animal_id or data.get("dam_id") == animal_id:
                result.append(pairing)
        return result

    def _unfinished_pairings(self, animal_id):
        return [
            pairing
            for pairing in self._pairings_for_animal(animal_id)
            if pairing["status"] in ("proposed", "approved")
        ]

    def _in_transit_transfers(self, animal_id):
        result = []
        for transfer in self.repository.list_entities(kind="transfer"):
            if transfer["status"] == "in_transit" and transfer["data"].get("animal_id") == animal_id:
                result.append(transfer)
        return result

    @staticmethod
    def _build_blockers(pairings, transfers):
        blockers = []
        for pairing in pairings:
            blockers.append(
                {"kind": "pairing", "id": pairing["id"], "status": pairing["status"]}
            )
        for transfer in transfers:
            blockers.append(
                {"kind": "transfer", "id": transfer["id"], "status": transfer["status"]}
            )
        return blockers

    def _invalidate_pairing(self, pairing_id, actor):
        pairing = self.repository.get_entity(pairing_id)
        if not pairing or pairing["kind"] != "pairing" or pairing["status"] != "approved":
            return False  # 已失效或未批准：无需处理（幂等）
        merged = dict(pairing["data"])
        merged["invalidated_by"] = "quarantine"
        merged["invalidated_at"] = utcnow()
        self.repository.update_entity(pairing_id, None, "invalidated", merged)
        self.audit.record(
            pairing_id, actor, "invalidate", pairing["status"], "invalidated",
            {"reason": "quarantine"},
        )
        return True

    def _reconfirm_pairing(self, pairing_id, actor):
        pairing = self.repository.get_entity(pairing_id)
        if not pairing or pairing["kind"] != "pairing" or pairing["status"] != "invalidated":
            return False  # 非失效状态：无需重新确认
        merged = dict(pairing["data"])
        merged["reconfirmed_by"] = "quarantine_release"
        merged["reconfirmed_at"] = utcnow()
        self.repository.update_entity(pairing_id, None, "proposed", merged)
        self.audit.record(
            pairing_id, actor, "reconfirm", pairing["status"], "proposed",
            {"reason": "quarantine_release"},
        )
        return True

    def _sync_animal_status(self, animal_id, target_status, actor):
        animal = self.repository.get_entity(animal_id)
        if not animal or animal["kind"] != "animal":
            return False
        if target_status == "quarantined" and animal["status"] == "active":
            self.repository.update_entity(animal_id, None, "quarantined", dict(animal["data"]))
            self.audit.record(animal_id, actor, "quarantine_animal", animal["status"], "quarantined", {"reason": "quarantine_order"})
            return True
        if target_status == "active" and animal["status"] == "quarantined":
            self.repository.update_entity(animal_id, None, "active", dict(animal["data"]))
            self.audit.record(animal_id, actor, "release_quarantine", animal["status"], "active", {"reason": "quarantine_release"})
            return True
        return False

    def submit_quarantine(self, actor, animal_id, reason, location):
        animal_id = str(animal_id or "")
        reason = str(reason or "").strip()
        location = str(location or "").strip()
        self.rules.validate_create(
            actor, "quarantine",
            {"animal_id": animal_id, "reason": reason, "location": location},
            self._lookup,
        )
        animal = self.repository.get_entity(animal_id)
        if not animal or animal["kind"] != "animal":
            raise NotFoundError("animal not found: " + animal_id)
        # 幂等：同一原因只留一张隔离单。重复提交返回已有单据（先到先生效）。
        existing = self._find_quarantine(animal_id, reason)
        if existing:
            return existing
        # 扫描受影响对象：未完成配对 + 在途运输。
        pairings = self._unfinished_pairings(animal_id)
        transfers = self._in_transit_transfers(animal_id)
        blockers = self._build_blockers(pairings, transfers)
        # 批准随即失效（逐对象持久化，失败后只续做未完成对象）。
        checklist = {"pairings": {}, "transfers": {}}
        for pairing in pairings:
            if pairing["status"] == "approved":
                self._invalidate_pairing(pairing["id"], actor)
                checklist["pairings"][pairing["id"]] = "invalidated"
        for transfer in transfers:
            checklist["transfers"][transfer["id"]] = "blocking"
        # 在途运输不得回退：只要还有在途运输，隔离单暂停，等待到达后恢复。
        has_blocker = bool(transfers) or any(p["status"] == "proposed" for p in pairings)
        status = "paused" if has_blocker else "active"
        data = {
            "animal_id": animal_id,
            "reason": reason,
            "location": location,
            "blockers": blockers,
            "checklist": checklist,
        }
        order = self.repository.create_entity(
            str(uuid4()), "quarantine", status, data, actor.user_id
        )
        self.audit.record(
            order["id"], actor, "submit_quarantine", None, status,
            {"animal_id": animal_id, "reason": reason, "location": location, "blockers": blockers},
        )
        if status == "active":
            self._sync_animal_status(animal_id, "quarantined", actor)
        return order

    def resume_quarantine(self, actor, quarantine_id, expected_version=None):
        order = self._require_quarantine(quarantine_id)
        # 乐观锁竞争：先到先生效，败者立刻拿到最新阻塞清单。
        if expected_version is not None and order["version"] != int(expected_version):
            fresh = self.repository.get_entity(quarantine_id)
            raise CoordinationConflict(
                "quarantine was updated by another user; latest blocking list attached",
                details=fresh,
            )
        if order["status"] in ("active", "released"):
            return order  # 幂等：已生效或已解除，直接返回
        data = dict(order["data"])
        animal_id = data.get("animal_id")
        pairings = self._unfinished_pairings(animal_id)
        transfers = self._in_transit_transfers(animal_id)
        checklist = dict(data.get("checklist", {"pairings": {}, "transfers": {}}))
        checklist.setdefault("pairings", {})
        checklist.setdefault("transfers", {})
        for pairing in pairings:
            if pairing["status"] == "approved":
                self._invalidate_pairing(pairing["id"], actor)
                checklist["pairings"][pairing["id"]] = "invalidated"
        for transfer in transfers:
            checklist["transfers"][transfer["id"]] = "blocking"
        blockers = self._build_blockers(pairings, transfers)
        has_blocker = bool(transfers) or any(p["status"] == "proposed" for p in pairings)
        new_status = "paused" if has_blocker else "active"
        data["blockers"] = blockers
        data["checklist"] = checklist
        if new_status == "active":
            data["activated_at"] = utcnow()
        try:
            updated = self.repository.update_entity(
                quarantine_id, expected_version, new_status, data
            )
        except ConflictError:
            # 乐观锁竞争：先到先生效，败者拿到最新阻塞清单。
            fresh = self.repository.get_entity(quarantine_id)
            raise CoordinationConflict(
                "quarantine was updated by another user; latest blocking list attached",
                details=fresh,
            )
        self.audit.record(
            quarantine_id, actor, "resume_quarantine", order["status"], new_status,
            {"blockers": blockers},
        )
        if new_status == "active":
            self._sync_animal_status(animal_id, "quarantined", actor)
        return updated

    def release_quarantine(self, actor, quarantine_id, expected_version=None):
        order = self._require_quarantine(quarantine_id)
        # 乐观锁竞争：先到先生效，败者立刻拿到最新状态（含失效原因）。
        if expected_version is not None and order["version"] != int(expected_version):
            fresh = self.repository.get_entity(quarantine_id)
            raise CoordinationConflict(
                "quarantine was updated by another user; latest state attached",
                details=fresh,
            )
        if order["status"] != "active":
            raise InvalidTransition(
                "cannot release quarantine in status %s" % order["status"]
            )
        data = dict(order["data"])
        animal_id = data.get("animal_id")
        # 解除隔离后未完成配对重新确认（失效 -> 待批准）。
        reconfirmed = []
        for pairing in self._pairings_for_animal(animal_id):
            if pairing["status"] == "invalidated":
                self._reconfirm_pairing(pairing["id"], actor)
                reconfirmed.append(pairing["id"])
        data["reconfirmed_pairings"] = reconfirmed
        data["released_at"] = utcnow()
        try:
            updated = self.repository.update_entity(
                quarantine_id, expected_version, "released", data
            )
        except ConflictError:
            fresh = self.repository.get_entity(quarantine_id)
            raise CoordinationConflict(
                "quarantine was updated by another user; latest state attached",
                details=fresh,
            )
        self.audit.record(
            quarantine_id, actor, "release_quarantine", order["status"], "released",
            {"reconfirmed_pairings": reconfirmed},
        )
        self._sync_animal_status(animal_id, "active", actor)
        return updated

    def _auto_resume_quarantine(self, animal_id, actor):
        orders = self.repository.find_entities("quarantine", "animal_id", animal_id) or []
        for order in orders:
            if order["status"] != "paused":
                continue
            try:
                self.resume_quarantine(actor, order["id"], expected_version=None)
            except Exception:
                pass

    def recover_paused(self):
        """进程重启后接着处理所有暂停的隔离单。"""
        orders = self.repository.list_entities(kind="quarantine", status="paused") or []
        actor = Actor("system", "admin")
        recovered = []
        for order in orders:
            try:
                updated = self.resume_quarantine(actor, order["id"], expected_version=None)
                recovered.append({"id": order["id"], "status": updated["status"]})
            except Exception as exc:
                recovered.append({"id": order["id"], "status": "error", "error": str(exc)})
        return recovered
