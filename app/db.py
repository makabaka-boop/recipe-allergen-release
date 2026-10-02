"""SQLite 连接管理与建表脚本。

设计要点：
- 配方版本(recipe_versions)一经写入不可变（服务端没有任何 UPDATE 内容的路径，
  只有状态机字段随审批推进）；组件行(component)带 material_labels 快照。
- 审批(approvals)只追加，不删除、不改写，召回/过期只会新增"审批失效"标记。
- 唯一约束 (recipe_version_id, reviewer_id) 从数据库层面阻止同一审核者重复确认。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from .config import DATABASE_PATH

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS allergens (
    code        TEXT PRIMARY KEY,          -- 如 WHEAT / PEANUT（GB 7718 致敏物）
    name        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviewers (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS materials (
    id          INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,      -- 原料编码
    name        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS material_allergens (
    material_id INTEGER NOT NULL REFERENCES materials(id),
    allergen_code TEXT NOT NULL REFERENCES allergens(code),
    PRIMARY KEY (material_id, allergen_code)
);

CREATE TABLE IF NOT EXISTS batches (
    id              INTEGER PRIMARY KEY,
    material_id     INTEGER NOT NULL REFERENCES materials(id),
    code            TEXT NOT NULL UNIQUE,  -- 供应批次号
    received_on     TEXT NOT NULL,         -- ISO 日期
    expires_on      TEXT NOT NULL,         -- ISO 日期
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'recalled', 'expired'))
);

CREATE TABLE IF NOT EXISTS recipes (
    id              INTEGER PRIMARY KEY,
    code            TEXT NOT NULL UNIQUE,  -- 配方编码
    name            TEXT NOT NULL,
    current_version_id INTEGER REFERENCES recipe_versions(id), -- 头指针，只增不改写旧版本
    revision        INTEGER NOT NULL DEFAULT 0                 -- 乐观锁/并发替换检测用
);

CREATE TABLE IF NOT EXISTS recipe_versions (
    id              INTEGER PRIMARY KEY,
    recipe_id       INTEGER NOT NULL REFERENCES recipes(id),
    version_no      INTEGER NOT NULL,
    created_on      TEXT NOT NULL,
    created_by      TEXT NOT NULL,        -- 起草/提出替换的审核者
    content_hash    TEXT NOT NULL,        -- 组件结构 + 快照的哈希（不可变快照指纹）
    status          TEXT NOT NULL DEFAULT 'in_approval'
                    CHECK (status IN ('in_approval', 'released')),
    needs_review    INTEGER NOT NULL DEFAULT 0, -- 召回传播命中后置 1；放行记录本身不动
    released_on     TEXT,
    note            TEXT NOT NULL DEFAULT '',
    UNIQUE (recipe_id, version_no)
);

CREATE TABLE IF NOT EXISTS components (
    id                  INTEGER PRIMARY KEY,
    recipe_version_id   INTEGER NOT NULL REFERENCES recipe_versions(id),
    position            INTEGER NOT NULL,
    -- kind=material：引用原料并钉住供应批次
    -- kind=subrecipe：引用更早的配方版本（child_version_id）
    kind                TEXT NOT NULL CHECK (kind IN ('material', 'subrecipe')),
    material_id         INTEGER REFERENCES materials(id),
    batch_id            INTEGER REFERENCES batches(id),
    child_version_id    INTEGER REFERENCES recipe_versions(id),
    qty                 TEXT NOT NULL DEFAULT '',
    material_labels     TEXT NOT NULL DEFAULT '[]', -- 创建版本时拍下来的过敏原快照 JSON
    UNIQUE (recipe_version_id, position)
);

CREATE TABLE IF NOT EXISTS approvals (
    id                  INTEGER PRIMARY KEY,
    recipe_version_id   INTEGER NOT NULL REFERENCES recipe_versions(id),
    reviewer_id         INTEGER NOT NULL REFERENCES reviewers(id),
    decided_on          TEXT NOT NULL,
    stale               INTEGER NOT NULL DEFAULT 0, -- 1 = 因配方变化/召回而失效，仅作标记
    stale_reason        TEXT NOT NULL DEFAULT '',
    UNIQUE (recipe_version_id, reviewer_id)
);

CREATE TABLE IF NOT EXISTS batch_recalls (
    id          INTEGER PRIMARY KEY,
    batch_id    INTEGER NOT NULL REFERENCES batches(id),
    reason      TEXT NOT NULL,
    recalled_on TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS proposals (
    id                      INTEGER PRIMARY KEY,
    recipe_id               INTEGER NOT NULL REFERENCES recipes(id),
    base_version_id         INTEGER NOT NULL REFERENCES recipe_versions(id),
    component_id            INTEGER REFERENCES components(id),
    new_material_id         INTEGER REFERENCES materials(id),
    new_batch_id            INTEGER REFERENCES batches(id),
    proposed_by             INTEGER NOT NULL REFERENCES reviewers(id),
    reason                  TEXT NOT NULL DEFAULT '',
    created_on              TEXT NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'open'
                            CHECK (status IN ('open', 'applied', 'rejected', 'superseded'))
);

CREATE INDEX IF NOT EXISTS idx_components_child ON components(child_version_id);
CREATE INDEX IF NOT EXISTS idx_components_batch ON components(batch_id);
CREATE INDEX IF NOT EXISTS idx_approvals_version ON approvals(recipe_version_id);
"""

# 轻量迁移：老库补列（SQLite 的 ALTER TABLE ADD COLUMN 幂等性靠忽略重复列错误实现）
MIGRATIONS = [
    "ALTER TABLE proposals ADD COLUMN resulting_version_id INTEGER REFERENCES recipe_versions(id)",
]


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(proposals)")}
    if "resulting_version_id" not in cols:
        conn.execute(MIGRATIONS[0])


def _connect() -> sqlite3.Connection:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        DATABASE_PATH, timeout=30, isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """请求级连接。"""
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def new_conn() -> sqlite3.Connection:
    """测试 / 脚本使用的独立连接。"""
    return _connect()


def init_db() -> None:
    conn = _connect()
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """串行化写事务（BEGIN IMMEDIATE），双人确认 / 并发替换的竞争在此解决。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise
