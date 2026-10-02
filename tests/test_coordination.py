import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from src.domain import Actor, ConflictError, CoordinationConflict, InvalidTransition
from src.http_api import create_handler
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def _actor(role="veterinarian", user="vet-1"):
    return Actor(user, role)


class QuarantineCoordinationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.vet = _actor()
        self.admin = _actor("admin", "admin-1")

    def tearDown(self):
        self.tmp.cleanup()

    def _animal(self, name="A", sex="male"):
        return self.service.create(self.admin, "animal", {"name": name, "sex": sex})

    def _pairing(self, sire, dam, status="proposed"):
        p = self.service.create(self.admin, "pairing", {"proposed_by": "coordinator"})
        if status == "approved":
            self.service.transition(
                self.admin, p["id"], "approve",
                {"sire_id": sire, "dam_id": dam, "approvals": ["vet-1"]},
            )
        return self.service.get(p["id"])

    def _transfer(self, animal, status="planned"):
        t = self.service.create(self.admin, "transfer", {
            "animal_id": animal, "from_institution": "Zoo-A", "to_institution": "Zoo-B",
        })
        if status in ("authorized", "in_transit", "completed"):
            self.service.transition(self.admin, t["id"], "authorize", {"permit_id": "P-1"})
        if status in ("in_transit", "completed"):
            self.service.transition(self.admin, t["id"], "ship", {"transport_id": "T-1"})
        if status == "completed":
            self.service.transition(self.admin, t["id"], "arrive", {"arrival_date": "2026-05-01"})
        return self.service.get(t["id"])

    def test_submit_no_blockers_activates_and_quarantines_animal(self):
        animal = self._animal()
        order = self.service.submit_quarantine(self.vet, animal["id"], "H5N1", "Q-A")
        self.assertEqual(order["status"], "active")
        self.assertEqual(order["data"]["reason"], "H5N1")
        self.assertEqual(order["data"]["location"], "Q-A")
        self.assertEqual(order["data"]["blockers"], [])
        # 个体状态同步为隔离中
        self.assertEqual(self.service.get(animal["id"])["status"], "quarantined")

    def test_submit_with_approved_pairing_invalidates_and_activates(self):
        sire = self._animal("M", "male")
        dam = self._animal("F", "female")
        pairing = self._pairing(sire["id"], dam["id"], status="approved")
        order = self.service.submit_quarantine(self.vet, sire["id"], "H5N1", "Q-A")
        self.assertEqual(order["status"], "active")
        # 受影响对象被列出
        kinds = [b["kind"] for b in order["data"]["blockers"]]
        self.assertIn("pairing", kinds)
        # 配对批准失效
        self.assertEqual(self.service.get(pairing["id"])["status"], "invalidated")
        # 清单记录已处理对象
        self.assertEqual(order["data"]["checklist"]["pairings"][pairing["id"]], "invalidated")

    def test_submit_with_in_transit_transfer_pauses_and_lists_blocker(self):
        animal = self._animal()
        transfer = self._transfer(animal["id"], status="in_transit")
        order = self.service.submit_quarantine(self.vet, animal["id"], "H5N1", "Q-A")
        self.assertEqual(order["status"], "paused")
        blockers = order["data"]["blockers"]
        self.assertTrue(any(b["kind"] == "transfer" and b["id"] == transfer["id"] for b in blockers))
        # 在途运输不得回退：运输仍在途中
        self.assertEqual(self.service.get(transfer["id"])["status"], "in_transit")
        # 个体未被隔离（还在运输）
        self.assertEqual(self.service.get(animal["id"])["status"], "active")

    def test_transfer_arrival_auto_resumes_paused_quarantine(self):
        animal = self._animal()
        transfer = self._transfer(animal["id"], status="in_transit")
        order = self.service.submit_quarantine(self.vet, animal["id"], "H5N1", "Q-A")
        self.assertEqual(order["status"], "paused")
        # 运输到达 -> 隔离自动恢复生效
        self.service.transition(self.admin, transfer["id"], "arrive", {"arrival_date": "2026-05-01"})
        resumed = self.service.get(order["id"])
        self.assertEqual(resumed["status"], "active")
        # 运输已完成，未被回退
        self.assertEqual(self.service.get(transfer["id"])["status"], "completed")
        self.assertEqual(self.service.get(animal["id"])["status"], "quarantined")

    def test_same_reason_returns_one_order_idempotent(self):
        animal = self._animal()
        first = self.service.submit_quarantine(self.vet, animal["id"], "H5N1", "Q-A")
        second = self.service.submit_quarantine(self.vet, animal["id"], "H5N1", "Q-B")
        self.assertEqual(first["id"], second["id"])
        # 场所仍以第一张为准（先到先生效）
        self.assertEqual(second["data"]["location"], "Q-A")

    def test_different_reason_creates_separate_orders(self):
        animal = self._animal()
        o1 = self.service.submit_quarantine(self.vet, animal["id"], "H5N1", "Q-A")
        o2 = self.service.submit_quarantine(self.vet, animal["id"], "H3N2", "Q-B")
        self.assertNotEqual(o1["id"], o2["id"])

    def test_release_reconfirms_invalidated_pairings(self):
        sire = self._animal("M", "male")
        dam = self._animal("F", "female")
        pairing = self._pairing(sire["id"], dam["id"], status="approved")
        order = self.service.submit_quarantine(self.vet, sire["id"], "H5N1", "Q-A")
        self.assertEqual(self.service.get(pairing["id"])["status"], "invalidated")
        # 解除隔离 -> 未完成配对重新确认（回到待批准）
        self.service.release_quarantine(self.vet, order["id"])
        self.assertEqual(self.service.get(pairing["id"])["status"], "proposed")
        self.assertEqual(self.service.get(order["id"])["status"], "released")
        self.assertEqual(self.service.get(sire["id"])["status"], "active")

    def test_cannot_release_paused_quarantine(self):
        animal = self._animal()
        self._transfer(animal["id"], status="in_transit")
        order = self.service.submit_quarantine(self.vet, animal["id"], "H5N1", "Q-A")
        self.assertEqual(order["status"], "paused")
        with self.assertRaises(InvalidTransition):
            self.service.release_quarantine(self.vet, order["id"])

    def test_concurrent_release_first_writer_wins_loser_gets_latest_state(self):
        animal = self._animal()
        order = self.service.submit_quarantine(self.vet, animal["id"], "H5N1", "Q-A")
        self.assertEqual(order["status"], "active")
        v1 = order["version"]
        # 两人同时提交解除：先到先生效
        self.service.release_quarantine(self.vet, order["id"], expected_version=v1)
        # 败者拿到最新状态（含失效原因/阻塞清单）
        with self.assertRaises(CoordinationConflict) as ctx:
            self.service.release_quarantine(self.vet, order["id"], expected_version=v1)
        details = ctx.exception.details
        self.assertEqual(details["status"], "released")
        self.assertIn("reconfirmed_pairings", details["data"])

    def test_concurrent_resume_loser_gets_latest_blockers(self):
        animal = self._animal()
        transfer = self._transfer(animal["id"], status="in_transit")
        order = self.service.submit_quarantine(self.vet, animal["id"], "H5N1", "Q-A")
        v1 = order["version"]
        # 两次并发恢复：第一次仍被在途运输阻塞（更新阻塞清单），第二次拿到最新清单
        self.service.resume_quarantine(self.vet, order["id"], expected_version=v1)
        with self.assertRaises(CoordinationConflict) as ctx:
            self.service.resume_quarantine(self.vet, order["id"], expected_version=v1)
        details = ctx.exception.details
        self.assertEqual(details["status"], "paused")
        self.assertTrue(any(b["id"] == transfer["id"] for b in details["data"]["blockers"]))

    def test_recover_paused_resumes_after_restart(self):
        animal = self._animal()
        transfer = self._transfer(animal["id"], status="in_transit")
        order = self.service.submit_quarantine(self.vet, animal["id"], "H5N1", "Q-A")
        self.assertEqual(order["status"], "paused")
        # 模拟重启：新服务实例、同一数据库文件。重启期间运输到达并落库
        # （绕过服务动作，因此不会触发自动恢复，只能靠 recover_paused 兜底）。
        restarted = DomainService(SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine())
        t = restarted.repository.get_entity(transfer["id"])
        restarted.repository.update_entity(transfer["id"], None, "completed", dict(t["data"]))
        recovered = restarted.recover_paused()
        self.assertTrue(any(r["id"] == order["id"] and r["status"] == "active" for r in recovered))
        self.assertEqual(restarted.get(order["id"])["status"], "active")
        # 运输未被回退
        self.assertEqual(restarted.get(transfer["id"])["status"], "completed")

    def test_retry_only_touches_unfinished_objects(self):
        sire = self._animal("M", "male")
        dam = self._animal("F", "female")
        p1 = self._pairing(sire["id"], dam["id"], status="approved")
        p2 = self._pairing(sire["id"], dam["id"], status="approved")
        order = self.service.submit_quarantine(self.vet, sire["id"], "H5N1", "Q-A")
        # 两张批准都已失效
        self.assertEqual(self.service.get(p1["id"])["status"], "invalidated")
        self.assertEqual(self.service.get(p2["id"])["status"], "invalidated")
        # 重试只续做未完成对象：已失效的不再处理（幂等）
        self.assertFalse(self.service._invalidate_pairing(p1["id"], self.vet))
        self.assertFalse(self.service._invalidate_pairing(p2["id"], self.vet))
        # 解除后重新确认也只做一次（重试不再重复处理）
        self.service.release_quarantine(self.vet, order["id"])
        self.assertFalse(self.service._reconfirm_pairing(p1["id"], self.vet))
        self.assertFalse(self.service._reconfirm_pairing(p2["id"], self.vet))
        # 清单持久记录了每对象的处理结果
        self.assertEqual(order["data"]["checklist"]["pairings"][p1["id"]], "invalidated")

    def test_approve_blocked_while_quarantined(self):
        sire = self._animal("M", "male")
        dam = self._animal("F", "female")
        order = self.service.submit_quarantine(self.vet, sire["id"], "H5N1", "Q-A")
        self.assertEqual(order["status"], "active")
        # 隔离中的个体不能批准配对
        with self.assertRaises(Exception):
            self.service.transition(
                self.admin,
                self.service.create(self.admin, "pairing", {"proposed_by": "coordinator"})["id"],
                "approve",
                {"sire_id": sire["id"], "dam_id": dam["id"], "approvals": ["vet-1"]},
            )

    def test_permission_denied_for_viewer(self):
        animal = self._animal()
        with self.assertRaises(Exception):
            self.service.submit_quarantine(Actor("v", "viewer"), animal["id"], "H5N1", "Q-A")


class QuarantineHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin-1", "admin")
        handler = create_handler(self.service, RuleEngine(), str(Path(__file__).resolve().parent.parent / "static"))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def _post(self, path, body, headers=None):
        data = json.dumps(body).encode("utf-8")
        req = Request(self._url(path), data=data, method="POST", headers=headers or {})
        try:
            with urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _get(self, path):
        with urlopen(self._url(path)) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def test_http_quarantine_flow(self):
        headers = {"Content-Type": "application/json", "X-User-Id": "vet-1", "X-Role": "veterinarian"}
        animal = self.service.create(self.admin, "animal", {"name": "A", "sex": "male"})
        status, order = self._post("/api/quarantines", {
            "animal_id": animal["id"], "reason": "H5N1", "location": "Q-A",
        }, headers)
        self.assertEqual(status, 201)
        self.assertEqual(order["status"], "active")
        # 列表可查
        _, listing = self._get("/api/quarantines")
        self.assertTrue(any(q["id"] == order["id"] for q in listing["items"]))
        # 解除
        status, released = self._post(
            "/api/quarantines/%s/release" % order["id"],
            {"expected_version": order["version"]}, headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(released["status"], "released")

    def test_http_conflict_returns_latest_state(self):
        headers = {"Content-Type": "application/json", "X-User-Id": "vet-1", "X-Role": "veterinarian"}
        animal = self.service.create(self.admin, "animal", {"name": "A", "sex": "male"})
        status, order = self._post("/api/quarantines", {
            "animal_id": animal["id"], "reason": "H5N1", "location": "Q-A",
        }, headers)
        self.assertEqual(status, 201)
        # 第一次解除成功
        self._post("/api/quarantines/%s/release" % order["id"],
                   {"expected_version": order["version"]}, headers)
        # 第二次用旧版本 -> 409，且带最新状态
        status, body = self._post("/api/quarantines/%s/release" % order["id"],
                                  {"expected_version": order["version"]}, headers)
        self.assertEqual(status, 409)
        self.assertIn("details", body)
        self.assertEqual(body["details"]["status"], "released")

    def test_http_validation_error_shows_reason(self):
        headers = {"Content-Type": "application/json", "X-User-Id": "vet-1", "X-Role": "veterinarian"}
        status, body = self._post("/api/quarantines", {
            "animal_id": "nonexistent", "reason": "H5N1", "location": "Q-A",
        }, headers)
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_health(self):
        status, body = self._get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")


if __name__ == "__main__":
    unittest.main()
