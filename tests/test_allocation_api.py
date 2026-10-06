"""样品分配与出口许可的 HTTP 路由测试。"""

import unittest

from polar_station_foundation.api import route

from allocation_fixture import DOCUMENTS, AllocationFixture


class AllocationApiTest(unittest.TestCase):
    def setUp(self):
        self.fixture = AllocationFixture()
        self.service = self.fixture.service

    def tearDown(self):
        self.fixture.close()

    def call(self, method, path, body=None, actor="op1"):
        return route(self.service, method, path, body or {},
                     {"X-Actor-Id": actor})

    def test_full_allocation_flow_over_http(self):
        status, body = self.call("POST", "/allocations/applications", {
            "request_id": "http-app", "batch_id": "b1", "lab_id": "lab-a",
            "intended_use": "geochemistry", "requested_quantity": 30,
            "planned_consumption": 10, "documents": DOCUMENTS,
        })
        self.assertEqual(201, status)
        allocation_id = body["resource_id"]

        status, body = self.call("POST", "/allocations/approve", {
            "request_id": "http-approve", "allocation_id": allocation_id,
        }, actor="rv1")
        self.assertEqual(201, status)

        status, body = self.call("GET", f"/allocations/{allocation_id}", actor="rv1")
        self.assertEqual(200, status)
        self.assertEqual("reserved", body["status"])
        self.assertEqual("P1", body["rights"]["permit"]["permit_id"])

        status, body = self.call("GET", "/batches/b1/reconcile")
        self.assertEqual(200, status)
        self.assertTrue(body["balanced"])
        self.assertEqual(30, body["reserved"])

    def test_failed_clause_returns_conflict_with_409(self):
        status, body = self.call("POST", "/allocations/applications", {
            "request_id": "http-bad", "batch_id": "b1", "lab_id": "lab-b",
            "intended_use": "geochemistry", "requested_quantity": 10,
            "planned_consumption": 0, "documents": {},
        })
        # 失败申请同样落库，HTTP 层视为已创建资源。
        self.assertEqual(201, status)
        status, body = self.call("POST", "/allocations/approve", {
            "request_id": "http-bad-approve", "allocation_id": body["resource_id"],
        }, actor="rv1")
        self.assertEqual(409, status)
        self.assertEqual("conflict", body["error"])

    def test_allocations_list_filter(self):
        status, body = self.call("GET", "/allocations?batch_id=b1")
        self.assertEqual(200, status)
        self.assertEqual([], body["items"])
        self.call("POST", "/allocations/applications", {
            "request_id": "http-list", "batch_id": "b1", "lab_id": "lab-a",
            "intended_use": "geochemistry", "requested_quantity": 5,
            "planned_consumption": 0, "documents": DOCUMENTS,
        })
        status, body = self.call("GET", "/allocations?batch_id=b1")
        self.assertEqual(200, status)
        self.assertEqual(1, len(body["items"]))

    def test_batch_report_endpoint(self):
        status, body = self.call("GET", "/batches/b1")
        self.assertEqual(200, status)
        self.assertEqual("SED-2026-001", body["external_key"])
        self.assertEqual(100.0, body["balances"]["available"])


if __name__ == "__main__":
    unittest.main()
