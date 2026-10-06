"""验证并发确认下的原子占用与超分配防护。"""

import threading
import unittest

from polar_station_foundation.errors import DomainError

from allocation_fixture import DOCUMENTS, AllocationFixture


class ConcurrentApprovalTest(unittest.TestCase):
    def setUp(self):
        self.fixture = AllocationFixture(total_quantity=100.0)
        self.service = self.fixture.service

    def tearDown(self):
        self.fixture.close()

    def test_concurrent_approvals_never_over_allocate(self):
        # 10 份各 15g 的申请，总需求 150g > 批次 100g，最多只能批准 6 份。
        allocation_ids = []
        for index in range(10):
            receipt = self.service.submit_application(
                request_id=f"conc-app-{index:02d}", actor_id="op1", batch_id="b1",
                lab_id="lab-a", intended_use="geochemistry",
                requested_quantity=15, planned_consumption=0, documents=DOCUMENTS,
            )
            allocation_ids.append(receipt.resource_id)

        outcomes: list[tuple[str, int]] = []
        outcomes_lock = threading.Lock()

        def approve(index: int) -> None:
            try:
                self.service.approve_application(
                    request_id=f"conc-apr-{index:02d}", actor_id="rv1",
                    allocation_id=allocation_ids[index],
                )
                result = ("approved", index)
            except DomainError:
                result = ("blocked", index)
            with outcomes_lock:
                outcomes.append(result)

        threads = [threading.Thread(target=approve, args=(i,)) for i in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        approved = sum(1 for result, _ in outcomes if result == "approved")
        self.assertEqual(6, approved)
        reconciliation = self.service.reconcile_batch("b1")
        self.assertTrue(reconciliation["balanced"])
        self.assertEqual(90.0, reconciliation["reserved"])
        self.assertEqual(10.0, reconciliation["allocatable"])
        self.assertEqual(100.0, reconciliation["initial_total_quantity"])


if __name__ == "__main__":
    unittest.main()
