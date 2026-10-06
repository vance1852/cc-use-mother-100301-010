"""样品分配与出口许可的硬性条款、量桶与规则引擎。

每项条款独立评估、分别记录；任一条款未通过时，服务层不得签发分装。
数量只在以下量桶之间做等额转移，因而任意时刻都能核对总量守恒：

- available：可分配（站内可再分配库存）
- reserved：已批准占用但尚未发运
- in_transit：在途（含海关扣留，责任方另由运输记录说明）
- at_lab：实验室持有、尚未消耗（待返还）
- consumed：已消耗
- returned：退件或返还到站、等待处置
- frozen：许可撤回后冻结、不得再分配的站内份额
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any

#: 数量比对使用的容差，避免浮点尾差误判。
EPS = 1e-9

#: 每份申请必须逐项通过的硬性条款。
CHECK_CARDS: "OrderedDict[str, dict[str, Any]]" = OrderedDict(
    ownership_custody={"label": "采集批次所有权与保管权", "mandatory": True},
    permit_version={"label": "采样许可条款版本", "mandatory": True},
    use_restriction={"label": "用途限制", "mandatory": True},
    lab_qualification={"label": "实验室资质", "mandatory": True},
    mta={"label": "材料转移协议", "mandatory": True},
    consumption_budget={"label": "消耗预算", "mandatory": True},
    return_agreement={"label": "返还约定", "mandatory": True},
    shipping_documents={"label": "运输文件", "mandatory": True},
)
CHECK_CODES = frozenset(CHECK_CARDS)

#: 发运审查阶段必须齐备的出口运输文件类型。
REQUIRED_SHIPPING_DOCS = ("export_permit", "customs_declaration", "packing_list")

#: 实际参与总量守恒的量桶（'' 仅用于期初登记的来源桶）。
REAL_BUCKETS = ("available", "reserved", "in_transit", "at_lab",
                "consumed", "returned", "frozen")


def _passed(evidence: dict[str, Any]) -> tuple[bool, dict[str, Any], str]:
    return True, evidence, ""


def _failed(reason: str, evidence: dict[str, Any] | None = None) -> tuple[bool, dict[str, Any], str]:
    return False, evidence or {}, reason


def _check_ownership_custody(ctx: dict[str, Any]) -> tuple[bool, dict[str, Any], str]:
    batch = ctx.get("batch")
    if not batch:
        return _failed("采集批次不存在")
    owner = batch.get("owner_organization_id")
    custodian = batch.get("custodian_organization_id")
    if not owner or not custodian:
        return _failed("批次缺少所有权方或保管方记录")
    return _passed({
        "batch_id": batch["batch_id"],
        "external_key": batch["external_key"],
        "owner_organization_id": owner,
        "custodian_organization_id": custodian,
    })


def _check_permit_version(ctx: dict[str, Any]) -> tuple[bool, dict[str, Any], str]:
    permit = ctx.get("permit_version")
    if not permit:
        return _failed("采样许可没有有效版本")
    if permit["status"] != "active":
        return _failed(f"许可版本状态为 {permit['status']}，不能据此出口")
    if permit["effective_at"] > ctx["now"]:
        return _failed(
            f"许可版本自 {permit['effective_at']} 起生效，当前时间 {ctx['now']} 尚不能据此出口",
        )
    return _passed({
        "permit_id": permit["permit_id"],
        "version": permit["version"],
        "status": permit["status"],
        "terms_hash": permit["terms_hash"],
        "effective_at": permit["effective_at"],
    })


def _check_use_restriction(ctx: dict[str, Any]) -> tuple[bool, dict[str, Any], str]:
    application = ctx["application"]
    intended_use = application["intended_use"]
    permit = ctx.get("permit_version")
    if not permit:
        return _failed("许可版本缺失，无法核对用途限制")
    allowed_by_permit = list(permit["terms"].get("allowed_uses", []))
    if intended_use not in allowed_by_permit:
        return _failed(f"用途 {intended_use} 不在许可允许范围 {allowed_by_permit} 内")
    mta = ctx.get("mta")
    if mta is not None:
        allowed_by_mta = list(mta["terms"].get("allowed_uses", []))
        if intended_use not in allowed_by_mta:
            return _failed(f"用途 {intended_use} 不在材料转移协议允许范围 {allowed_by_mta} 内")
    if not permit["terms"].get("export_allowed", False):
        return _failed("当前许可版本不允许出口")
    return _passed({
        "intended_use": intended_use,
        "allowed_uses_permit": allowed_by_permit,
        "export_allowed": True,
    })


def _check_lab_qualification(ctx: dict[str, Any]) -> tuple[bool, dict[str, Any], str]:
    intended_use = ctx["application"]["intended_use"]
    now = ctx["now"]
    lab = ctx.get("lab")
    if not lab:
        return _failed("接收实验室不存在")
    if not lab["active"]:
        return _failed("接收实验室处于停用状态")
    if lab["withdrawn"]:
        return _failed("接收实验室已退出合作，不能接收新分装")
    valid_matches = []
    expired_matches = []
    for qualification in ctx.get("qualifications", []):
        if qualification["scope"] != intended_use:
            continue
        if qualification["revoked"]:
            continue
        window_ok = qualification["valid_from"] <= now <= qualification["valid_until"]
        record = {
            "qualification_id": qualification["qualification_id"],
            "scope": qualification["scope"],
            "valid_from": qualification["valid_from"],
            "valid_until": qualification["valid_until"],
        }
        (valid_matches if window_ok else expired_matches).append(record)
    if valid_matches:
        return _passed({"intended_use": intended_use, "matched": valid_matches[0]})
    if expired_matches:
        return _failed("实验室对应用途的资质已过有效期", {"expired": expired_matches})
    if any(q["scope"] == intended_use and q["revoked"] for q in ctx.get("qualifications", [])):
        return _failed("实验室对应用途的资质已被撤销")
    return _failed(f"实验室缺少用途 {intended_use} 的资质")


def _check_mta(ctx: dict[str, Any]) -> tuple[bool, dict[str, Any], str]:
    mta = ctx.get("mta")
    batch = ctx["batch"]
    application = ctx["application"]
    if not mta:
        return _failed("所有权方与接收实验室之间没有有效的材料转移协议")
    if mta["status"] != "active":
        return _failed(f"材料转移协议状态为 {mta['status']}")
    if not (mta["effective_at"] <= ctx["now"] <= mta["expires_at"]):
        return _failed("材料转移协议不在有效期内")
    if mta["provider_organization_id"] != batch["owner_organization_id"]:
        return _failed("材料转移协议的提供方与批次所有权方不一致")
    if mta["recipient_lab_id"] != application["lab_id"]:
        return _failed("材料转移协议的接收方与申请实验室不一致")
    return _passed({
        "mta_id": mta["mta_id"],
        "version": mta["version"],
        "status": mta["status"],
        "terms_hash": mta["terms_hash"],
        "provider_organization_id": mta["provider_organization_id"],
        "recipient_lab_id": mta["recipient_lab_id"],
        "effective_at": mta["effective_at"],
        "expires_at": mta["expires_at"],
    })


def _check_consumption_budget(ctx: dict[str, Any]) -> tuple[bool, dict[str, Any], str]:
    application = ctx["application"]
    batch = ctx["batch"]
    requested = float(application["requested_quantity"])
    planned = float(application["planned_consumption"])
    total = float(batch["total_quantity"])
    already_budgeted = float(ctx.get("approved_consumption_total", 0.0))
    mta = ctx.get("mta")
    fraction = 1.0
    if mta is not None:
        fraction = float(mta["terms"].get("max_consumption_fraction", 1.0))
    cap_by_agreement = requested * fraction
    evidence = {
        "requested_quantity": requested,
        "planned_consumption": planned,
        "batch_total_quantity": total,
        "already_budgeted_consumption": already_budgeted,
        "allocatable_quantity": float(ctx.get("available_quantity", 0.0)),
        "max_consumption_fraction": fraction,
        "agreement_consumption_cap": cap_by_agreement,
        "batch_consumption_remaining": total - already_budgeted,
    }
    if requested > float(ctx.get("available_quantity", 0.0)) + EPS:
        return _failed("申请数量超过当前可分配数量", evidence)
    if planned > requested + EPS:
        return _failed("计划消耗量不能超过申请分装数量", evidence)
    if planned > cap_by_agreement + EPS:
        return _failed("消耗计划超过材料转移协议约定的消耗比例上限", evidence)
    if already_budgeted + planned > total + EPS:
        return _failed("累计消耗预算超过采集批次总量", evidence)
    return _passed(evidence)


def _check_return_agreement(ctx: dict[str, Any]) -> tuple[bool, dict[str, Any], str]:
    permit = ctx.get("permit_version")
    if not permit:
        return _failed("许可版本缺失，无法核对返还约定")
    if not permit["terms"].get("requires_return", False):
        return _passed({"return_required": False,
                        "waiver": "许可版本未要求返还，按破坏性分析豁免"})
    mta = ctx.get("mta")
    if not mta:
        return _failed("许可要求返还，但缺少材料转移协议约定")
    terms = mta["terms"]
    return_by_days = terms.get("return_by_days")
    if terms.get("return_required") and isinstance(return_by_days, (int, float)) and return_by_days > 0:
        return _passed({"return_required": True, "return_by_days": return_by_days})
    return _failed("许可要求返还，但材料转移协议缺少返还时限条款")


def _check_shipping_documents(ctx: dict[str, Any]) -> tuple[bool, dict[str, Any], str]:
    documents = ctx["application"].get("documents") or {}
    present = {key: str(value) for key, value in documents.items() if value}
    missing = [key for key in REQUIRED_SHIPPING_DOCS if not present.get(key)]
    evidence = {"required": list(REQUIRED_SHIPPING_DOCS), "present": present}
    if missing:
        return _failed(f"运输文件不齐全，缺少：{', '.join(missing)}", evidence)
    return _passed(evidence)


_EVALUATORS = {
    "ownership_custody": _check_ownership_custody,
    "permit_version": _check_permit_version,
    "use_restriction": _check_use_restriction,
    "lab_qualification": _check_lab_qualification,
    "mta": _check_mta,
    "consumption_budget": _check_consumption_budget,
    "return_agreement": _check_return_agreement,
    "shipping_documents": _check_shipping_documents,
}


def evaluate_check(code: str, ctx: dict[str, Any]) -> tuple[bool, dict[str, Any], str]:
    """依据已登记事实评估单条硬性条款，返回是否通过、证据与说明。"""

    if code not in CHECK_CODES:
        raise ValueError(f"未知审查条款：{code}")
    return _EVALUATORS[code](ctx)


def derive_status(balances: dict[str, float], *, customs_held: bool,
                  returning: bool) -> str:
    """根据量桶余额与运输状态推导分装当前状态。"""

    active = sum(max(0.0, balances.get(bucket, 0.0))
                 for bucket in ("reserved", "in_transit", "at_lab", "returned", "frozen"))
    if active <= EPS and balances.get("in_transit", 0.0) <= EPS:
        return "closed"
    if customs_held:
        return "customs_hold"
    if returning and balances.get("in_transit", 0.0) > EPS:
        return "returning"
    if balances.get("in_transit", 0.0) > EPS:
        return "in_transit"
    if balances.get("at_lab", 0.0) > EPS:
        return "partial_consumed" if balances.get("consumed", 0.0) > EPS else "delivered"
    if balances.get("returned", 0.0) > EPS:
        return "returned_pending"
    if balances.get("frozen", 0.0) > EPS and balances.get("reserved", 0.0) <= EPS:
        return "frozen"
    if balances.get("reserved", 0.0) > EPS:
        return "reserved"
    return "closed"
