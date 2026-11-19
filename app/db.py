import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Config


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_db(db_path: str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_contracts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  version_no INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('现行','草稿','停用')),
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(table_name, version_no)
);

CREATE TABLE IF NOT EXISTS contract_fields (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contract_id INTEGER NOT NULL REFERENCES schema_contracts(id),
  field_name TEXT NOT NULL,
  field_type TEXT NOT NULL,
  UNIQUE(contract_id, field_name)
);

CREATE TABLE IF NOT EXISTS catalog_tables (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL UNIQUE,
  live_version INTEGER NOT NULL,
  registered_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS apply_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contract_id INTEGER NOT NULL,
  table_name TEXT NOT NULL,
  from_version INTEGER,
  to_version INTEGER NOT NULL,
  result TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reconcile_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of TEXT NOT NULL,
  matched INTEGER NOT NULL,
  drifted INTEGER NOT NULL,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- 水位类型凭证的唯一落库形态：快照表逐次收下刷新结果。
-- 不生成只读类型包，避免两套凭证并存。
CREATE TABLE IF NOT EXISTS watermark_type_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  snapshot_no TEXT NOT NULL UNIQUE,
  table_name TEXT NOT NULL,
  field_name TEXT NOT NULL,
  contract_id INTEGER NOT NULL,
  contract_version INTEGER NOT NULL,
  contract_type TEXT NOT NULL,
  wash_type TEXT NOT NULL,
  resolved_type TEXT NOT NULL,
  operator TEXT NOT NULL DEFAULT '值班',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS watermark_refresh_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  snapshot_no TEXT,
  table_name TEXT NOT NULL,
  operator TEXT NOT NULL DEFAULT '值班',
  outcome TEXT NOT NULL,
  message TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS watermark_bindings (
  table_name TEXT PRIMARY KEY,
  field_name TEXT NOT NULL
);

-- 清洗侧当前声明的类型。刷新后必须与契约解析结果一致。
CREATE TABLE IF NOT EXISTS watermark_wash_config (
  table_name TEXT PRIMARY KEY,
  field_type TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS watermark_state (
  table_name TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 刷新互斥：单行锁。左右值班同时点刷新时只允许一方收拢类型。
CREATE TABLE IF NOT EXISTS watermark_refresh_lock (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  holder TEXT NOT NULL DEFAULT '',
  acquired_at TEXT
);
"""

SEED = [
    ("dw_orders_d", 1, "现行", "初始现行", [("order_id", "INT"), ("amount", "DECIMAL"), ("biz_date", "DATE")]),
    ("dw_orders_d", 2, "草稿", "拟删 amount 改币种", [("order_id", "INT"), ("currency", "STRING"), ("biz_date", "DATE")]),
    ("dw_payments_d", 1, "现行", "支付现行", [("pay_id", "INT"), ("order_id", "INT"), ("paid_at", "TIMESTAMP")]),
]

# 水位列绑定：清洗推进列与契约字段的对应关系。
WATERMARK_BINDINGS = [
    ("dw_payments_d", "paid_at"),
]

# 清洗侧在刷新前声明的类型（与契约不一致即为待收拢的缺陷现场）。
WATERMARK_WASH_DECLARED = [
    ("dw_payments_d", "INT"),
]


def init_db(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA)
        # 旧版把水位类型散在 meta 两个键里，属于第二套凭证形态，一律清掉。
        conn.execute(
            "DELETE FROM meta WHERE key IN ('wm_type_contract','wm_type_wash','wm_value')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO watermark_refresh_lock(id, holder, acquired_at) "
            "VALUES (1, '', NULL)"
        )
        if conn.execute("SELECT value FROM meta WHERE key='seeded'").fetchone():
            return
        for table, ver, status, note, fields in SEED:
            cur = conn.execute(
                "INSERT INTO schema_contracts(table_name, version_no, status, note) VALUES (?,?,?,?)",
                (table, ver, status, note),
            )
            cid = cur.lastrowid
            for fname, ftype in fields:
                conn.execute(
                    "INSERT INTO contract_fields(contract_id, field_name, field_type) VALUES (?,?,?)",
                    (cid, fname, ftype),
                )
            if status == "现行":
                conn.execute(
                    "INSERT OR REPLACE INTO catalog_tables(table_name, live_version) VALUES (?,?)",
                    (table, ver),
                )
        for table, field in WATERMARK_BINDINGS:
            conn.execute(
                "INSERT OR IGNORE INTO watermark_bindings(table_name, field_name) VALUES (?,?)",
                (table, field),
            )
        for table, declared in WATERMARK_WASH_DECLARED:
            conn.execute(
                "INSERT OR IGNORE INTO watermark_wash_config(table_name, field_type) "
                "VALUES (?,?)",
                (table, declared),
            )
        conn.execute(
            "INSERT OR IGNORE INTO watermark_state(table_name, value) VALUES (?,?)",
            ("dw_payments_d", "2026-11-18 00:00:00"),
        )
        conn.execute("INSERT INTO meta(key, value) VALUES ('seeded','1')")
