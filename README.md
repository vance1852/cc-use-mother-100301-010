# 办理科研样品出口许可与分配基础服务

本项目提供极地科考站服务端应用共用的基础能力，用于登记科考机构、站点、操作者和结构化业务资料，并提供角色权限、请求幂等、SQLite 事务与哈希串联审计。具体的物流、样品、能源、医疗和许可业务可在这些边界上扩展自己的状态、规则和接口。

`polar_station_foundation.allocations_service` 在这些边界上实现了一套**沉积物样品分配与出口许可系统**：

- 每份申请逐项审查八条硬性条款——采集批次的所有权与保管权、采样许可条款版本、用途限制、实验室资质、材料转移协议（MTA）、消耗预算、返还约定、运输文件；每一条独立记录通过/失败与证据，任一条未通过都不得签发对应分装。
- 只有全部条款通过后，批准动作才在同一 `BEGIN IMMEDIATE` 事务内**原子占用**数量；重复申请（同一 request_id）回放原回执，并发确认由事务串行化，双重保证不会超分配。
- 数量只在可分配（available）、预留（reserved）、在途（in_transit）、实验室持有（at_lab）、已消耗（consumed）、退回站内（returned）、冻结（frozen）七个量桶间等额转移，任意时刻对账都与批次总量守恒。
- 支持部分发运、海关扣留/放行/退件、实验室退出、许可撤回；这些事件只处置尚未消耗的份额。已发表结果固化当时的许可与 MTA 条款快照，许可规则变化只形成新版本（superseded/withdrawn），绝不篡改旧版本。
- `GET /allocations/{id}` 随时给出每份分装的**权利来源、当前责任方与剩余义务**；`GET /batches/{id}/reconcile` 证明可分配、在途、已消耗、待返还数量始终一致。

## 目录

- src/polar_station_foundation/：领域模型、SQLite 存储、权限服务、审计链、样品分配与许可规则、HTTP 路由和离线验收；
- tests/：基础规则、硬性条款、原子占用、并发不超分配、接口路由和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests -v

## 构建检查

    python3 -m compileall -q src tests

## 离线验收

    PYTHONPATH=src python3 -m polar_station_foundation.acceptance

验收命令会在临时 SQLite 数据库中登记科考机构、操作者、站点和业务资料，核对幂等回执与审计链，成功时输出一行 status 为 ok 的 JSON 并以退出码 0 结束。

## HTTP 服务

    PYTHONPATH=src python3 -m polar_station_foundation.api --database polar_station.sqlite3 --host 127.0.0.1 --port 8080

健康检查使用 GET /health。写入接口通过 X-Actor-Id 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。

### 样品分配与出口许可接口

登记事实：`POST /sample-batches`、`POST /permits/versions`、`POST /permits/withdraw`、`POST /laboratories`、`POST /laboratories/withdraw`、`POST /lab-qualifications`、`POST /lab-qualifications/revoke`、`POST /mtas`。

申请与签发：`POST /allocations/applications`（逐项记录八条硬性条款，失败也留痕但不占数量）、`POST /allocations/approve`（全部通过才原子占用）。

运输与处置：`POST /shipments`（部分发运）、`POST /shipments/events`（customs_hold / customs_release / deliver / customs_returned / return_received）、`POST /allocations/consumptions`、`POST /allocations/returns`、`POST /allocations/restock`、`POST /publications`。

查询：`GET /batches/{id}`、`GET /batches/{id}/reconcile`、`GET /allocations?batch_id=&lab_id=`、`GET /allocations/{id}`（权利来源、当前责任方、剩余义务）、`GET /allocations/{id}/shipments`。

所有写接口都要求 `request_id` 幂等键：同键同载荷返回首次结果（`replayed=true`），同键不同载荷返回 409。
