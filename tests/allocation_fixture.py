"""为样品分配测试搭建共用的机构、批次、许可、实验室、资质与 MTA 事实。"""

from __future__ import annotations

from datetime import datetime, timezone

from polar_station_foundation.allocations_service import AllocationService
from polar_station_foundation.storage import Database

CLOCK = datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)

PERMIT_TERMS = {
    "allowed_uses": ["geochemistry", "microbiology"],
    "export_allowed": True,
    "requires_return": True,
}
MTA_TERMS = {
    "allowed_uses": ["geochemistry"],
    "max_consumption_fraction": 0.5,
    "return_required": True,
    "return_by_days": 90,
}
DOCUMENTS = {
    "export_permit": "EX-1",
    "customs_declaration": "CD-1",
    "packing_list": "PL-1",
}


class AllocationFixture:
    """构造一套可直接提交申请的最小事实集合。"""

    def __init__(self, total_quantity: float = 100.0) -> None:
        self.database = Database()
        self.service = AllocationService(self.database, _clock(CLOCK))
        self.service.register_organization(request_id="org-001", actor_id="bootstrap",
                                           organization_id="o1", name="极地科考中心")
        self.service.register_actor(request_id="actor-admin", actor_id="bootstrap",
                                    new_actor_id="a1", display_name="管理员",
                                    role="admin", organization_id="o1")
        self.service.register_actor(request_id="actor-operator", actor_id="a1",
                                    new_actor_id="op1", display_name="操作员",
                                    role="operator", organization_id="o1")
        self.service.register_actor(request_id="actor-reviewer", actor_id="a1",
                                    new_actor_id="rv1", display_name="审查员",
                                    role="reviewer", organization_id="o1")
        self.service.register_site(request_id="site-001", actor_id="op1", site_id="s1",
                                   organization_id="o1", name="一号站", timezone_name="UTC")
        self.service.register_batch(request_id="batch-001", actor_id="op1", batch_id="b1",
                                    site_id="s1", external_key="SED-2026-001",
                                    owner_organization_id="o1",
                                    custodian_organization_id="o1",
                                    total_quantity=total_quantity, unit="g",
                                    collected_at="2026-01-01T00:00:00Z")
        self.service.register_permit_version(request_id="permit-v1", actor_id="rv1",
                                             permit_id="P1", batch_id="b1",
                                             terms=dict(PERMIT_TERMS))
        self.service.register_lab(request_id="lab-alpha", actor_id="a1", lab_id="lab-a",
                                  organization_id="o1", name="阿尔法实验室",
                                  country_code="DE")
        self.service.register_lab(request_id="lab-beta", actor_id="a1", lab_id="lab-b",
                                  organization_id="o1", name="贝塔实验室",
                                  country_code="JP")
        self.service.register_qualification(request_id="qual-alpha", actor_id="rv1",
                                            qualification_id="q-a", lab_id="lab-a",
                                            scope="geochemistry",
                                            valid_from="2025-01-01T00:00:00Z",
                                            valid_until="2027-12-31T00:00:00Z")
        self.service.register_mta(request_id="mta-alpha", actor_id="rv1", mta_id="M1",
                                  provider_organization_id="o1", recipient_lab_id="lab-a",
                                  terms=dict(MTA_TERMS),
                                  effective_at="2026-01-01T00:00:00Z",
                                  expires_at="2027-12-31T00:00:00Z")

    def close(self) -> None:
        self.database.close()


def _clock(value):
    from polar_station_foundation.clock import FixedClock

    return FixedClock(value)
