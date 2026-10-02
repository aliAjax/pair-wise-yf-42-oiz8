import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from src.coordinator import Coordinator
from src.domain import (
    Actor,
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService

VET = Actor("vet-1", "veterinarian")
ADMIN = Actor("admin", "admin")
COORD = Actor("coord-1", "coordinator")
REG = Actor("reg-1", "registrar")


class CoordinationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())

    def tearDown(self):
        self.tmp.cleanup()

    # ------------------------------------------------------------------
    # Fixtures
    # ------------------------------------------------------------------

    def _animal(self, name="A", sex="male"):
        return self.service.create(ADMIN, "animal", {"name": name, "sex": sex})

    def _pair(self, sire, dam, approve=True):
        pair = self.service.create(COORD, "pairing", {"proposed_by": "coord-1"})
        if approve:
            pair = self.service.transition(
                COORD, pair["id"], "approve",
                {"sire_id": sire["id"], "dam_id": dam["id"], "approvals": ["vet-1"]},
            )
        return pair

    def _transfer(self, animal, state="planned"):
        transfer = self.service.create(
            REG, "transfer",
            {"animal_id": animal["id"], "from_institution": "Zoo-A", "to_institution": "Zoo-B"},
        )
        if state in ("authorized", "in_transit", "completed"):
            transfer = self.service.transition(
                REG, transfer["id"], "authorize", {"permit_id": "P-1"}
            )
        if state in ("in_transit", "completed"):
            transfer = self.service.transition(
                REG, transfer["id"], "ship", {"transport_id": "T-1"}
            )
        if state == "completed":
            transfer = self.service.transition(
                REG, transfer["id"], "arrive", {"arrival_date": "2026-10-01"}
            )
        return transfer

    def _quarantine(self, animal, reason="fever", facility="Ward-1", actor=VET):
        return self.service.create(
            actor, "quarantine",
            {"animal_id": animal["id"], "reason": reason, "facility": facility},
        )

    # ------------------------------------------------------------------
    # Basic coordination
    # ------------------------------------------------------------------

    def test_vet_records_reason_and_facility_and_order_activates(self):
        animal = self._animal()
        order = self._quarantine(animal)
        self.assertEqual(order["status"], "active")
        self.assertEqual(order["data"]["reason"], "fever")
        self.assertEqual(order["data"]["facility"], "Ward-1")
        self.assertEqual(order["data"]["submitted_by"], "vet-1")
        animal = self.service.get(animal["id"])
        self.assertEqual(animal["status"], "quarantined")
        self.assertEqual(animal["data"]["quarantine_facility"], "Ward-1")

    def test_only_veterinarian_or_admin_may_submit(self):
        animal = self._animal()
        with self.assertRaises(PermissionDenied):
            self._quarantine(animal, actor=REG)

    def test_reason_and_facility_are_required(self):
        animal = self._animal()
        with self.assertRaises(ValidationError):
            self.service.create(
                VET, "quarantine",
                {"animal_id": animal["id"], "reason": "fever"},
            )

    def test_approved_pairing_is_invalidated_immediately(self):
        sire = self._animal("M", "male")
        dam = self._animal("F", "female")
        pair = self._pair(sire, dam)
        order = self._quarantine(sire)
        pair = self.service.get(pair["id"])
        self.assertEqual(pair["status"], "invalidated")
        self.assertEqual(pair["data"]["invalidated_by_quarantine"], order["id"])
        self.assertEqual(pair["data"]["invalidation_reason"], "fever")
        self.assertIn({"type": "pairing", "id": pair["id"]}, order["data"]["affected"])

    def test_pairing_approval_blocked_while_quarantine_open(self):
        sire = self._animal("M", "male")
        dam = self._animal("F", "female")
        self._quarantine(sire)
        pair = self.service.create(COORD, "pairing", {"proposed_by": "coord-1"})
        with self.assertRaises(InvalidTransition) as caught:
            self.service.transition(
                COORD, pair["id"], "approve",
                {"sire_id": sire["id"], "dam_id": dam["id"], "approvals": ["vet-1"]},
            )
        # The failure names the blocking isolation order and its reason.
        self.assertIn("quarantine", str(caught.exception).lower())

    # ------------------------------------------------------------------
    # In-transit transfers pause the order; shipping is blocked
    # ------------------------------------------------------------------

    def test_in_transit_transfer_blocks_activation_and_is_listed(self):
        animal = self._animal()
        transfer = self._transfer(animal, state="in_transit")
        order = self._quarantine(animal)
        self.assertEqual(order["status"], "blocked")
        self.assertEqual(
            order["data"]["blockages"][0]["type"], "transfer_in_transit"
        )
        self.assertEqual(order["data"]["blockages"][0]["target_id"], transfer["id"])
        self.assertIn({"type": "transfer", "id": transfer["id"]},
                      order["data"]["affected"])
        # Individual is not sent away / not quarantined while in transit.
        self.assertEqual(self.service.get(animal["id"])["status"], "active")

    def test_arrival_resumes_blocked_order_automatically(self):
        animal = self._animal()
        transfer = self._transfer(animal, state="in_transit")
        order = self._quarantine(animal)
        self.assertEqual(order["status"], "blocked")
        # A second transfer (the bug scenario: individual to be isolated must
        # not be shipped) must be refused while the order is open.
        transfer2 = self._transfer(animal, state="authorized")
        with self.assertRaises(InvalidTransition):
            self.service.transition(REG, transfer2["id"], "ship",
                                    {"transport_id": "T-2"})
        # The existing in-transit transfer arrives -> order resumes.
        arrived = self.service.transition(
            REG, transfer["id"], "arrive", {"arrival_date": "2026-10-02"}
        )
        self.assertEqual(arrived["status"], "completed")
        order = self.service.get(order["id"])
        self.assertEqual(order["status"], "active")
        self.assertEqual(self.service.get(animal["id"])["status"], "quarantined")

    def test_started_transport_is_not_rolled_back_on_release(self):
        animal = self._animal()
        transfer = self._transfer(animal, state="in_transit")
        order = self._quarantine(animal)
        self.assertEqual(order["status"], "blocked")
        # Release may be requested even while blocked: the pairing is already
        # returned, but the started transport is not rolled back, so the order
        # stays "releasing" until arrival.
        releasing = self.service.transition(VET, order["id"], "release", {})
        self.assertEqual(releasing["status"], "releasing")
        self.assertEqual(self.service.get(transfer["id"])["status"], "in_transit")
        pairings = self.repo.list_entities(kind="pairing")
        self.assertTrue(
            all(p["status"] != "invalidated" for p in pairings)
        )
        # Arrival finishes the release automatically.
        self.service.transition(
            REG, transfer["id"], "arrive", {"arrival_date": "2026-10-02"}
        )
        self.assertEqual(self.service.get(order["id"])["status"], "released")
        self.assertEqual(self.service.get(animal["id"])["status"], "active")

    # ------------------------------------------------------------------
    # Release and re-confirmation
    # ------------------------------------------------------------------

    def test_release_returns_pairing_for_reconfirmation(self):
        sire = self._animal("M", "male")
        dam = self._animal("F", "female")
        pair = self._pair(sire, dam)
        order = self._quarantine(sire)
        self.assertEqual(self.service.get(pair["id"])["status"], "invalidated")

        released = self.service.transition(VET, order["id"], "release", {})
        self.assertEqual(released["status"], "released")
        pair = self.service.get(pair["id"])
        self.assertEqual(pair["status"], "reconfirm_required")
        self.assertTrue(pair["data"]["awaiting_reconfirmation"])
        self.assertEqual(self.service.get(sire["id"])["status"], "active")

        # Re-confirm the old approval, then complete.
        pair = self.service.transition(
            COORD, pair["id"], "reconfirm", {"approvals": ["vet-1"]}
        )
        self.assertEqual(pair["status"], "approved")
        pair = self.service.transition(
            COORD, pair["id"], "complete", {"offspring_ids": ["kid-1"]}
        )
        self.assertEqual(pair["status"], "completed")

    def test_release_waiting_for_in_transit_then_arrival_finishes_it(self):
        animal = self._animal()
        # Order activates first, transfer happens while quarantined is
        # impossible; instead: transfer in transit -> blocked order -> arrival
        # activates -> release, then a new shipment during release is blocked.
        self._transfer(animal, state="in_transit")
        order = self._quarantine(animal)
        # Arrival activates the order.
        transfer_id = self.repo.list_entities(kind="transfer")[0]["id"]
        self.service.transition(REG, transfer_id, "arrive",
                                {"arrival_date": "2026-10-02"})
        order = self.service.get(order["id"])
        self.assertEqual(order["status"], "active")
        released = self.service.transition(VET, order["id"], "release", {})
        self.assertEqual(released["status"], "released")

    # ------------------------------------------------------------------
    # Uniqueness and concurrency
    # ------------------------------------------------------------------

    def test_same_reason_keeps_a_single_open_order(self):
        animal = self._animal()
        first = self._quarantine(animal, reason="same")
        animal2 = self._animal("B", "female")
        try:
            self._quarantine(animal2, reason="same")
            self.fail("expected ConflictError")
        except ConflictError as exc:
            self.assertEqual(exc.payload["quarantine_id"], first["id"])
            # Loser receives the latest blockage list.
            self.assertIn("blockages", exc.payload)

    def test_same_animal_second_open_order_conflicts(self):
        animal = self._animal()
        self._quarantine(animal, reason="r1")
        with self.assertRaises(ConflictError) as caught:
            self._quarantine(animal, reason="r2")
        self.assertIn("quarantine_id", caught.exception.payload)

    def test_same_reason_allowed_after_release(self):
        animal = self._animal()
        order = self._quarantine(animal, reason="once")
        self.service.transition(VET, order["id"], "release", {})
        second = self._quarantine(animal, reason="once")
        self.assertEqual(second["status"], "active")

    def test_concurrent_submissions_first_writer_wins(self):
        animal = self._animal()
        animal2 = self._animal("B", "female")
        barrier = threading.Barrier(2)
        results = []

        def submit(target, reason):
            try:
                barrier.wait(timeout=5)
                results.append(("ok", self._quarantine(target, reason=reason)["id"]))
            except ConflictError as exc:
                results.append(("conflict", exc.payload.get("quarantine_id")))
            except Exception as exc:  # pragma: no cover
                results.append(("error", str(exc)))

        t1 = threading.Thread(target=submit, args=(animal, "race"))
        t2 = threading.Thread(target=submit, args=(animal2, "race"))
        t1.start(); t2.start(); t1.join(); t2.join()
        statuses = sorted(r[0] for r in results)
        self.assertEqual(statuses, ["conflict", "ok"])
        winner = next(r[1] for r in results if r[0] == "ok")
        loser_sees = next(r[1] for r in results if r[0] == "conflict")
        self.assertEqual(winner, loser_sees)

    def test_concurrent_releases_first_wins_loser_gets_blocking_list(self):
        animal = self._animal()
        order = self._quarantine(animal)
        barrier = threading.Barrier(2)
        results = []

        def release(vet):
            try:
                barrier.wait(timeout=5)
                self.service.transition(vet, order["id"], "release", {})
                results.append(("ok", None))
            except ConflictError as exc:
                results.append(("conflict", exc.payload.get("status")))
            except Exception as exc:  # pragma: no cover
                results.append(("error", str(exc)))

        t1 = threading.Thread(target=release, args=(Actor("v-a", "veterinarian"),))
        t2 = threading.Thread(target=release, args=(Actor("v-b", "veterinarian"),))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(self.service.get(order["id"])["status"], "released")
        self.assertEqual(len(results), 2)
        self.assertTrue(any(r[0] == "ok" for r in results))
        # The losing vet gets the latest state and blocking list.
        loser = next(r for r in results if r[0] == "conflict")
        self.assertEqual(loser[1], "released")

    # ------------------------------------------------------------------
    # Failure retention, retry and restart recovery
    # ------------------------------------------------------------------

    def test_failed_write_is_retained_and_retry_only_resumes_unfinished(self):
        sire = self._animal("M", "male")
        dam = self._animal("F", "female")
        pair = self._pair(sire, dam)

        # Let the durable planning transaction succeed, then fail the first
        # coordination write (the pairing invalidation).
        self.repo.arm_write_failure(sqlite3.OperationalError("disk I/O error"), after=6)
        order = self._quarantine(sire)
        # Order row exists; item is failed; pairing is untouched.
        self.assertEqual(order["status"], "blocked")
        self.assertEqual(self.service.get(pair["id"])["status"], "approved")
        failed = self.repo.list_items(
            quarantine_id=order["id"], states=("failed",)
        )
        self.assertEqual(len(failed), 1)
        self.assertIn("disk I/O error", failed[0]["last_error"])
        self.assertEqual(
            order["data"]["blockages"][0]["type"], "invalidate_pairing"
        )

        # Retry resumes only the unfinished item.
        retried = self.service.transition(VET, order["id"], "retry", {})
        self.assertEqual(retried["status"], "active")
        self.assertEqual(self.service.get(pair["id"])["status"], "invalidated")
        self.assertEqual(self.service.get(sire["id"])["status"], "quarantined")
        # Finished items are never re-applied.
        self.assertFalse(
            self.repo.list_items(quarantine_id=order["id"], states=("failed", "pending"))
        )

    def test_restart_resumes_unfinished_coordination(self):
        sire = self._animal("M", "male")
        dam = self._animal("F", "female")
        pair = self._pair(sire, dam)

        self.repo.arm_write_failure(sqlite3.OperationalError("simulated crash"), after=6)
        order = self._quarantine(sire)
        self.assertEqual(order["status"], "blocked")

        # Simulate a full service restart against the same database file.
        new_repo = SQLiteRepository(self.repo.path)
        new_service = DomainService(new_repo, RuleEngine())
        resumed = new_service.recover_on_startup()
        self.assertEqual(resumed, [order["id"]])
        order = new_service.get(order["id"])
        self.assertEqual(order["status"], "active")
        self.assertEqual(new_service.get(pair["id"])["status"], "invalidated")

    def test_restart_with_transfer_waiting_resumes_after_arrival(self):
        animal = self._animal()
        self._transfer(animal, state="in_transit")
        order = self._quarantine(animal)
        new_repo = SQLiteRepository(self.repo.path)
        new_service = DomainService(new_repo, RuleEngine())
        self.assertEqual(new_service.recover_on_startup(), [order["id"]])
        # Still blocked after restart (transfer still in transit).
        self.assertEqual(new_service.get(order["id"])["status"], "blocked")
        transfer_id = new_repo.list_entities(kind="transfer")[0]["id"]
        new_service.transition(REG, transfer_id, "arrive",
                               {"arrival_date": "2026-10-02"})
        self.assertEqual(new_service.get(order["id"])["status"], "active")

    def test_release_write_failure_is_recoverable(self):
        sire = self._animal("M", "male")
        dam = self._animal("F", "female")
        pair = self._pair(sire, dam)
        order = self._quarantine(sire)

        # The claim (lock + "releasing" + plan) commits; then a write fails
        # while the individual release items are being processed.
        with self._crash_in_release_phase():
            self.service.transition(VET, order["id"], "release", {})

        self.assertEqual(self.service.get(order["id"])["status"], "releasing")

        # A second vet cannot hijack the phase; retry resumes and finishes.
        with self.assertRaises(ConflictError):
            self.service.transition(
                Actor("vet-2", "veterinarian"), order["id"], "release", {}
            )
        retried = self.service.transition(VET, order["id"], "retry", {})
        self.assertEqual(retried["status"], "released")
        self.assertEqual(
            self.service.get(pair["id"])["status"], "reconfirm_required"
        )
        self.assertEqual(self.service.get(sire["id"])["status"], "active")

    def _crash_in_release_phase(self):
        """Context manager that fails a write while release items run.

        Item failures are retained (the item becomes "failed") rather than
        aborting the whole request, so no exception escapes the call.
        """
        class _Armed:
            def __init__(self, test):
                self.test = test

            def __enter__(self):
                # Claim writes (lock, order flip, audit, 2 plan items) pass;
                # the first release-item side effect then fails.
                self.test.repo.arm_write_failure(
                    sqlite3.OperationalError("crash during release"), after=5
                )
                return self

            def __exit__(self, exc_type, exc, tb):
                # The failed item is retained, not propagated.
                self.test.assertIsNone(exc_type)
                return False

        return _Armed(self)

    def test_restart_recovers_interrupted_release(self):
        sire = self._animal("M", "male")
        dam = self._animal("F", "female")
        pair = self._pair(sire, dam)
        order = self._quarantine(sire)

        with self._crash_in_release_phase():
            self.service.transition(VET, order["id"], "release", {})
        self.assertEqual(self.service.get(order["id"])["status"], "releasing")

        new_repo = SQLiteRepository(self.repo.path)
        new_service = DomainService(new_repo, RuleEngine())
        resumed = new_service.recover_on_startup()
        self.assertEqual(resumed, [order["id"]])
        self.assertEqual(new_service.get(order["id"])["status"], "released")
        self.assertEqual(
            new_service.get(pair["id"])["status"], "reconfirm_required"
        )

    def test_audit_trail_records_coordination(self):
        sire = self._animal("M", "male")
        dam = self._animal("F", "female")
        pair = self._pair(sire, dam)
        order = self._quarantine(sire)
        actions = {row["action"] for row in self.service.audit_log(pair["id"])}
        self.assertIn("invalidate", actions)
        order_actions = {row["action"] for row in self.service.audit_log(order["id"])}
        self.assertIn("submit", order_actions)
        self.assertIn("coordinate", order_actions)


if __name__ == "__main__":
    unittest.main()
