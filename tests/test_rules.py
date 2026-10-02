import unittest

from src.rules import inbreeding_coefficient
from src.domain import Actor, PermissionDenied, ValidationError
from src.rules import RuleEngine


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = RuleEngine()
        self.admin = Actor("rule-tester", "admin")

    def test_rule_calculation_or_validation(self):
        self.assertEqual(inbreeding_coefficient({"id": "a"}, {"id": "a"}), 0.5)
        self.assertEqual(inbreeding_coefficient({"id": "a", "sire_id": "b"}, {"id": "b"}), 0.25)
        self.assertEqual(inbreeding_coefficient({"id": "a", "sire_id": "x"}, {"id": "b", "sire_id": "y"}), 0.0)
        with self.assertRaises(ValidationError):
            self.rules.validate_create(self.admin, "animals", {"name": "A", "sex": "other"})


if __name__ == "__main__":
    unittest.main()
