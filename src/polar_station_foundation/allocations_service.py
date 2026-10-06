"""实现样品分配与出口许可的申请、审查、签发与处置流程。

设计要点：

- 所有数量移动都在 ``BEGIN IMMEDIATE`` 事务内基于最新流水重算余额后写入，
  申请幂等回执与数量占用同生共死，重复申请或并发确认都不可能超分配；
- 八项硬性条款在提交时和批准时各评估一次、分别留痕，任一未通过即拒绝；
- 批准的分装固化当时的许可条款与 MTA 条款摘要，旧许可日后被替代或撤回
  都不改变既有授权依据，新规则只能以新版本登记；
- 退件、实验室退出、许可撤回只移动尚未消耗份额所在的量桶；
- 每份分装随时可导出权利来源、当前责任方与剩余义务报告。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from .allocation import CHECK_CARDS, EPS, REAL_BUCKETS, derive_status, evaluate_check
from .audit import append_event, canonical_json, digest
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .service import DomainService
from .storage import Database

SHIPMENT_EVENTS = frozenset({
    "customs_hold", "customs_release", "deliver", "customs_returned", "return_received",
})


class AllocationService(DomainService):
    """在基础服务之上协调样品批次、许可版本与分装全生命周期。"""

    def __init__(self, database: Database, clock=None) -> None:
        super().__init__(database, clock)

    # ------------------------------------------------------------------
    # 通用辅助
    # ------------------------------------------------------------------

    def _iso(self, value: str, field: str) -> str:
        text = self._text(value, field, 40)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"{field} 必须是 ISO-8601 时间") from exc
        return parsed.isoformat().replace("+00:00", "Z")

    def _quantity(self, value: Any, field: str, *, positive: bool = False) -> float:
        try:
            quantity = float(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{field} 必须是数字") from exc
        if quantity != quantity or quantity in (float("inf"), float("-inf")):
            raise ValidationError(f"{field} 必须是有限数字")
        if positive:
            if quantity <= 0:
                raise ValidationError(f"{field} 必须大于 0")
        elif quantity < 0:
            raise ValidationError(f"{field} 不能为负数")
        return quantity

    def _terms(self, value: Any, field: str) -> dict[str, Any]:
        if not isinstance(value, dict) or not value:
            raise ValidationError(f"{field} 必须是非空对象")
        return value

    def _load_batch(self, connection, batch_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM sample_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFoundError("采集批次不存在")
        return dict(row)

    def _active_permit(self, connection, batch_id: str) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT * FROM permits WHERE batch_id=? AND status='active' "
            "ORDER BY version DESC LIMIT 1",
            (batch_id,),
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["terms"] = json.loads(data.pop("terms_json"))
        return data

    def _permit_version(self, connection, permit_id: str, version: int) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM permits WHERE permit_id=? AND version=?", (permit_id, version)
        ).fetchone()
        if row is None:
            raise NotFoundError("许可版本不存在")
        data = dict(row)
        data["terms"] = json.loads(data.pop("terms_json"))
        return data

    def _active_mta(self, connection, provider_organization_id: str,
                    recipient_lab_id: str) -> dict[str, Any] | None:
        now = self._now()
        row = connection.execute(
            "SELECT * FROM mta_agreements WHERE provider_organization_id=? AND recipient_lab_id=? "
            "AND status='active' AND effective_at<=? AND expires_at>=? "
            "ORDER BY version DESC LIMIT 1",
            (provider_organization_id, recipient_lab_id, now, now),
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["terms"] = json.loads(data.pop("terms_json"))
        return data

    def _load_lab(self, connection, lab_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM laboratories WHERE lab_id=?", (lab_id,)).fetchone()
        if row is None:
            raise NotFoundError("接收实验室不存在")
        data = dict(row)
        data["active"] = bool(data["active"])
        data["withdrawn"] = bool(data["withdrawn"])
        return data

    def _load_allocation(self, connection, allocation_id: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM allocations WHERE allocation_id=?", (allocation_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("分装不存在")
        return dict(row)

    def _qualifications(self, connection, lab_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM lab_qualifications WHERE lab_id=?", (lab_id,)
        ).fetchall()
        return [dict(row, revoked=bool(row["revoked"])) for row in rows]

    def _balances(self, connection, batch_id: str,
                  allocation_id: str | None = None) -> dict[str, float]:
        balances = {bucket: 0.0 for bucket in REAL_BUCKETS}
        # 分装账从 reserved 起算：从批次公共池 available 的注入/回收腿只属于批次总账。
        tracked = REAL_BUCKETS if allocation_id is None else (
            "reserved", "in_transit", "at_lab", "consumed", "returned", "frozen")
        query = "SELECT * FROM quantity_movements WHERE batch_id=?"
        parameters: list[Any] = [batch_id]
        if allocation_id is not None:
            query += " AND allocation_id=?"
            parameters.append(allocation_id)
        for row in connection.execute(query, parameters):
            if row["from_bucket"] in tracked:
                balances[row["from_bucket"]] -= row["quantity"]
            if row["to_bucket"] in tracked:
                balances[row["to_bucket"]] += row["quantity"]
        return balances

    def _move(self, connection, *, batch_id: str, allocation_id: str | None, event_type: str,
              from_bucket: str, to_bucket: str, quantity: float, created_by: str,
              document_ref: str | None = None, detail: dict[str, Any] | None = None,
              allocation_scoped: bool = False) -> None:
        if quantity <= 0:
            raise ValidationError("移动数量必须大于 0")
        balance_scope = allocation_id if allocation_scoped else None
        balances = self._balances(connection, batch_id, balance_scope)
        if from_bucket in balances and balances[from_bucket] + EPS < quantity:
            raise ConflictError(
                f"{from_bucket} 余额 {balances[from_bucket]:g} 不足，无法移出 {quantity:g}"
            )
        # 记录的余额快照始终按批次全局口径，便于对账；源桶充足性按调用方指定口径。
        global_balances = self._balances(connection, batch_id) if allocation_scoped else balances
        if from_bucket in global_balances:
            global_balances[from_bucket] -= quantity
        if to_bucket in global_balances:
            global_balances[to_bucket] += quantity
        connection.execute(
            "INSERT INTO quantity_movements(movement_id,batch_id,allocation_id,event_type,"
            "from_bucket,to_bucket,quantity,balance_after_json,document_ref,detail_json,"
            "created_by,occurred_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, batch_id, allocation_id, event_type, from_bucket, to_bucket,
             quantity, canonical_json(global_balances), document_ref, canonical_json(detail or {}),
             created_by, self._now()),
        )

    def _evaluation_context(self, connection, batch: dict[str, Any], lab_id: str,
                            intended_use: str, requested_quantity: float,
                            planned_consumption: float, documents: dict[str, Any]) -> dict[str, Any]:
        lab = None
        try:
            lab = self._load_lab(connection, lab_id)
        except NotFoundError:
            lab = None
        permit = self._active_permit(connection, batch["batch_id"])
        mta = self._active_mta(connection, batch["owner_organization_id"], lab_id)
        approved_consumption = connection.execute(
            "SELECT COALESCE(SUM(planned_consumption),0) AS total FROM allocations "
            "WHERE batch_id=? AND approved=1",
            (batch["batch_id"],),
        ).fetchone()["total"]
        available_quantity = self._balances(connection, batch["batch_id"])["available"]
        return {
            "now": self._now(),
            "batch": batch,
            "lab": lab,
            "qualifications": self._qualifications(connection, lab_id) if lab else [],
            "permit_version": permit,
            "mta": mta,
            "approved_consumption_total": float(approved_consumption),
            "available_quantity": available_quantity,
            "application": {
                "lab_id": lab_id,
                "intended_use": intended_use,
                "requested_quantity": requested_quantity,
                "planned_consumption": planned_consumption,
                "documents": documents,
            },
        }

    def _evaluate(self, ctx: dict[str, Any]) -> dict[str, Any]:
        results: dict[str, Any] = {}
        for code, meta in CHECK_CARDS.items():
            passed, evidence, reason = evaluate_check(code, ctx)
            results[code] = {"label": meta["label"], "mandatory": meta["mandatory"],
                             "passed": passed, "evidence": evidence, "reason": reason}
        return {"evaluated_at": self._now(), "results": results,
                "all_passed": all(item["passed"] for item in results.values())}

    def _require_passed(self, snapshot: dict[str, Any]) -> None:
        failed = {code: item["reason"] for code, item in snapshot["results"].items()
                  if not item["passed"]}
        if failed:
            raise ConflictError(json.dumps({"硬性条款未通过": failed}, ensure_ascii=False))

    # ------------------------------------------------------------------
    # 基础事实登记
    # ------------------------------------------------------------------

    def register_batch(self, *, request_id: str, actor_id: str, batch_id: str, site_id: str,
                       external_key: str, owner_organization_id: str,
                       custodian_organization_id: str, total_quantity: float,
                       unit: str, collected_at: str) -> Any:
        payload = {"actor_id": actor_id, "batch_id": batch_id, "site_id": site_id,
                   "external_key": external_key,
                   "owner_organization_id": owner_organization_id,
                   "custodian_organization_id": custodian_organization_id,
                   "total_quantity": total_quantity, "unit": unit,
                   "collected_at": collected_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            batch_id = self._identifier(batch_id, "batch_id")
            external_key = self._identifier(external_key, "external_key")
            site = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
            if site is None:
                raise NotFoundError("场所不存在")
            for field, org_id in (("owner_organization_id", owner_organization_id),
                                  ("custodian_organization_id", custodian_organization_id)):
                if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                      (org_id,)).fetchone() is None:
                    raise NotFoundError(f"{field} 指向的组织不存在")
            total = self._quantity(total_quantity, "total_quantity", positive=True)
            unit = self._text(unit, "unit", 16)
            collected_at = self._iso(collected_at, "collected_at")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO sample_batches(batch_id,site_id,external_key,"
                        "owner_organization_id,custodian_organization_id,total_quantity,"
                        "unit,collected_at,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (batch_id, site_id, external_key, owner_organization_id,
                         custodian_organization_id, total, unit, collected_at, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("批次编号或业务键已经存在") from exc
                # 期初库存：来源桶为空，全额进入可分配桶。
                self._move(connection, batch_id=batch_id, allocation_id=None,
                           event_type="batch_registered", from_bucket="", to_bucket="available",
                           quantity=total, created_by=actor_id,
                           detail={"external_key": external_key})
                append_event(connection, actor_id=actor_id, action="batch.registered",
                             resource_type="sample_batch", resource_id=batch_id,
                             detail={"site_id": site_id, "external_key": external_key,
                                     "owner_organization_id": owner_organization_id,
                                     "custodian_organization_id": custodian_organization_id,
                                     "total_quantity": total, "unit": unit},
                             occurred_at=self._now())
                return "sample_batch", batch_id, {"batch_id": batch_id, "total_quantity": total}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_batch", payload=payload, create=create)

    def register_permit_version(self, *, request_id: str, actor_id: str, permit_id: str,
                                batch_id: str, terms: dict[str, Any],
                                effective_at: str | None = None) -> Any:
        payload = {"actor_id": actor_id, "permit_id": permit_id, "batch_id": batch_id,
                   "terms": terms, "effective_at": effective_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            permit_id = self._identifier(permit_id, "permit_id")
            batch = self._load_batch(connection, batch_id)
            terms = self._terms(terms, "terms")
            allowed_uses = terms.get("allowed_uses")
            if not isinstance(allowed_uses, list) or not all(isinstance(v, str) and v for v in allowed_uses):
                raise ValidationError("terms.allowed_uses 必须是非空字符串数组")
            if not isinstance(terms.get("export_allowed"), bool):
                raise ValidationError("terms.export_allowed 必须是布尔值")
            if not isinstance(terms.get("requires_return"), bool):
                raise ValidationError("terms.requires_return 必须是布尔值")
            effective_at = self._iso(effective_at or self._now(), "effective_at")
            row = connection.execute(
                "SELECT MAX(version) AS version FROM permits WHERE permit_id=?", (permit_id,)
            ).fetchone()
            version = (row["version"] or 0) + 1
            terms_hash = digest(terms)

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO permits(permit_id,version,batch_id,terms_json,terms_hash,status,"
                    "created_by,effective_at,created_at) VALUES(?,?,?,?,?,'active',?,?,?)",
                    (permit_id, version, batch_id, canonical_json(terms), terms_hash,
                     actor_id, effective_at, self._now()),
                )
                superseded = connection.execute(
                    "SELECT permit_id AS pid, version, status FROM permits "
                    "WHERE batch_id=? AND status IN ('active','withdrawn') "
                    "AND NOT (permit_id=? AND version=?)",
                    (batch_id, permit_id, version),
                ).fetchall()
                reauthorized_frozen = 0.0
                reauthorizing = any(previous["status"] == "withdrawn" for previous in superseded)
                for previous in superseded:
                    withdrawn = previous["status"] == "withdrawn"
                    connection.execute(
                        "UPDATE permits SET status='superseded',superseded_by_permit_id=?,"
                        "superseded_by_version=? WHERE permit_id=? AND version=?",
                        (permit_id, version, previous["pid"], previous["version"]),
                    )
                # 撤回后以新版本恢复授权：撤回期间冻结的份额（含批次公共池与各分装预留/
                # 退件份额）才可解除冻结，重新回到可分配桶。
                if reauthorizing:
                    frozen_rows = connection.execute(
                        "SELECT allocation_id AS aid, "
                        "SUM(CASE WHEN to_bucket='frozen' THEN quantity ELSE 0 END) "
                        "- SUM(CASE WHEN from_bucket='frozen' THEN quantity ELSE 0 END) AS amount "
                        "FROM quantity_movements WHERE batch_id=? GROUP BY allocation_id",
                        (batch_id,),
                    ).fetchall()
                    for frozen_row in frozen_rows:
                        amount = float(frozen_row["amount"] or 0.0)
                        if amount <= EPS:
                            continue
                        self._move(connection, batch_id=batch_id,
                                   allocation_id=frozen_row["aid"],
                                   event_type="permit_reauthorized", from_bucket="frozen",
                                   to_bucket="available", quantity=amount,
                                   created_by=actor_id,
                                   document_ref=f"{permit_id}:v{version}",
                                   allocation_scoped=frozen_row["aid"] is not None,
                                   detail={"reissued_permit": f"{permit_id}:v{version}"})
                        reauthorized_frozen += amount
                        # 旧分装依据的是被撤回的旧版本，标记保留；份额回到公共池后
                        # 由新许可下的新申请重新占用。
                append_event(connection, actor_id=actor_id, action="permit.version_registered",
                             resource_type="permit", resource_id=f"{permit_id}:v{version}",
                             detail={"batch_id": batch_id, "version": version,
                                     "terms_hash": terms_hash, "effective_at": effective_at,
                                     "superseded": [f"{r['pid']}:v{r['version']}" for r in superseded],
                                     "reauthorized_frozen": reauthorized_frozen},
                             occurred_at=self._now())
                return ("permit_version", f"{permit_id}:v{version}",
                        {"permit_id": permit_id, "version": version, "terms_hash": terms_hash})

            return self._idempotent(connection, request_id=request_id,
                                    action="register_permit_version", payload=payload, create=create)

    def register_lab(self, *, request_id: str, actor_id: str, lab_id: str,
                     organization_id: str, name: str, country_code: str) -> Any:
        payload = {"actor_id": actor_id, "lab_id": lab_id,
                   "organization_id": organization_id, "name": name, "country_code": country_code}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            lab_id = self._identifier(lab_id, "lab_id")
            name = self._text(name, "name")
            country_code = self._text(country_code, "country_code", 8).upper()
            if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                  (organization_id,)).fetchone() is None:
                raise NotFoundError("组织不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO laboratories(lab_id,organization_id,name,country_code,active,"
                        "withdrawn,created_at) VALUES(?,?,?,?,1,0,?)",
                        (lab_id, organization_id, name, country_code, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("实验室编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="lab.registered",
                             resource_type="laboratory", resource_id=lab_id,
                             detail={"organization_id": organization_id, "name": name,
                                     "country_code": country_code},
                             occurred_at=self._now())
                return "laboratory", lab_id, {"lab_id": lab_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_lab", payload=payload, create=create)

    def register_qualification(self, *, request_id: str, actor_id: str, qualification_id: str,
                               lab_id: str, scope: str, valid_from: str,
                               valid_until: str) -> Any:
        payload = {"actor_id": actor_id, "qualification_id": qualification_id, "lab_id": lab_id,
                   "scope": scope, "valid_from": valid_from, "valid_until": valid_until}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            qualification_id = self._identifier(qualification_id, "qualification_id")
            lab = self._load_lab(connection, lab_id)
            scope = self._text(scope, "scope", 120)
            valid_from = self._iso(valid_from, "valid_from")
            valid_until = self._iso(valid_until, "valid_until")
            if valid_until <= valid_from:
                raise ValidationError("资质有效期止期必须晚于起期")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO lab_qualifications(qualification_id,lab_id,scope,valid_from,"
                        "valid_until,revoked,created_at) VALUES(?,?,?,?,?,0,?)",
                        (qualification_id, lab["lab_id"], scope, valid_from, valid_until,
                         self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("资质编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="qualification.registered",
                             resource_type="lab_qualification", resource_id=qualification_id,
                             detail={"lab_id": lab["lab_id"], "scope": scope,
                                     "valid_from": valid_from, "valid_until": valid_until},
                             occurred_at=self._now())
                return "lab_qualification", qualification_id, {"qualification_id": qualification_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_qualification", payload=payload, create=create)

    def revoke_qualification(self, *, request_id: str, actor_id: str,
                             qualification_id: str) -> Any:
        payload = {"actor_id": actor_id, "qualification_id": qualification_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            row = connection.execute(
                "SELECT * FROM lab_qualifications WHERE qualification_id=?", (qualification_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("资质不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE lab_qualifications SET revoked=1 WHERE qualification_id=?",
                    (qualification_id,),
                )
                append_event(connection, actor_id=actor_id, action="qualification.revoked",
                             resource_type="lab_qualification", resource_id=qualification_id,
                             detail={"lab_id": row["lab_id"], "scope": row["scope"]},
                             occurred_at=self._now())
                return "lab_qualification", qualification_id, {"qualification_id": qualification_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="revoke_qualification", payload=payload, create=create)

    def register_mta(self, *, request_id: str, actor_id: str, mta_id: str,
                     provider_organization_id: str, recipient_lab_id: str,
                     terms: dict[str, Any], effective_at: str, expires_at: str) -> Any:
        payload = {"actor_id": actor_id, "mta_id": mta_id,
                   "provider_organization_id": provider_organization_id,
                   "recipient_lab_id": recipient_lab_id, "terms": terms,
                   "effective_at": effective_at, "expires_at": expires_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            mta_id = self._identifier(mta_id, "mta_id")
            if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                  (provider_organization_id,)).fetchone() is None:
                raise NotFoundError("提供方组织不存在")
            lab = self._load_lab(connection, recipient_lab_id)
            terms = self._terms(terms, "terms")
            allowed_uses = terms.get("allowed_uses")
            if not isinstance(allowed_uses, list) or not all(isinstance(v, str) and v for v in allowed_uses):
                raise ValidationError("terms.allowed_uses 必须是非空字符串数组")
            fraction = terms.get("max_consumption_fraction", 1.0)
            if not isinstance(fraction, (int, float)) or not 0 <= float(fraction) <= 1:
                raise ValidationError("terms.max_consumption_fraction 必须是 0 到 1 之间的数字")
            if not isinstance(terms.get("return_required"), bool):
                raise ValidationError("terms.return_required 必须是布尔值")
            if terms["return_required"] and not (
                isinstance(terms.get("return_by_days"), (int, float))
                and terms["return_by_days"] > 0
            ):
                raise ValidationError("要求返还时 terms.return_by_days 必须是正数")
            effective_at = self._iso(effective_at, "effective_at")
            expires_at = self._iso(expires_at, "expires_at")
            if expires_at <= effective_at:
                raise ValidationError("MTA 止期必须晚于起期")
            row = connection.execute(
                "SELECT MAX(version) AS version FROM mta_agreements WHERE mta_id=?", (mta_id,)
            ).fetchone()
            version = (row["version"] or 0) + 1
            terms_hash = digest(terms)

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO mta_agreements(mta_id,version,provider_organization_id,"
                    "recipient_lab_id,terms_json,terms_hash,status,effective_at,expires_at,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,'active',?,?,?,?)",
                    (mta_id, version, provider_organization_id, lab["lab_id"],
                     canonical_json(terms), terms_hash, effective_at, expires_at,
                     actor_id, self._now()),
                )
                connection.execute(
                    "UPDATE mta_agreements SET status='terminated' WHERE mta_id=? AND version<>?",
                    (mta_id, version),
                )
                append_event(connection, actor_id=actor_id, action="mta.registered",
                             resource_type="mta", resource_id=f"{mta_id}:v{version}",
                             detail={"provider_organization_id": provider_organization_id,
                                     "recipient_lab_id": lab["lab_id"], "version": version,
                                     "terms_hash": terms_hash},
                             occurred_at=self._now())
                return "mta", f"{mta_id}:v{version}", {"mta_id": mta_id, "version": version}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_mta", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 申请、审查与批准（原子占用）
    # ------------------------------------------------------------------

    def submit_application(self, *, request_id: str, actor_id: str, batch_id: str,
                           lab_id: str, intended_use: str, requested_quantity: float,
                           planned_consumption: float,
                           documents: dict[str, Any] | None = None) -> Any:
        documents = documents or {}
        if not isinstance(documents, dict):
            raise ValidationError("documents 必须是对象")
        payload = {"actor_id": actor_id, "batch_id": batch_id, "lab_id": lab_id,
                   "intended_use": intended_use, "requested_quantity": requested_quantity,
                   "planned_consumption": planned_consumption, "documents": documents}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            batch = self._load_batch(connection, batch_id)
            lab = self._load_lab(connection, lab_id)
            intended_use = self._text(intended_use, "intended_use", 120)
            requested = self._quantity(requested_quantity, "requested_quantity", positive=True)
            planned = self._quantity(planned_consumption, "planned_consumption")
            clean_docs = {str(key): str(value) for key, value in documents.items() if value}

            def create() -> tuple[str, str, dict[str, Any]]:
                ctx = self._evaluation_context(
                    connection, batch, lab["lab_id"], intended_use, requested, planned, clean_docs
                )
                snapshot = self._evaluate(ctx)
                allocation_id = uuid.uuid4().hex
                permit = ctx["permit_version"]
                mta = ctx["mta"]
                connection.execute(
                    "INSERT INTO allocations(allocation_id,request_id,batch_id,lab_id,permit_id,"
                    "permit_version,permit_terms_hash,permit_terms_json,mta_id,mta_version,"
                    "mta_terms_hash,mta_terms_json,intended_use,requested_quantity,"
                    "planned_consumption,documents_json,checks_json,approved,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)",
                    (allocation_id, self._identifier(request_id, "request_id"), batch["batch_id"],
                     lab["lab_id"], permit["permit_id"] if permit else None,
                     permit["version"] if permit else None,
                     permit["terms_hash"] if permit else "",
                     canonical_json(permit["terms"]) if permit else "{}",
                     mta["mta_id"] if mta else None, mta["version"] if mta else None,
                     mta["terms_hash"] if mta else "",
                     canonical_json(mta["terms"]) if mta else "{}",
                     intended_use, requested, planned, canonical_json(clean_docs),
                     canonical_json(snapshot),
                     actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="allocation.submitted",
                             resource_type="allocation", resource_id=allocation_id,
                             detail={"batch_id": batch["batch_id"], "lab_id": lab["lab_id"],
                                     "intended_use": intended_use,
                                     "requested_quantity": requested,
                                     "planned_consumption": planned,
                                     "all_passed": snapshot["all_passed"],
                                     "failed": [code for code, item in snapshot["results"].items()
                                                if not item["passed"]]},
                             occurred_at=self._now())
                return ("allocation", allocation_id,
                        {"allocation_id": allocation_id, "approved": False,
                         "all_passed": snapshot["all_passed"]})

            return self._idempotent(connection, request_id=request_id,
                                    action="submit_application", payload=payload, create=create)

    def approve_application(self, *, request_id: str, actor_id: str,
                            allocation_id: str) -> Any:
        payload = {"actor_id": actor_id, "allocation_id": allocation_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            allocation = self._load_allocation(connection, allocation_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                if allocation["approved"]:
                    raise ConflictError("分装已经批准，不能重复签发")
                # 批准瞬间基于最新事实重新评估，防止提交后条件恶化仍被签发。
                batch = self._load_batch(connection, allocation["batch_id"])
                application_documents = json.loads(allocation["documents_json"])
                ctx = self._evaluation_context(
                    connection, batch, allocation["lab_id"], allocation["intended_use"],
                    allocation["requested_quantity"], allocation["planned_consumption"],
                    application_documents,
                )
                snapshot = self._evaluate(ctx)
                balances = self._balances(connection, batch["batch_id"])
                if balances["available"] + EPS < allocation["requested_quantity"]:
                    raise ConflictError(
                        f"可分配数量 {balances['available']:g} 不足，申请需要 "
                        f"{allocation['requested_quantity']:g}"
                    )
                self._require_passed(snapshot)
                # 全部硬性条款通过后，才在同一事务内原子占用。
                self._move(connection, batch_id=batch["batch_id"], allocation_id=allocation_id,
                           event_type="allocation_approved", from_bucket="available",
                           to_bucket="reserved", quantity=allocation["requested_quantity"],
                           created_by=actor_id, detail={"request_id": allocation["request_id"]})
                connection.execute(
                    "UPDATE allocations SET approved=1, approved_at=?, checks_json=? "
                    "WHERE allocation_id=?",
                    (self._now(), canonical_json(snapshot), allocation_id),
                )
                append_event(connection, actor_id=actor_id, action="allocation.approved",
                             resource_type="allocation", resource_id=allocation_id,
                             detail={"batch_id": batch["batch_id"],
                                     "requested_quantity": allocation["requested_quantity"],
                                     "permit": f"{allocation['permit_id']}:v{allocation['permit_version']}",
                                     "mta": f"{allocation['mta_id']}:v{allocation['mta_version']}",
                                     "all_passed": True},
                             occurred_at=self._now())
                return ("allocation", allocation_id,
                        {"allocation_id": allocation_id, "approved": True,
                         "reserved_quantity": allocation["requested_quantity"]})

            return self._idempotent(connection, request_id=request_id,
                                    action="approve_application", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 发运、海关与交付
    # ------------------------------------------------------------------

    def ship_shipment(self, *, request_id: str, actor_id: str, allocation_id: str,
                      quantity: float, carrier_ref: str,
                      documents: dict[str, Any] | None = None) -> Any:
        documents = documents or {}
        payload = {"actor_id": actor_id, "allocation_id": allocation_id,
                   "quantity": quantity, "carrier_ref": carrier_ref, "documents": documents}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            allocation = self._load_allocation(connection, allocation_id)
            quantity = self._quantity(quantity, "quantity", positive=True)
            carrier_ref = self._text(carrier_ref, "carrier_ref", 120)
            clean_docs = {str(key): str(value) for key, value in documents.items() if value}

            def create() -> tuple[str, str, dict[str, Any]]:
                # 状态守卫放在幂等查重之后，事后回放返回原回执而非新状态的错误。
                if not allocation["approved"]:
                    raise ConflictError("分装尚未通过批准，不能发运")
                if allocation["permit_withdrawn"]:
                    raise ConflictError("许可已撤回，不能继续发运")
                if allocation["lab_withdrawn"]:
                    raise ConflictError("接收实验室已退出合作，不能继续发运")
                balances = self._balances(connection, allocation["batch_id"], allocation_id)
                if balances["reserved"] + EPS < quantity:
                    raise ConflictError(
                        f"待发预留数量 {balances['reserved']:g} 不足，不能发运 {quantity:g}"
                    )
                shipment_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO shipments(shipment_id,allocation_id,kind,quantity,carrier_ref,"
                    "documents_json,status,created_by,created_at) "
                    "VALUES(?,?, 'outbound',?,?,?, 'in_transit',?,?)",
                    (shipment_id, allocation_id, quantity, carrier_ref,
                     canonical_json(clean_docs), actor_id, self._now()),
                )
                self._move(connection, batch_id=allocation["batch_id"], allocation_id=allocation_id,
                           event_type="shipped", from_bucket="reserved", to_bucket="in_transit",
                           quantity=quantity, created_by=actor_id, document_ref=shipment_id,
                           detail={"carrier_ref": carrier_ref}, allocation_scoped=True)
                append_event(connection, actor_id=actor_id, action="shipment.shipped",
                             resource_type="shipment", resource_id=shipment_id,
                             detail={"allocation_id": allocation_id, "quantity": quantity,
                                     "carrier_ref": carrier_ref, "partial": quantity < allocation["requested_quantity"]},
                             occurred_at=self._now())
                return "shipment", shipment_id, {"shipment_id": shipment_id, "quantity": quantity}

            return self._idempotent(connection, request_id=request_id,
                                    action="ship_shipment", payload=payload, create=create)

    def shipment_event(self, *, request_id: str, actor_id: str, shipment_id: str,
                       event: str, note: str | None = None) -> Any:
        event = self._identifier(event, "event")
        if event not in SHIPMENT_EVENTS:
            raise ValidationError(f"event 必须是 {sorted(SHIPMENT_EVENTS)} 之一")
        payload = {"actor_id": actor_id, "shipment_id": shipment_id, "event": event, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            row = connection.execute(
                "SELECT * FROM shipments WHERE shipment_id=?", (shipment_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("运输记录不存在")
            shipment = dict(row)
            allocation = self._load_allocation(connection, shipment["allocation_id"])

            transitions = {
                "customs_hold": (("in_transit",), "customs_held"),
                "customs_release": (("customs_held",), "in_transit"),
                "deliver": (("in_transit",), "delivered"),
                "customs_returned": (("in_transit", "customs_held"), "returned_to_station"),
                "return_received": (("in_transit",), "returned_to_station"),
            }
            required_from, target = transitions[event]

            def create() -> tuple[str, str, dict[str, Any]]:
                # 状态机校验放在幂等查重之后，使同一 request_id 的重放返回原回执。
                latest = connection.execute(
                    "SELECT * FROM shipments WHERE shipment_id=?", (shipment_id,)
                ).fetchone()
                current_status = latest["status"]
                if current_status not in required_from:
                    raise ConflictError(
                        f"运输记录当前状态为 {current_status}，不能处理 {event}"
                    )
                if event == "deliver" and shipment["kind"] != "outbound":
                    raise ConflictError("交付事件只适用于出境发运")
                if event == "return_received" and shipment["kind"] != "return":
                    raise ConflictError("退件签收事件只适用于返还发运")
                if event == "deliver":
                    self._move(connection, batch_id=allocation["batch_id"],
                               allocation_id=allocation["allocation_id"], event_type="delivered",
                               from_bucket="in_transit", to_bucket="at_lab",
                               quantity=shipment["quantity"], created_by=actor_id,
                               document_ref=shipment_id, detail={"note": note or ""},
                               allocation_scoped=True)
                elif event in ("customs_returned", "return_received"):
                    # 许可已撤回后到站的份额不能回流可分配池，直接冻结等待新许可版本。
                    target_bucket = "frozen" if allocation["permit_withdrawn"] else "returned"
                    self._move(connection, batch_id=allocation["batch_id"],
                               allocation_id=allocation["allocation_id"], event_type=event,
                               from_bucket="in_transit", to_bucket=target_bucket,
                               quantity=shipment["quantity"], created_by=actor_id,
                               document_ref=shipment_id,
                               detail={"note": note or "", "frozen": target_bucket == "frozen"},
                               allocation_scoped=True)
                connection.execute("UPDATE shipments SET status=? WHERE shipment_id=?",
                                   (target, shipment_id))
                append_event(connection, actor_id=actor_id, action=f"shipment.{event}",
                             resource_type="shipment", resource_id=shipment_id,
                             detail={"allocation_id": allocation["allocation_id"],
                                     "previous_status": current_status, "status": target,
                                     "quantity": shipment["quantity"], "note": note or ""},
                             occurred_at=self._now())
                return "shipment", shipment_id, {"shipment_id": shipment_id, "status": target}

            return self._idempotent(connection, request_id=request_id,
                                    action=f"shipment_event:{event}", payload=payload,
                                    create=create)

    # ------------------------------------------------------------------
    # 消耗、发表与返还
    # ------------------------------------------------------------------

    def record_consumption(self, *, request_id: str, actor_id: str, allocation_id: str,
                           quantity: float, note: str | None = None) -> Any:
        payload = {"actor_id": actor_id, "allocation_id": allocation_id,
                   "quantity": quantity, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            allocation = self._load_allocation(connection, allocation_id)
            quantity = self._quantity(quantity, "quantity", positive=True)

            def create() -> tuple[str, str, dict[str, Any]]:
                if not allocation["approved"]:
                    raise ConflictError("分装未批准，不能登记消耗")
                if allocation["permit_withdrawn"]:
                    raise ConflictError("许可已撤回，未消耗份额只能返还，不能继续消耗")
                lab = self._load_lab(connection, allocation["lab_id"])
                if lab["withdrawn"] or allocation["lab_withdrawn"]:
                    raise ConflictError("实验室已退出合作，不能继续消耗，只能安排返还")
                balances = self._balances(connection, allocation["batch_id"], allocation_id)
                if balances["at_lab"] + EPS < quantity:
                    raise ConflictError(
                        f"实验室持有未消耗份额 {balances['at_lab']:g} 不足，不能消耗 {quantity:g}"
                    )
                planned_total = allocation["planned_consumption"]
                consumed_before = balances["consumed"]
                if consumed_before + quantity > planned_total + EPS:
                    raise ConflictError(
                        f"累计消耗 {consumed_before + quantity:g} 超过批准消耗预算 {planned_total:g}"
                    )
                self._move(connection, batch_id=allocation["batch_id"],
                           allocation_id=allocation_id, event_type="consumed",
                           from_bucket="at_lab", to_bucket="consumed", quantity=quantity,
                           created_by=actor_id, detail={"note": note or ""},
                           allocation_scoped=True)
                append_event(connection, actor_id=actor_id, action="allocation.consumed",
                             resource_type="allocation", resource_id=allocation_id,
                             detail={"quantity": quantity, "note": note or ""},
                             occurred_at=self._now())
                return "allocation", allocation_id, {"allocation_id": allocation_id,
                                                      "consumed_quantity": quantity}

            return self._idempotent(connection, request_id=request_id,
                                    action="record_consumption", payload=payload, create=create)

    def register_return_shipment(self, *, request_id: str, actor_id: str, allocation_id: str,
                                 quantity: float, carrier_ref: str | None = None) -> Any:
        payload = {"actor_id": actor_id, "allocation_id": allocation_id,
                   "quantity": quantity, "carrier_ref": carrier_ref}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            allocation = self._load_allocation(connection, allocation_id)
            quantity = self._quantity(quantity, "quantity", positive=True)
            carrier_ref = self._text(carrier_ref or f"return-{allocation_id[:8]}",
                                     "carrier_ref", 120)

            def create() -> tuple[str, str, dict[str, Any]]:
                balances = self._balances(connection, allocation["batch_id"], allocation_id)
                if balances["at_lab"] + EPS < quantity:
                    raise ConflictError(
                        f"实验室持有未消耗份额 {balances['at_lab']:g} 不足，不能返还 {quantity:g}"
                    )
                shipment_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO shipments(shipment_id,allocation_id,kind,quantity,carrier_ref,"
                    "documents_json,status,created_by,created_at) "
                    "VALUES(?,?, 'return',?,?, '{}', 'in_transit',?,?)",
                    (shipment_id, allocation_id, quantity, carrier_ref, actor_id, self._now()),
                )
                self._move(connection, batch_id=allocation["batch_id"],
                           allocation_id=allocation_id, event_type="return_shipped",
                           from_bucket="at_lab", to_bucket="in_transit", quantity=quantity,
                           created_by=actor_id, document_ref=shipment_id,
                           detail={"carrier_ref": carrier_ref}, allocation_scoped=True)
                append_event(connection, actor_id=actor_id, action="shipment.return_started",
                             resource_type="shipment", resource_id=shipment_id,
                             detail={"allocation_id": allocation_id, "quantity": quantity},
                             occurred_at=self._now())
                return "shipment", shipment_id, {"shipment_id": shipment_id, "quantity": quantity}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_return_shipment", payload=payload,
                                    create=create)

    def restock_returned(self, *, request_id: str, actor_id: str, allocation_id: str,
                         quantity: float) -> Any:
        """把退回站内、检验合格的份额重新纳入可分配库存。"""

        payload = {"actor_id": actor_id, "allocation_id": allocation_id, "quantity": quantity}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            allocation = self._load_allocation(connection, allocation_id)
            quantity = self._quantity(quantity, "quantity", positive=True)

            def create() -> tuple[str, str, dict[str, Any]]:
                balances = self._balances(connection, allocation["batch_id"], allocation_id)
                if balances["returned"] + EPS < quantity:
                    raise ConflictError(
                        f"站内退回份额 {balances['returned']:g} 不足，不能重新入库 {quantity:g}"
                    )
                self._move(connection, batch_id=allocation["batch_id"],
                           allocation_id=allocation_id, event_type="restocked",
                           from_bucket="returned", to_bucket="available", quantity=quantity,
                           created_by=actor_id, allocation_scoped=True)
                append_event(connection, actor_id=actor_id, action="allocation.restocked",
                             resource_type="allocation", resource_id=allocation_id,
                             detail={"quantity": quantity}, occurred_at=self._now())
                return "allocation", allocation_id, {"allocation_id": allocation_id,
                                                      "restocked_quantity": quantity}

            return self._idempotent(connection, request_id=request_id,
                                    action="restock_returned", payload=payload, create=create)

    def register_publication(self, *, request_id: str, actor_id: str, allocation_id: str,
                             reference: str, consumed_quantity: float) -> Any:
        """登记发表结果，并固化发表当时的授权依据快照。"""

        payload = {"actor_id": actor_id, "allocation_id": allocation_id,
                   "reference": reference, "consumed_quantity": consumed_quantity}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            allocation = self._load_allocation(connection, allocation_id)
            reference = self._text(reference, "reference", 400)
            consumed_quantity = self._quantity(consumed_quantity, "consumed_quantity")

            def create() -> tuple[str, str, dict[str, Any]]:
                if not allocation["approved"]:
                    raise ConflictError("分装未批准，不能登记发表")
                balances = self._balances(connection, allocation["batch_id"], allocation_id)
                if consumed_quantity > balances["consumed"] + EPS:
                    raise ConflictError("登记的发表消耗量不能超过实际已消耗量")
                checks = json.loads(allocation["checks_json"])
                snapshot = {
                    "captured_at": self._now(),
                    "batch_id": allocation["batch_id"],
                    "lab_id": allocation["lab_id"],
                    "intended_use": allocation["intended_use"],
                    "permit": {"permit_id": allocation["permit_id"],
                               "version": allocation["permit_version"],
                               "terms_hash": allocation["permit_terms_hash"],
                               "terms": json.loads(allocation["permit_terms_json"])},
                    "mta": {"mta_id": allocation["mta_id"], "version": allocation["mta_version"],
                            "terms_hash": allocation["mta_terms_hash"],
                            "terms": json.loads(allocation["mta_terms_json"])},
                    "approval_checks": checks,
                    "consumed_quantity": consumed_quantity,
                }
                publication_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO publications(publication_id,allocation_id,reference,"
                    "consumed_quantity,authorization_snapshot_json,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (publication_id, allocation_id, reference, consumed_quantity,
                     canonical_json(snapshot), actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="publication.registered",
                             resource_type="publication", resource_id=publication_id,
                             detail={"allocation_id": allocation_id, "reference": reference,
                                     "permit": f"{allocation['permit_id']}:v{allocation['permit_version']}",
                                     "permit_terms_hash": allocation["permit_terms_hash"]},
                             occurred_at=self._now())
                return "publication", publication_id, {"publication_id": publication_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_publication", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 退出与撤回：只处置尚未消耗的份额
    # ------------------------------------------------------------------

    def withdraw_lab(self, *, request_id: str, actor_id: str, lab_id: str,
                     reason: str) -> Any:
        payload = {"actor_id": actor_id, "lab_id": lab_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            lab = self._load_lab(connection, lab_id)
            reason = self._text(reason, "reason", 400)

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE laboratories SET withdrawn=1, withdrawn_at=? WHERE lab_id=?",
                    (self._now(), lab_id),
                )
                affected = connection.execute(
                    "SELECT allocation_id, batch_id FROM allocations WHERE lab_id=? AND approved=1",
                    (lab_id,),
                ).fetchall()
                for row in affected:
                    connection.execute(
                        "UPDATE allocations SET lab_withdrawn=1 WHERE allocation_id=?",
                        (row["allocation_id"],),
                    )
                    balances = self._balances(connection, row["batch_id"], row["allocation_id"])
                    released = 0.0
                    # 从未发运的预留份额与已退回站内的份额尚在站内控制下，直接释放回
                    # 可分配池供其他实验室使用；在途和实验室持有的份额只能等待返还。
                    for bucket in ("reserved", "returned"):
                        if balances[bucket] > EPS:
                            self._move(connection, batch_id=row["batch_id"],
                                       allocation_id=row["allocation_id"],
                                       event_type="lab_withdrawn", from_bucket=bucket,
                                       to_bucket="available", quantity=balances[bucket],
                                       created_by=actor_id, detail={"reason": reason},
                                       allocation_scoped=True)
                            released += balances[bucket]
                    append_event(connection, actor_id=actor_id, action="allocation.lab_withdrawn",
                                 resource_type="allocation", resource_id=row["allocation_id"],
                                 detail={"lab_id": lab_id, "reason": reason, "released": released,
                                         "note": "在途/持有份额按返还流程回收，已消耗份额不回滚"},
                                 occurred_at=self._now())
                append_event(connection, actor_id=actor_id, action="lab.withdrawn",
                             resource_type="laboratory", resource_id=lab_id,
                             detail={"reason": reason, "affected_allocations": len(affected)},
                             occurred_at=self._now())
                return "laboratory", lab_id, {"lab_id": lab_id,
                                              "affected_allocations": len(affected)}

            return self._idempotent(connection, request_id=request_id,
                                    action="withdraw_lab", payload=payload, create=create)

    def withdraw_permit(self, *, request_id: str, actor_id: str, permit_id: str,
                        reason: str) -> Any:
        payload = {"actor_id": actor_id, "permit_id": permit_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            permit_id = self._identifier(permit_id, "permit_id")
            reason = self._text(reason, "reason", 400)

            def create() -> tuple[str, str, dict[str, Any]]:
                # 版本选择放在幂等查重之后，重复回放时版本已是 withdrawn，应返回原回执。
                row = connection.execute(
                    "SELECT * FROM permits WHERE permit_id=? AND status='active' "
                    "ORDER BY version DESC LIMIT 1",
                    (permit_id,),
                ).fetchone()
                if row is None:
                    raise NotFoundError("许可没有处于生效状态的版本")
                active_version = row["version"]
                batch_id = row["batch_id"]
                connection.execute(
                    "UPDATE permits SET status='withdrawn', withdrawn_reason=? "
                    "WHERE permit_id=? AND version=?",
                    (reason, permit_id, active_version),
                )
                # 站内整批可分配份额立即冻结，防止撤回后继续出口。
                batch_balances = self._balances(connection, batch_id)
                if batch_balances["available"] > EPS:
                    self._move(connection, batch_id=batch_id, allocation_id=None,
                               event_type="permit_withdrawn", from_bucket="available",
                               to_bucket="frozen", quantity=batch_balances["available"],
                               created_by=actor_id, document_ref=f"{permit_id}:v{active_version}",
                               detail={"reason": reason})
                allocations = connection.execute(
                    "SELECT allocation_id FROM allocations WHERE permit_id=? "
                    "AND permit_version=? AND approved=1",
                    (permit_id, active_version),
                ).fetchall()
                frozen_reserved = 0.0
                for item in allocations:
                    allocation_id = item["allocation_id"]
                    connection.execute(
                        "UPDATE allocations SET permit_withdrawn=1 WHERE allocation_id=?",
                        (allocation_id,),
                    )
                    balances = self._balances(connection, batch_id, allocation_id)
                    # 已预留未发运、已退件尚未再分配的份额立即冻结；
                    # 在途与实验室持有的份额物理上无法冻结，只能停止消耗并等待返还。
                    for bucket in ("reserved", "returned"):
                        if balances[bucket] > EPS:
                            self._move(connection, batch_id=batch_id, allocation_id=allocation_id,
                                       event_type="permit_withdrawn", from_bucket=bucket,
                                       to_bucket="frozen", quantity=balances[bucket],
                                       created_by=actor_id,
                                       document_ref=f"{permit_id}:v{active_version}",
                                       detail={"reason": reason}, allocation_scoped=True)
                            if bucket == "reserved":
                                frozen_reserved += balances[bucket]
                    append_event(connection, actor_id=actor_id,
                                 action="allocation.permit_withdrawn",
                                 resource_type="allocation", resource_id=allocation_id,
                                 detail={"permit": f"{permit_id}:v{active_version}",
                                         "reason": reason,
                                         "outstanding_return": balances["at_lab"] + balances["in_transit"]},
                                 occurred_at=self._now())
                append_event(connection, actor_id=actor_id, action="permit.withdrawn",
                             resource_type="permit", resource_id=f"{permit_id}:v{active_version}",
                             detail={"reason": reason, "affected_allocations": len(allocations),
                                     "frozen_reserved": frozen_reserved},
                             occurred_at=self._now())
                return ("permit", f"{permit_id}:v{active_version}",
                        {"permit_id": permit_id, "version": active_version,
                         "affected_allocations": len(allocations)})

            return self._idempotent(connection, request_id=request_id,
                                    action="withdraw_permit", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 查询：权利来源、责任方、剩余义务与总量核对
    # ------------------------------------------------------------------

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        row = self.database.connection.execute(
            "SELECT * FROM sample_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("采集批次不存在")
        result = dict(row)
        result["balances"] = self.reconcile_batch(batch_id)["balances"]
        return result

    def reconcile_batch(self, batch_id: str) -> dict[str, Any]:
        """证明可分配、在途、已消耗、待返还等数量始终等于批次总量。"""

        row = self.database.connection.execute(
            "SELECT total_quantity FROM sample_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("采集批次不存在")
        balances = self._balances(self.database.connection, batch_id)
        total_in_buckets = sum(balances.values())
        initial = float(row["total_quantity"])
        return {
            "batch_id": batch_id,
            "initial_total_quantity": initial,
            "balances": {key: round(value, 9) for key, value in balances.items()},
            "allocatable": round(balances["available"], 9),
            "in_transit": round(balances["in_transit"], 9),
            "consumed": round(balances["consumed"], 9),
            "awaiting_return": round(balances["at_lab"], 9),
            "returned_to_station": round(balances["returned"], 9),
            "reserved": round(balances["reserved"], 9),
            "frozen": round(balances["frozen"], 9),
            "balanced": abs(total_in_buckets - initial) <= EPS,
            "conservation_delta": round(total_in_buckets - initial, 9),
        }

    def list_allocations(self, batch_id: str | None = None,
                         lab_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM allocations WHERE 1=1"
        parameters: list[Any] = []
        if batch_id:
            query += " AND batch_id=?"
            parameters.append(batch_id)
        if lab_id:
            query += " AND lab_id=?"
            parameters.append(lab_id)
        query += " ORDER BY created_at, allocation_id"
        items = []
        for row in self.database.connection.execute(query, parameters):
            item = {key: row[key] for key in
                    ("allocation_id", "request_id", "batch_id", "lab_id", "intended_use",
                     "requested_quantity", "planned_consumption", "approved",
                     "permit_withdrawn", "lab_withdrawn", "created_at")}
            items.append(item)
        return items

    def list_shipments(self, allocation_id: str) -> list[dict[str, Any]]:
        rows = self.database.connection.execute(
            "SELECT * FROM shipments WHERE allocation_id=? ORDER BY created_at",
            (allocation_id,),
        ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["documents"] = json.loads(item.pop("documents_json"))
            items.append(item)
        return items

    def get_allocation(self, allocation_id: str) -> dict[str, Any]:
        """组装单份分装的权利来源、当前责任方与剩余义务完整报告。"""

        connection = self.database.connection
        row = connection.execute("SELECT * FROM allocations WHERE allocation_id=?",
                                 (allocation_id,)).fetchone()
        if row is None:
            raise NotFoundError("分装不存在")
        allocation = dict(row)
        batch = self._load_batch(connection, allocation["batch_id"])
        balances = self._balances(connection, allocation["batch_id"], allocation_id)
        shipments = self.list_shipments(allocation_id)
        active_shipment = next((s for s in reversed(shipments)
                                if s["status"] in ("in_transit", "customs_held")), None)
        permit_now_row = connection.execute(
            "SELECT status FROM permits WHERE permit_id=? AND version=?",
            (allocation["permit_id"], allocation["permit_version"]),
        ).fetchone()
        permit_status_now = permit_now_row["status"] if permit_now_row else "missing"
        publications = []
        for pub in connection.execute(
            "SELECT * FROM publications WHERE allocation_id=? ORDER BY created_at",
            (allocation_id,),
        ):
            publications.append({"publication_id": pub["publication_id"],
                                 "reference": pub["reference"],
                                 "consumed_quantity": pub["consumed_quantity"],
                                 "authorization_snapshot":
                                     json.loads(pub["authorization_snapshot_json"])})

        customs_held = active_shipment is not None and active_shipment["status"] == "customs_held"
        returning = active_shipment is not None and active_shipment["kind"] == "return"
        status = derive_status(balances, customs_held=customs_held, returning=returning)
        if allocation["permit_withdrawn"] and balances["at_lab"] > EPS:
            status = "permit_withdrawn_pending_return"
        elif allocation["lab_withdrawn"] and balances["at_lab"] > EPS:
            status = "lab_exited_pending_return"

        # 当前责任方：按物理所在量桶与运输状态推导。
        responsibilities: list[dict[str, Any]] = []
        if balances["reserved"] > EPS:
            responsibilities.append({"bucket": "reserved", "quantity": round(balances["reserved"], 9),
                                     "responsible_party": batch["custodian_organization_id"],
                                     "role": "站内保管方"})
        if balances["in_transit"] > EPS and active_shipment is not None:
            if active_shipment["status"] == "customs_held":
                party, role = "customs", "海关扣留中"
            else:
                party, role = active_shipment["carrier_ref"], "承运人"
            responsibilities.append({"bucket": "in_transit",
                                     "quantity": round(balances["in_transit"], 9),
                                     "responsible_party": party, "role": role,
                                     "shipment_id": active_shipment["shipment_id"],
                                     "kind": active_shipment["kind"]})
        if balances["at_lab"] > EPS:
            responsibilities.append({"bucket": "at_lab", "quantity": round(balances["at_lab"], 9),
                                     "responsible_party": allocation["lab_id"],
                                     "role": "接收实验室（退出合作中）" if allocation["lab_withdrawn"]
                                     else "接收实验室"})
        for bucket, role in (("returned", "站内保管方"), ("frozen", "站内保管方（冻结）")):
            if balances[bucket] > EPS:
                responsibilities.append({"bucket": bucket,
                                         "quantity": round(balances[bucket], 9),
                                         "responsible_party": batch["custodian_organization_id"],
                                         "role": role})

        permit_terms = json.loads(allocation["permit_terms_json"])
        # 待返还：所有离站且尚未消耗的份额（实验室持有或在途，含返还途中）。
        outstanding_return = balances["at_lab"] + balances["in_transit"]
        obligations = {
            "return_required": bool(permit_terms.get("requires_return", False)),
            "pending_return_quantity": round(outstanding_return, 9),
            "consumed_quantity": round(balances["consumed"], 9),
            "consumption_budget": allocation["planned_consumption"],
            "consumption_remaining": round(
                allocation["planned_consumption"] - balances["consumed"], 9
            ),
            "frozen_quantity": round(balances["frozen"], 9),
            "permit_withdrawn": bool(allocation["permit_withdrawn"]),
            "lab_withdrawn": bool(allocation["lab_withdrawn"]),
            "open_shipments": [
                {"shipment_id": s["shipment_id"], "kind": s["kind"], "status": s["status"],
                 "quantity": s["quantity"], "carrier_ref": s["carrier_ref"]}
                for s in shipments if s["status"] in ("in_transit", "customs_held")
            ],
        }
        checks = json.loads(allocation["checks_json"])
        return {
            "allocation_id": allocation_id,
            "request_id": allocation["request_id"],
            "status": status,
            "approved": bool(allocation["approved"]),
            "approved_at": allocation["approved_at"],
            "intended_use": allocation["intended_use"],
            "requested_quantity": allocation["requested_quantity"],
            "planned_consumption": allocation["planned_consumption"],
            "rights": {
                "batch_id": batch["batch_id"],
                "external_key": batch["external_key"],
                "owner_organization_id": batch["owner_organization_id"],
                "custodian_organization_id": batch["custodian_organization_id"],
                "permit": {"permit_id": allocation["permit_id"],
                           "version": allocation["permit_version"],
                           "terms_hash": allocation["permit_terms_hash"],
                           "status_then": "active",
                           "status_now": permit_status_now},
                "mta": {"mta_id": allocation["mta_id"], "version": allocation["mta_version"],
                        "terms_hash": allocation["mta_terms_hash"]},
                "checks": checks,
            },
            "balances": {key: round(value, 9) for key, value in balances.items()},
            "responsibilities": responsibilities,
            "obligations": obligations,
            "shipments": [{"shipment_id": s["shipment_id"], "kind": s["kind"],
                           "quantity": s["quantity"], "carrier_ref": s["carrier_ref"],
                           "status": s["status"]} for s in shipments],
            "publications": publications,
        }
