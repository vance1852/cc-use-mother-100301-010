"""样品分配与出口许可核心规则测试。"""

import json
import unittest

from polar_station_foundation.errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)

from allocation_fixture import DOCUMENTS, AllocationFixture


class AllocationRulesTest(unittest.TestCase):
    def setUp(self):
        self.fixture = AllocationFixture()
        self.service = self.fixture.service

    def tearDown(self):
        self.fixture.close()

    def apply(self, request_id="app-001", *, lab="lab-a", use="geochemistry",
              requested=30.0, planned=10.0, documents=None):
        receipt = self.service.submit_application(
            request_id=request_id, actor_id="op1", batch_id="b1", lab_id=lab,
            intended_use=use, requested_quantity=requested,
            planned_consumption=planned,
            documents=DOCUMENTS if documents is None else documents,
        )
        return receipt.resource_id

    def approve(self, allocation_id, request_id="appr-001"):
        return self.service.approve_application(
            request_id=request_id, actor_id="rv1", allocation_id=allocation_id
        )

    # ------------------------------------------------------------------
    # 逐项硬性条款
    # ------------------------------------------------------------------

    def test_each_hard_clause_is_recorded_separately(self):
        allocation_id = self.apply("app-record")
        report = self.service.get_allocation(allocation_id)
        results = report["rights"]["checks"]["results"]
        self.assertEqual(
            {
                "ownership_custody", "permit_version", "use_restriction",
                "lab_qualification", "mta", "consumption_budget",
                "return_agreement", "shipping_documents",
            },
            set(results),
        )
        self.assertTrue(all(item["passed"] for item in results.values()))

    def test_missing_shipping_document_fails_only_that_clause(self):
        documents = {"export_permit": "EX", "customs_declaration": "CD"}
        allocation_id = self.apply("app-docs", documents=documents)
        results = self.service.get_allocation(allocation_id)["rights"]["checks"]["results"]
        self.assertFalse(results["shipping_documents"]["passed"])
        self.assertIn("packing_list", results["shipping_documents"]["reason"])
        self.assertTrue(results["ownership_custody"]["passed"])
        with self.assertRaises(ConflictError):
            self.approve(allocation_id)

    def test_use_outside_permit_scope_is_rejected(self):
        # microbiology 虽在许可范围内，但 MTA 只允许 geochemistry。
        allocation_id = self.apply("app-use", use="microbiology")
        results = self.service.get_allocation(allocation_id)["rights"]["checks"]["results"]
        self.assertTrue(results["permit_version"]["passed"])
        self.assertFalse(results["use_restriction"]["passed"])
        self.assertTrue(results["mta"]["passed"])  # MTA 本身有效，是用途与其范围不符
        with self.assertRaises(ConflictError):
            self.approve(allocation_id)

    def test_use_completely_outside_permit_is_rejected(self):
        allocation_id = self.apply("app-use-x", use="radiometric_dating")
        results = self.service.get_allocation(allocation_id)["rights"]["checks"]["results"]
        self.assertFalse(results["use_restriction"]["passed"])
        with self.assertRaises(ConflictError):
            self.approve(allocation_id)

    def test_lab_without_qualification_or_mta_is_rejected(self):
        allocation_id = self.apply("app-lab-b", lab="lab-b")
        results = self.service.get_allocation(allocation_id)["rights"]["checks"]["results"]
        self.assertFalse(results["lab_qualification"]["passed"])
        self.assertFalse(results["mta"]["passed"])
        with self.assertRaises(ConflictError):
            self.approve(allocation_id)

    def test_consumption_plan_over_agreement_fraction_is_rejected(self):
        # 申请 30g，MTA 最多消耗 50%，计划 20g 超标。
        allocation_id = self.apply("app-budget", requested=30, planned=20)
        results = self.service.get_allocation(allocation_id)["rights"]["checks"]["results"]
        self.assertFalse(results["consumption_budget"]["passed"])
        with self.assertRaises(ConflictError):
            self.approve(allocation_id)

    def test_expired_or_revoked_qualification_blocks_approval(self):
        allocation_id = self.apply("app-expiry")
        # 提交时资质有效；批准前撤销，模拟提交后条件恶化。
        self.service.revoke_qualification(request_id="revoke-q", actor_id="rv1",
                                         qualification_id="q-a")
        with self.assertRaises(ConflictError):
            self.approve(allocation_id)
        # 未签发，数量没有被占用。
        balances = self.service.reconcile_batch("b1")["balances"]
        self.assertEqual(100.0, balances["available"])

    # ------------------------------------------------------------------
    # 原子占用与超分配
    # ------------------------------------------------------------------

    def test_quantities_move_only_after_approval(self):
        before = self.service.reconcile_batch("b1")["balances"]
        self.assertEqual(100.0, before["available"])
        allocation_id = self.apply("app-hold")
        pending = self.service.reconcile_batch("b1")["balances"]
        self.assertEqual(100.0, pending["available"])
        self.assertEqual(0.0, pending["reserved"])
        self.approve(allocation_id)
        after = self.service.reconcile_batch("b1")["balances"]
        self.assertEqual(70.0, after["available"])
        self.assertEqual(30.0, after["reserved"])

    def test_over_allocation_is_blocked_at_approval(self):
        first = self.apply("app-first", requested=60, planned=0)
        self.approve(first, "appr-first")
        second = self.apply("app-second", requested=60, planned=0)
        with self.assertRaises(ConflictError):
            self.approve(second, "appr-second")
        balances = self.service.reconcile_batch("b1")["balances"]
        self.assertEqual(40.0, balances["available"])
        self.assertEqual(60.0, balances["reserved"])

    def test_duplicate_approval_request_replays_without_double_occupancy(self):
        allocation_id = self.apply("app-dup")
        first = self.approve(allocation_id, "appr-dup")
        replay = self.approve(allocation_id, "appr-dup")
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        balances = self.service.reconcile_batch("b1")["balances"]
        self.assertEqual(30.0, balances["reserved"])

    def test_second_request_id_cannot_approve_same_allocation(self):
        allocation_id = self.apply("app-one")
        self.approve(allocation_id, "appr-one")
        with self.assertRaises(ConflictError):
            self.approve(allocation_id, "appr-two")

    # ------------------------------------------------------------------
    # 部分发运、海关、退件
    # ------------------------------------------------------------------

    def _deliver(self, allocation_id, quantity, request_prefix):
        shipment = self.service.ship_shipment(
            request_id=f"{request_prefix}-ship", actor_id="op1",
            allocation_id=allocation_id, quantity=quantity,
            carrier_ref=f"CARRIER-{request_prefix}", documents=DOCUMENTS,
        )
        shipment_id = shipment.resource_id
        self.service.shipment_event(request_id=f"{request_prefix}-hold",
                                    actor_id="op1", shipment_id=shipment_id,
                                    event="customs_hold")
        self.service.shipment_event(request_id=f"{request_prefix}-release",
                                    actor_id="op1", shipment_id=shipment_id,
                                    event="customs_release")
        self.service.shipment_event(request_id=f"{request_prefix}-deliver",
                                    actor_id="op1", shipment_id=shipment_id,
                                    event="deliver")
        return shipment_id

    def test_partial_shipment_and_customs_lifecycle(self):
        allocation_id = self.apply("app-flow")
        self.approve(allocation_id, "appr-flow")
        shipment_id = self._deliver(allocation_id, 20, "flow")
        report = self.service.get_allocation(allocation_id)
        self.assertEqual("delivered", report["status"])
        self.assertEqual(20.0, report["balances"]["at_lab"])
        self.assertEqual(10.0, report["balances"]["reserved"])
        # 海关状态事件幂等重放。
        replay = self.service.shipment_event(
            request_id="flow-hold", actor_id="op1", shipment_id=shipment_id,
            event="customs_hold",
        )
        self.assertTrue(replay.replayed)

    def test_customs_returned_goods_become_returned_balance(self):
        allocation_id = self.apply("app-ret")
        self.approve(allocation_id, "appr-ret")
        shipment = self.service.ship_shipment(
            request_id="ret-ship", actor_id="op1", allocation_id=allocation_id,
            quantity=30, carrier_ref="CARRIER-RET", documents=DOCUMENTS,
        )
        self.service.shipment_event(request_id="ret-back", actor_id="op1",
                                    shipment_id=shipment.resource_id,
                                    event="customs_returned", note="文件不全")
        balances = self.service.reconcile_batch("b1")["balances"]
        self.assertEqual(30.0, balances["returned"])
        self.assertEqual(70.0, balances["available"])
        # 重新入库后可再分配。
        self.service.restock_returned(request_id="ret-restock", actor_id="op1",
                                      allocation_id=allocation_id, quantity=30)
        balances = self.service.reconcile_batch("b1")["balances"]
        self.assertEqual(100.0, balances["available"])

    def test_goods_can_be_returned_directly_from_customs_hold(self):
        allocation_id = self.apply("app-hold-ret")
        self.approve(allocation_id, "appr-hold-ret")
        shipment = self.service.ship_shipment(
            request_id="holdret-ship", actor_id="op1",
            allocation_id=allocation_id, quantity=30,
            carrier_ref="CARRIER-HR", documents=DOCUMENTS,
        )
        self.service.shipment_event(request_id="holdret-hold", actor_id="op1",
                                    shipment_id=shipment.resource_id,
                                    event="customs_hold")
        # 无需先放行，海关可直接退运。
        self.service.shipment_event(request_id="holdret-back", actor_id="op1",
                                    shipment_id=shipment.resource_id,
                                    event="customs_returned", note="扣留后直接退运")
        balances = self.service.reconcile_batch("b1")["balances"]
        self.assertEqual(30.0, balances["returned"])
        report = self.service.get_allocation(allocation_id)
        self.assertEqual("returned_pending", report["status"])

    def test_cannot_ship_more_than_reserved(self):
        allocation_id = self.apply("app-ship-limit")
        self.approve(allocation_id, "appr-ship-limit")
        with self.assertRaises(ConflictError):
            self.service.ship_shipment(
                request_id="ship-over", actor_id="op1",
                allocation_id=allocation_id, quantity=31,
                carrier_ref="X", documents=DOCUMENTS,
            )

    # ------------------------------------------------------------------
    # 消耗预算与发表
    # ------------------------------------------------------------------

    def test_consumption_cannot_exceed_budget(self):
        allocation_id = self.apply("app-cons")
        self.approve(allocation_id, "appr-cons")
        self._deliver(allocation_id, 30, "cons")
        self.service.record_consumption(request_id="consume-ok", actor_id="rv1",
                                        allocation_id=allocation_id, quantity=10)
        with self.assertRaises(ConflictError):
            self.service.record_consumption(
                request_id="consume-bad", actor_id="rv1",
                allocation_id=allocation_id, quantity=1,
            )
        report = self.service.get_allocation(allocation_id)
        self.assertEqual(0.0, report["obligations"]["consumption_remaining"])

    def test_publication_preserves_authorization_basis_after_permit_change(self):
        allocation_id = self.apply("app-pub")
        self.approve(allocation_id, "appr-pub")
        self._deliver(allocation_id, 30, "pub")
        self.service.record_consumption(request_id="pub-consume", actor_id="rv1",
                                        allocation_id=allocation_id, quantity=10)
        self.service.register_publication(
            request_id="publication-1", actor_id="rv1",
            allocation_id=allocation_id, reference="2027 极地沉积物论文",
            consumed_quantity=10,
        )
        new_terms = {
            "allowed_uses": ["microbiology"],
            "export_allowed": True,
            "requires_return": True,
        }
        self.service.register_permit_version(request_id="permit-v2", actor_id="a1",
                                             permit_id="P1", batch_id="b1",
                                             terms=new_terms)
        report = self.service.get_allocation(allocation_id)
        self.assertEqual("superseded", report["rights"]["permit"]["status_now"])
        publication = report["publications"][0]
        self.assertEqual(1, publication["authorization_snapshot"]["permit"]["version"])
        self.assertEqual(
            ["geochemistry", "microbiology"],
            publication["authorization_snapshot"]["permit"]["terms"]["allowed_uses"],
        )
        self.assertTrue(
            publication["authorization_snapshot"]["approval_checks"]["all_passed"]
        )

    # ------------------------------------------------------------------
    # 许可版本化、撤回与实验室退出
    # ------------------------------------------------------------------

    def test_new_permit_version_supersedes_without_tampering(self):
        new_terms = {
            "allowed_uses": ["geochemistry"],
            "export_allowed": True,
            "requires_return": False,
        }
        self.service.register_permit_version(request_id="permit-v2", actor_id="a1",
                                             permit_id="P1", batch_id="b1",
                                             terms=new_terms)
        allocation_id = self.apply("app-v2")
        results = self.service.get_allocation(allocation_id)["rights"]["checks"]["results"]
        # 新许可不要求返还，返还条款以豁免通过。
        self.assertTrue(results["return_agreement"]["passed"])
        self.assertFalse(results["return_agreement"]["evidence"]["return_required"])
        # 旧版本行仍然存在且未被篡改。
        old = self.service.database.connection.execute(
            "SELECT status, terms_json FROM permits WHERE permit_id='P1' AND version=1"
        ).fetchone()
        self.assertEqual("superseded", old["status"])
        self.assertIn("microbiology", old["terms_json"])

    def test_permit_withdrawal_freezes_stock_and_blocks_consumption(self):
        allocation_id = self.apply("app-wd")
        self.approve(allocation_id, "appr-wd")
        self._deliver(allocation_id, 20, "wd")
        self.service.record_consumption(request_id="wd-consume", actor_id="rv1",
                                        allocation_id=allocation_id, quantity=5)
        self.service.withdraw_permit(request_id="withdraw-p1", actor_id="a1",
                                     permit_id="P1", reason="监管暂停")
        balances = self.service.reconcile_batch("b1")["balances"]
        # 在实验室的 15g 物理无法冻结；站内 70 可用 + 10 预留被冻结。
        self.assertEqual(80.0, balances["frozen"])
        self.assertEqual(15.0, balances["at_lab"])
        with self.assertRaises(ConflictError):
            self.service.record_consumption(
                request_id="wd-consume-2", actor_id="rv1",
                allocation_id=allocation_id, quantity=1,
            )
        # 已消耗 5g 不回滚，历史发表依据保留。
        self.assertEqual(5.0, balances["consumed"])

    def test_returned_during_withdrawal_is_frozen_until_reauthorization(self):
        allocation_id = self.apply("app-wdr")
        self.approve(allocation_id, "appr-wdr")
        self._deliver(allocation_id, 30, "wdr")
        self.service.withdraw_permit(request_id="wdr-p", actor_id="a1",
                                     permit_id="P1", reason="暂停")
        ret = self.service.register_return_shipment(
            request_id="wdr-return", actor_id="op1",
            allocation_id=allocation_id, quantity=30, carrier_ref="BACK",
        )
        self.service.shipment_event(request_id="wdr-received", actor_id="op1",
                                    shipment_id=ret.resource_id,
                                    event="return_received")
        balances = self.service.reconcile_batch("b1")["balances"]
        self.assertEqual(100.0, balances["frozen"])
        self.assertEqual(0.0, balances["available"])
        terms = {
            "allowed_uses": ["geochemistry"],
            "export_allowed": True,
            "requires_return": True,
        }
        self.service.register_permit_version(request_id="wdr-v3", actor_id="a1",
                                             permit_id="P1", batch_id="b1", terms=terms)
        balances = self.service.reconcile_batch("b1")["balances"]
        self.assertEqual(100.0, balances["available"])
        self.assertEqual(0.0, balances["frozen"])

    def test_lab_withdrawal_blocks_consumption_and_new_shipments(self):
        allocation_id = self.apply("app-lw")
        self.approve(allocation_id, "appr-lw")
        self._deliver(allocation_id, 20, "lw")
        self.service.withdraw_lab(request_id="lab-withdraw", actor_id="a1",
                                  lab_id="lab-a", reason="合作终止")
        with self.assertRaises(ConflictError):
            self.service.record_consumption(
                request_id="lw-consume", actor_id="rv1",
                allocation_id=allocation_id, quantity=1,
            )
        # 退出后新申请仍会留痕，但实验室资质条款失败，不得签发。
        new_id = self.apply("app-lw-new", lab="lab-a")
        results = self.service.get_allocation(new_id)["rights"]["checks"]["results"]
        self.assertFalse(results["lab_qualification"]["passed"])
        with self.assertRaises(ConflictError):
            self.approve(new_id, "appr-lw-new")

    # ------------------------------------------------------------------
    # 权利来源、责任方、对账
    # ------------------------------------------------------------------

    def test_lab_withdrawal_releases_never_shipped_reserved_stock(self):
        allocation_id = self.apply("app-lw-release")
        self.approve(allocation_id, "appr-lw-release")
        self.assertEqual(30.0, self.service.reconcile_batch("b1")["reserved"])
        self.service.withdraw_lab(request_id="lab-release", actor_id="a1",
                                  lab_id="lab-a", reason="合作终止")
        balances = self.service.reconcile_batch("b1")["balances"]
        # 从未发运的 30g 预留释放回可分配池，许可对其他方仍有效。
        self.assertEqual(0.0, balances["reserved"])
        self.assertEqual(100.0, balances["available"])
        report = self.service.get_allocation(allocation_id)
        self.assertTrue(report["obligations"]["lab_withdrawn"])

    def test_report_states_rights_responsibility_and_obligations(self):
        allocation_id = self.apply("app-report")
        self.approve(allocation_id, "appr-report")
        self._deliver(allocation_id, 30, "report")
        report = self.service.get_allocation(allocation_id)
        self.assertEqual("o1", report["rights"]["owner_organization_id"])
        self.assertEqual("o1", report["rights"]["custodian_organization_id"])
        self.assertEqual("P1", report["rights"]["permit"]["permit_id"])
        self.assertEqual(1, report["rights"]["permit"]["version"])
        self.assertEqual("M1", report["rights"]["mta"]["mta_id"])
        holder = next(item for item in report["responsibilities"]
                      if item["bucket"] == "at_lab")
        self.assertEqual("lab-a", holder["responsible_party"])
        self.assertTrue(report["obligations"]["return_required"])
        self.assertEqual(30.0, report["obligations"]["pending_return_quantity"])

    def test_batch_conservation_always_holds(self):
        allocation_id = self.apply("app-consv")
        self.approve(allocation_id, "appr-consv")
        reconciliation = self.service.reconcile_batch("b1")
        self.assertTrue(reconciliation["balanced"])
        self.assertEqual(0.0, reconciliation["conservation_delta"])

    def test_unknown_batch_and_allocation_raise_not_found(self):
        with self.assertRaises(NotFoundError):
            self.service.reconcile_batch("missing")
        with self.assertRaises(NotFoundError):
            self.service.get_allocation("missing")

    def test_operator_cannot_approve(self):
        allocation_id = self.apply("app-perm")
        with self.assertRaises(PermissionDenied):
            self.service.approve_application(
                request_id="appr-perm-bad", actor_id="op1",
                allocation_id=allocation_id,
            )

    def test_invalid_quantity_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.apply("app-badq", requested=-1, planned=0)


if __name__ == "__main__":
    unittest.main()
