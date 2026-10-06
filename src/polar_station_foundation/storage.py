"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sample_batches (
    batch_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    external_key TEXT NOT NULL,
    owner_organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    custodian_organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    total_quantity REAL NOT NULL CHECK(total_quantity > 0),
    unit TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(site_id, external_key)
);
CREATE TABLE IF NOT EXISTS permits (
    permit_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    batch_id TEXT NOT NULL REFERENCES sample_batches(batch_id),
    terms_json TEXT NOT NULL,
    terms_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','superseded','withdrawn')),
    superseded_by_permit_id TEXT,
    superseded_by_version INTEGER,
    withdrawn_reason TEXT,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    effective_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(permit_id, version)
);
CREATE TABLE IF NOT EXISTS laboratories (
    lab_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    country_code TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    withdrawn INTEGER NOT NULL DEFAULT 0 CHECK(withdrawn IN (0, 1)),
    withdrawn_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lab_qualifications (
    qualification_id TEXT PRIMARY KEY,
    lab_id TEXT NOT NULL REFERENCES laboratories(lab_id),
    scope TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mta_agreements (
    mta_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    provider_organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    recipient_lab_id TEXT NOT NULL REFERENCES laboratories(lab_id),
    terms_json TEXT NOT NULL,
    terms_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','terminated')),
    effective_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(mta_id, version)
);
CREATE TABLE IF NOT EXISTS allocations (
    allocation_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    batch_id TEXT NOT NULL REFERENCES sample_batches(batch_id),
    lab_id TEXT NOT NULL REFERENCES laboratories(lab_id),
    permit_id TEXT,
    permit_version INTEGER CHECK(permit_version IS NULL OR permit_version >= 1),
    permit_terms_hash TEXT NOT NULL,
    permit_terms_json TEXT NOT NULL,
    mta_id TEXT,
    mta_version INTEGER CHECK(mta_version IS NULL OR mta_version >= 1),
    mta_terms_hash TEXT NOT NULL,
    mta_terms_json TEXT NOT NULL,
    intended_use TEXT NOT NULL,
    requested_quantity REAL NOT NULL CHECK(requested_quantity > 0),
    planned_consumption REAL NOT NULL CHECK(planned_consumption >= 0),
    documents_json TEXT NOT NULL DEFAULT '{}',
    checks_json TEXT NOT NULL,
    approved INTEGER NOT NULL CHECK(approved IN (0, 1)),
    permit_withdrawn INTEGER NOT NULL DEFAULT 0 CHECK(permit_withdrawn IN (0, 1)),
    lab_withdrawn INTEGER NOT NULL DEFAULT 0 CHECK(lab_withdrawn IN (0, 1)),
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    approved_at TEXT,
    FOREIGN KEY(permit_id, permit_version) REFERENCES permits(permit_id, version),
    FOREIGN KEY(mta_id, mta_version) REFERENCES mta_agreements(mta_id, version)
);
CREATE TABLE IF NOT EXISTS quantity_movements (
    movement_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES sample_batches(batch_id),
    allocation_id TEXT REFERENCES allocations(allocation_id),
    event_type TEXT NOT NULL,
    from_bucket TEXT NOT NULL,
    to_bucket TEXT NOT NULL,
    quantity REAL NOT NULL CHECK(quantity > 0),
    balance_after_json TEXT NOT NULL,
    document_ref TEXT,
    detail_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shipments (
    shipment_id TEXT PRIMARY KEY,
    allocation_id TEXT NOT NULL REFERENCES allocations(allocation_id),
    kind TEXT NOT NULL CHECK(kind IN ('outbound','return')),
    quantity REAL NOT NULL CHECK(quantity > 0),
    carrier_ref TEXT,
    documents_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('in_transit','customs_held','delivered',
                                        'released','returned_to_station')),
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publications (
    publication_id TEXT PRIMARY KEY,
    allocation_id TEXT NOT NULL REFERENCES allocations(allocation_id),
    reference TEXT NOT NULL,
    consumed_quantity REAL NOT NULL CHECK(consumed_quantity >= 0),
    authorization_snapshot_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)
        # 单连接在多线程 HTTP 服务上共享，写事务必须串行，避免事务嵌套与交错提交。
        self._transaction_lock = threading.RLock()

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        with self._transaction_lock:
            self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield self.connection
            except Exception:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
