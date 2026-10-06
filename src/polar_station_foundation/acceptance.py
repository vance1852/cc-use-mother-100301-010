"""运行基础服务的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .allocations_service import AllocationService
from .service import DomainService
from .storage import Database


def run() -> dict[str, object]:
    """执行一条完整登记链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        service = AllocationService(database, FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)))
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范科考机构")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                               display_name="站务负责人", role="operator", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                              organization_id="org-001", name="一号科考站点", timezone_name="Asia/Shanghai")
        first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                           category="station_operator_profile", external_key="record-001",
                                           data={"name": "基础资料", "enabled": True})
        replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                            category="station_operator_profile", external_key="record-001",
                                            data={"name": "基础资料", "enabled": True})

        # 样品分配与出口许可：登记批次、许可、实验室、资质、MTA，提交并批准分装。
        service.register_batch(request_id="req-batch", actor_id="operator-001", batch_id="batch-001",
                               site_id="site-001", external_key="SED-001",
                               owner_organization_id="org-001",
                               custodian_organization_id="org-001",
                               total_quantity=100, unit="g",
                               collected_at="2026-01-01T00:00:00Z")
        permit_terms = {"allowed_uses": ["geochemistry"], "export_allowed": True,
                        "requires_return": True}
        service.register_permit_version(request_id="req-permit", actor_id="admin-001",
                                        permit_id="permit-001", batch_id="batch-001",
                                        terms=permit_terms)
        service.register_lab(request_id="req-lab", actor_id="admin-001", lab_id="lab-001",
                             organization_id="org-001", name="合作实验室", country_code="DE")
        service.register_qualification(request_id="req-qual", actor_id="admin-001",
                                       qualification_id="qual-001", lab_id="lab-001",
                                       scope="geochemistry",
                                       valid_from="2025-01-01T00:00:00Z",
                                       valid_until="2027-12-31T00:00:00Z")
        mta_terms = {"allowed_uses": ["geochemistry"], "max_consumption_fraction": 0.5,
                     "return_required": True, "return_by_days": 90}
        service.register_mta(request_id="req-mta", actor_id="admin-001", mta_id="mta-001",
                             provider_organization_id="org-001", recipient_lab_id="lab-001",
                             terms=mta_terms, effective_at="2026-01-01T00:00:00Z",
                             expires_at="2027-12-31T00:00:00Z")
        documents = {"export_permit": "EXP-1", "customs_declaration": "CUS-1",
                     "packing_list": "PKG-1"}
        applied = service.submit_application(
            request_id="req-application", actor_id="operator-001", batch_id="batch-001",
            lab_id="lab-001", intended_use="geochemistry", requested_quantity=30,
            planned_consumption=10, documents=documents,
        )
        approved = service.approve_application(request_id="req-approval", actor_id="admin-001",
                                               allocation_id=applied.resource_id)
        reconciliation = service.reconcile_batch("batch-001")
        report = service.get_allocation(applied.resource_id)
        valid, event_count = service.verify_audit()
        records = service.list_domain_data("site-001")
        result = {"status": "ok", "records": len(records), "audit_events": event_count,
                  "audit_valid": valid, "first_replayed": first.replayed,
                  "second_replayed": replay.replayed,
                  "allocation_id": applied.resource_id,
                  "allocation_approved": approved.replayed or not applied.replayed,
                  "allocation_balanced": reconciliation["balanced"],
                  "reserved_quantity": reconciliation["reserved"],
                  "available_quantity": reconciliation["allocatable"],
                  "rights_permit": report["rights"]["permit"]["permit_id"],
                  "rights_mta": report["rights"]["mta"]["mta_id"],
                  "all_checks_passed": report["rights"]["checks"]["all_passed"]}
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
