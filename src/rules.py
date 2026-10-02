from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)

# Quarantine orders that still coordinate the workflow (not yet released).
OPEN_QUARANTINE_STATUSES = ("submitted", "active", "blocked", "releasing")


def _validate_animal(actor, data, lookup):
    if data.get("sex") not in ("male", "female", "unknown"):
        raise ValidationError("sex must be male, female or unknown")


def inbreeding_coefficient(sire, dam):
    if not sire or not dam:
        return 1.0
    sire_id = sire.get("id")
    dam_id = dam.get("id")
    if sire_id is None or dam_id is None:
        return 0.0
    if sire_id == dam_id:
        return 0.5
    if sire.get("sire_id") == dam_id or dam.get("sire_id") == sire_id:
        return 0.25
    return 0.0


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _find_open_quarantine(lookup, animal_id):
    """Return the first non-released quarantine order for an animal."""
    if lookup is None:
        return None
    for order in lookup("quarantine", "animal_id", animal_id) or []:
        if order["status"] in OPEN_QUARANTINE_STATUSES:
            return order
    return None


def _validate_pairing(actor, entity, data, lookup):
    sire = _find_one(lookup, "animal", "id", data.get("sire_id"))
    dam = _find_one(lookup, "animal", "id", data.get("dam_id"))
    if not sire or not dam:
        raise ValidationError("pairing requires two existing animals")
    # Open isolation orders block fresh approval first, so the page can show
    # the concrete invalidation reason instead of a generic status error.
    for animal in (sire, dam):
        order = _find_open_quarantine(lookup, animal["id"])
        if order:
            raise InvalidTransition(
                "animal %s is covered by open quarantine %s (%s)"
                % (animal["id"], order["id"], order["data"].get("reason", "")),
                {"quarantine_id": order["id"], "reason": order["data"].get("reason")},
            )
    if sire["status"] != "active" or dam["status"] != "active":
        raise ValidationError("pairing animals must be active")
    if inbreeding_coefficient(sire["data"], dam["data"]) > 0.125:
        raise ValidationError("pairing exceeds inbreeding threshold")
    return {"approved_by": actor.user_id}


def _validate_reconfirm(actor, entity, data, lookup):
    sire_id = entity["data"].get("sire_id")
    dam_id = entity["data"].get("dam_id")
    for animal_id in (sire_id, dam_id):
        order = _find_open_quarantine(lookup, animal_id)
        if order:
            raise InvalidTransition(
                "animal %s is still under open quarantine %s"
                % (animal_id, order["id"]),
                {"quarantine_id": order["id"], "reason": order["data"].get("reason")},
            )
    return {"reconfirmed_by": actor.user_id}


def _validate_ship(actor, entity, data, lookup):
    animal_id = entity["data"].get("animal_id")
    order = _find_open_quarantine(lookup, animal_id)
    if order:
        # An individual to be isolated must not be sent away; once in transit
        # the transfer cannot roll back, so shipping is refused up front.
        raise InvalidTransition(
            "cannot ship animal %s under open quarantine %s"
            % (animal_id, order["id"]),
            {"quarantine_id": order["id"], "reason": order["data"].get("reason")},
        )
    animal = _find_one(lookup, "animal", "id", animal_id)
    if animal and animal["status"] != "active":
        raise InvalidTransition(
            "animal %s is not active (status %s)" % (animal_id, animal["status"])
        )


def _validate_quarantine_create(actor, data, lookup):
    animal = _find_one(lookup, "animal", "id", data.get("animal_id"))
    if not animal:
        raise ValidationError("quarantine requires an existing animal")
    if animal["status"] == "deceased":
        raise ValidationError("cannot quarantine a deceased animal")


CUSTOM_CREATE = {'animal': _validate_animal, 'quarantine': _validate_quarantine_create}
CUSTOM_TRANSITIONS = {
    ('pairing', 'approve'): _validate_pairing,
    ('pairing', 'reconfirm'): _validate_reconfirm,
    ('transfer', 'ship'): _validate_ship,
}


class RuleEngine:
    ALIASES = {
        'animals': 'animal',
        'pairings': 'pairing',
        'transfers': 'transfer',
        'quarantines': 'quarantine',
    }
    INITIAL_STATUS = {
        'animal': 'active',
        'pairing': 'proposed',
        'transfer': 'planned',
        # Every isolation order starts as "submitted"; the coordination
        # workflow moves it to "active" or "blocked" within the same request.
        'quarantine': 'submitted',
    }
    TRANSITIONS = {
        'animal': {
            'mark_deceased': (('active', 'quarantined'), 'deceased'),
        },
        'pairing': {
            'approve': (('proposed',), 'approved'),
            'reject': (('proposed',), 'rejected'),
            # An approved pairing invalidated by an isolation order must be
            # re-confirmed after release before it can be completed.
            'reconfirm': (('reconfirm_required',), 'approved'),
            'complete': (('approved',), 'completed'),
        },
        'transfer': {
            'authorize': (('planned',), 'authorized'),
            'ship': (('authorized',), 'in_transit'),
            'arrive': (('in_transit',), 'completed'),
        },
        'quarantine': {
            # Vet submits: reason + facility recorded, coordination runs.
            'submit': (('submitted',), 'submitted'),
            # Retry the still-unfinished coordination items.
            'retry': (('active', 'blocked', 'releasing'), 'submitted'),
        },
    }
    CREATE_REQUIRED = {
        'animal': ('name', 'sex'),
        'pairing': ('proposed_by',),
        'transfer': ('animal_id', 'from_institution', 'to_institution'),
        'quarantine': ('animal_id', 'reason', 'facility'),
    }
    ACTION_REQUIRED = {
        ('animal', 'mark_deceased'): ('cause',),
        ('pairing', 'approve'): ('sire_id', 'dam_id', 'approvals'),
        ('pairing', 'reject'): ('reason',),
        ('pairing', 'reconfirm'): ('approvals',),
        ('pairing', 'complete'): ('offspring_ids',),
        ('transfer', 'authorize'): ('permit_id',),
        ('transfer', 'ship'): ('transport_id',),
        ('transfer', 'arrive'): ('arrival_date',),
    }
    CREATE_ROLES = {
        'animal': ('admin', 'registrar'),
        'pairing': ('admin', 'coordinator'),
        'transfer': ('admin', 'registrar'),
        # Only veterinarians open isolation orders.
        'quarantine': ('admin', 'veterinarian'),
    }
    ROLE_ACTIONS = {
        'mark_deceased': ('admin', 'veterinarian'),
        'approve': ('admin', 'coordinator'),
        'reject': ('admin', 'coordinator'),
        'reconfirm': ('admin', 'coordinator', 'veterinarian'),
        'complete': ('admin', 'coordinator'),
        'authorize': ('admin', 'registrar'),
        'ship': ('admin', 'registrar'),
        'arrive': ('admin', 'registrar'),
        'retry': ('admin', 'veterinarian'),
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
