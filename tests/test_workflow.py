import tempfile
import unittest
from pathlib import Path

from src.domain import Actor
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def _resolve(value, created):
    if isinstance(value, str):
        for key, item in created.items():
            value = value.replace("{" + key + "}", str(item))
        return value
    if isinstance(value, list):
        return [_resolve(item, created) for item in value]
    if isinstance(value, dict):
        return {key: _resolve(item, created) for key, item in value.items()}
    return value


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_workflow(self):
        created = {}
        steps = [{'op': 'create', 'as': 'sire', 'kind': 'animal', 'data': {'name': 'M-1', 'sex': 'male'}}, {'op': 'create', 'as': 'dam', 'kind': 'animal', 'data': {'name': 'F-1', 'sex': 'female'}}, {'op': 'create', 'as': 'pairing', 'kind': 'pairing', 'data': {'proposed_by': 'coordinator'}}, {'op': 'transition', 'target': 'pairing', 'action': 'approve', 'data': {'sire_id': '{sire}', 'dam_id': '{dam}', 'approvals': ['vet-1']}, 'expect': 'approved'}, {'op': 'transition', 'target': 'pairing', 'action': 'complete', 'data': {'offspring_ids': ['offspring-1']}, 'expect': 'completed'}, {'op': 'create', 'as': 'transfer', 'kind': 'transfer', 'data': {'animal_id': '{sire}', 'from_institution': 'Zoo-A', 'to_institution': 'Zoo-B'}}, {'op': 'transition', 'target': 'transfer', 'action': 'authorize', 'data': {'permit_id': 'P-1'}, 'expect': 'authorized'}, {'op': 'transition', 'target': 'transfer', 'action': 'ship', 'data': {'transport_id': 'T-1'}, 'expect': 'in_transit'}, {'op': 'transition', 'target': 'transfer', 'action': 'arrive', 'data': {'arrival_date': '2026-05-01'}, 'expect': 'completed'}]
        for step in steps:
            if step["op"] == "create":
                entity = self.service.create(
                    self.actor,
                    step["kind"],
                    _resolve(step.get("data", {}), created),
                    step.get("idempotency_key"),
                )
                created[step["as"]] = entity["id"]
            else:
                entity = self.service.transition(
                    self.actor,
                    created[step["target"]],
                    step["action"],
                    _resolve(step.get("data", {}), created),
                    step.get("expected_version"),
                )
            if "expect" in step:
                self.assertEqual(entity["status"], step["expect"])


if __name__ == "__main__":
    unittest.main()
