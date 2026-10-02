from uuid import uuid4

from .audit import AuditTrail
from .coordinator import Coordinator
from .domain import ConflictError, NotFoundError
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None, coordinator=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)
        self.coordinator = coordinator or Coordinator(repository, self.audit)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def recover_on_startup(self):
        """Resume unfinished coordination items after a service restart."""
        return self.coordinator.resume_pending()

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        # Base validation (roles, required fields, cross-object checks).
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)

        if kind == "quarantine":
            # The coordinator creates the order and runs the resumable
            # coordination (invalidations, transit pause, animal status).
            entity = self.coordinator.submit_quarantine(actor, entity_id, payload)
        else:
            status = self.rules.initial_status(kind)
            entity = self.repository.create_entity(
                entity_id, kind, status, payload, actor.user_id
            )
            self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})

        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity["id"])
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)

        if entity["kind"] == "quarantine":
            if action == "release":
                return self.coordinator.release_quarantine(actor, entity_id)
            if action == "retry":
                return self.coordinator.retry(actor, entity_id)

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

        # Once an in-transit transfer arrives, isolation orders paused on it
        # resume automatically (the transport itself is never rolled back).
        if entity["kind"] == "transfer" and next_status == "completed":
            self.coordinator.transfer_arrived(updated)
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
