"""水位类型与推进。

类型凭证只落在 ``watermark_type_snapshots``：每次值班刷新把勘定的类型逐条收下。
不生成只读类型包，避免快照表与类型包两套形态并存。

页面水位、字段详情、清洗附页、进度水线四处都从 ``status()`` 的同一份解析结果取值，
不允许各自读 meta 里的类型副本。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.db import connect, get_db
from app.services.errors import ServiceError

SUPPORTED_TYPES = ("INT", "TIMESTAMP")
DEFAULT_OPERATOR = "值班"
LOCK_TTL_SECONDS = 60


class WatermarkError(ServiceError):
    """水位类型勘定失败；message 保持一句短话，直接上屏。"""

    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message, status_code)


# ---------------------------------------------------------------------------
# 类型解析
# ---------------------------------------------------------------------------

def _canonical_type(raw) -> str:
    return str(raw or "").strip().upper()


def _parse_int(value) -> tuple[int, str]:
    if isinstance(value, bool):
        raise ValueError
    if isinstance(value, int):
        n = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise ValueError
        n = int(value)
    elif isinstance(value, str):
        n = int(value.strip())
    else:
        raise ValueError
    return n, str(n)


def _parse_timestamp(value) -> tuple[datetime, str]:
    if isinstance(value, (bool, int, float)) or value is None:
        raise ValueError
    text = str(value).strip()
    if not text:
        raise ValueError
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    text = text.replace("T", " ")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed, parsed.strftime("%Y-%m-%d %H:%M:%S")


_PARSERS = {
    "INT": _parse_int,
    "TIMESTAMP": _parse_timestamp,
}


def parse_watermark(value, wm_type: str) -> tuple[object, str]:
    """按勘定类型解析水位；不可识别或值不符一律返回 ValueError。"""

    parser = _PARSERS.get(_canonical_type(wm_type))
    if parser is None:
        raise ValueError("unsupported type")
    return parser(value)


# ---------------------------------------------------------------------------
# 锁
# ---------------------------------------------------------------------------

def _acquire_lock(holder: str, db_path: str | None = None) -> bool:
    """用 ``BEGIN IMMEDIATE`` 抢写锁，避免两侧先读后写都拿到空锁位。"""

    conn = connect(db_path)
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT OR IGNORE INTO watermark_refresh_lock(id, holder, acquired_at) "
                "VALUES (1, '', NULL)"
            )
            cur = conn.execute(
                "UPDATE watermark_refresh_lock SET holder=?, acquired_at=datetime('now') "
                "WHERE id=1 AND (holder='' OR acquired_at IS NULL "
                "OR acquired_at < datetime('now', ?))",
                (holder, f"-{LOCK_TTL_SECONDS} seconds"),
            )
            conn.execute("COMMIT")
            return cur.rowcount == 1
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def _release_lock(holder: str, db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.execute(
            "UPDATE watermark_refresh_lock SET holder='', acquired_at=NULL "
            "WHERE id=1 AND holder=?",
            (holder,),
        )


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------

def _bindings(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT table_name, field_name FROM watermark_bindings ORDER BY table_name"
    ).fetchall()
    return [dict(r) for r in rows]


def _binding(conn, table_name: str | None) -> dict:
    if table_name:
        row = conn.execute(
            "SELECT table_name, field_name FROM watermark_bindings WHERE table_name=?",
            (table_name,),
        ).fetchone()
        if not row:
            raise WatermarkError("未绑定水位列")
        return dict(row)
    rows = _bindings(conn)
    if not rows:
        raise WatermarkError("未绑定水位列")
    return rows[0]


def _resolve(conn, binding: dict) -> dict:
    """契约是类型唯一权威；清洗类型只作为待收拢的声明值。"""

    table = binding["table_name"]
    field = binding["field_name"]

    live = conn.execute(
        "SELECT * FROM schema_contracts WHERE table_name=? AND status='现行'",
        (table,),
    ).fetchone()
    if not live:
        raise WatermarkError("无现行契约")

    field_row = conn.execute(
        "SELECT field_name, field_type FROM contract_fields "
        "WHERE contract_id=? AND field_name=?",
        (live["id"], field),
    ).fetchone()
    if not field_row:
        raise WatermarkError("现行契约无该水位字段")

    contract_type = _canonical_type(field_row["field_type"])
    if contract_type not in SUPPORTED_TYPES:
        raise WatermarkError("契约水位类型不可识别")

    wash_row = conn.execute(
        "SELECT field_type FROM watermark_wash_config WHERE table_name=?",
        (table,),
    ).fetchone()
    wash_type = _canonical_type(wash_row["field_type"]) if wash_row else ""
    if wash_type and wash_type not in SUPPORTED_TYPES:
        raise WatermarkError("清洗水位类型不可识别")

    return {
        "table_name": table,
        "field_name": field,
        "contract_id": live["id"],
        "contract_version": live["version_no"],
        "contract_type": contract_type,
        "wash_type": wash_type or None,
        "resolved_type": contract_type,
    }


def _latest_snapshot(conn, table: str):
    return conn.execute(
        "SELECT * FROM watermark_type_snapshots WHERE table_name=? "
        "ORDER BY id DESC LIMIT 1",
        (table,),
    ).fetchone()


def _state(conn, table: str):
    return conn.execute(
        "SELECT value, updated_at FROM watermark_state WHERE table_name=?",
        (table,),
    ).fetchone()


def _last_run(conn, table: str):
    return conn.execute(
        "SELECT * FROM watermark_refresh_runs WHERE table_name=? "
        "ORDER BY id DESC LIMIT 1",
        (table,),
    ).fetchone()


def _verify(state_row, wm_type: str | None) -> dict:
    """清洗附页与进度水线共用这一个校验，保证两处收口一致。

    类型只认快照；没有快照就不认任何推断出来的类型。
    """

    if not wm_type:
        return {"ok": False, "value": state_row["value"] if state_row else None,
                "canonical": None, "reason": "未刷新"}
    if not state_row:
        return {"ok": False, "value": None, "canonical": None, "reason": "无水位"}
    raw = state_row["value"]
    try:
        _, canonical = parse_watermark(raw, wm_type)
    except (TypeError, ValueError):
        return {"ok": False, "value": raw, "canonical": None, "reason": "类型不符"}
    return {"ok": True, "value": raw, "canonical": canonical, "reason": ""}


def _snapshot_dict(row) -> dict:
    return dict(row) if row else {}


def _run_dict(row) -> dict:
    return dict(row) if row else {}


def _item(conn, binding: dict) -> dict:
    table = binding["table_name"]
    try:
        resolved = _resolve(conn, binding)
    except WatermarkError as exc:
        return {
            "table_name": table,
            "field_name": binding["field_name"],
            "error": exc.message,
            "chip": "未刷新",
            "same_source": False,
        }

    snapshot = _latest_snapshot(conn, table)
    state_row = _state(conn, table)
    wash_row = conn.execute(
        "SELECT field_type, updated_at FROM watermark_wash_config WHERE table_name=?",
        (table,),
    ).fetchone()

    snap_type = _canonical_type(snapshot["resolved_type"]) if snapshot else None
    wash_effective = _canonical_type(wash_row["field_type"]) if wash_row else None
    page_type = snap_type  # 页面水位只认快照，不另读一份
    # 没有快照就没有可信类型；拿契约类型凑一个「假类型」正是要停的行为。
    verified = _verify(state_row, snap_type)

    same_source = bool(
        snapshot
        and page_type == resolved["contract_type"] == wash_effective
    )

    snapshot_no = snapshot["snapshot_no"] if snapshot else None
    item = {
        "table_name": table,
        "field_name": binding["field_name"],
        "chip": snapshot_no or "未刷新",
        "snapshot_no": snapshot_no,
        "snapshot_at": snapshot["created_at"] if snapshot else None,
        "contract": {
            "contract_id": resolved["contract_id"],
            "version_no": resolved["contract_version"],
            "field_type": resolved["contract_type"],
        },
        "field": {
            "table_name": table,
            "field_name": binding["field_name"],
            "field_type": resolved["contract_type"],
            "contract_version": resolved["contract_version"],
            "source": "契约",
        },
        "wash": {
            "declared_type": wash_effective,
            "effective_type": wash_effective,
            "resolved_type": wash_effective,
            "converged": wash_effective == resolved["contract_type"],
            "updated_at": wash_row["updated_at"] if wash_row else None,
            "last_run": _run_dict(_last_run(conn, table)),
            "verified": verified,
        },
        "progress": {
            "type": page_type,
            "current": verified["value"],
            "canonical": verified["canonical"],
            "verified": verified,
            "snapshot_no": snapshot_no,
            "same_source": same_source,
        },
        "page": {
            "type": page_type,
            "watermark": verified["value"],
            "canonical": verified["canonical"],
            "snapshot_no": snapshot_no,
        },
        "sources": {
            "page": page_type,
            "contract": resolved["contract_type"],
            "snapshot": snap_type,
            "wash": wash_effective,
        },
        "snapshot": _snapshot_dict(snapshot),
        "same_source": same_source,
    }
    return item


def status(table_name: str | None = None, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        bindings = _bindings(conn)
        if table_name:
            bindings = [b for b in bindings if b["table_name"] == table_name]
        items = [_item(conn, b) for b in bindings]
        snapshots = [
            dict(r)
            for r in conn.execute(
                "SELECT snapshot_no, table_name, field_name, contract_version, "
                "contract_type, wash_type, resolved_type, operator, created_at "
                "FROM watermark_type_snapshots ORDER BY id DESC LIMIT 20"
            ).fetchall()
        ]
        return {
            "items": items,
            "snapshots": snapshots,
            "chip": items[0]["chip"] if items else "未刷新",
        }


# ---------------------------------------------------------------------------
# 刷新（收拢类型）
# ---------------------------------------------------------------------------

def _record_failed_run(table: str, operator: str, message: str, db_path: str | None) -> None:
    # 失败记录用独立事务落库，避免跟被回滚的刷新事务一起消失。
    with get_db(db_path) as conn:
        conn.execute(
            "INSERT INTO watermark_refresh_runs(snapshot_no, table_name, operator, "
            "outcome, message) VALUES (NULL, ?, ?, '失败', ?)",
            (table, operator, message),
        )


def refresh(
    table_name: str | None = None,
    operator: str = DEFAULT_OPERATOR,
    db_path: str | None = None,
) -> dict:
    """值班点一次刷新：服务在单个事务里收拢全部（或指定）水位列的类型。

    失败不留下半份快照，并回一句短话。
    """

    holder = f"{operator}:{uuid.uuid4().hex[:8]}"
    if not _acquire_lock(holder, db_path):
        raise WatermarkError("刷新进行中，请稍后", 409)

    table = table_name or ""
    try:
        with get_db(db_path) as conn:
            if table_name:
                bindings = [_binding(conn, table_name)]
            else:
                bindings = _bindings(conn)
                if not bindings:
                    raise WatermarkError("未绑定水位列")
            table = bindings[0]["table_name"]

            refreshed = []
            items = []
            for binding in bindings:
                resolved = _resolve(conn, binding)
                table = binding["table_name"]
                snapshot_no = (
                    "WMS-"
                    + datetime.now().strftime("%Y%m%d%H%M%S%f")
                    + "-"
                    + uuid.uuid4().hex[:4]
                )
                conn.execute(
                    "INSERT INTO watermark_type_snapshots(snapshot_no, table_name, "
                    "field_name, contract_id, contract_version, contract_type, "
                    "wash_type, resolved_type, operator) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        snapshot_no,
                        table,
                        binding["field_name"],
                        resolved["contract_id"],
                        resolved["contract_version"],
                        resolved["contract_type"],
                        resolved["wash_type"] or "",
                        resolved["resolved_type"],
                        operator,
                    ),
                )
                conn.execute(
                    "INSERT INTO watermark_wash_config(table_name, field_type, updated_at) "
                    "VALUES (?,?,datetime('now')) "
                    "ON CONFLICT(table_name) DO UPDATE SET "
                    "field_type=excluded.field_type, updated_at=excluded.updated_at",
                    (table, resolved["resolved_type"]),
                )
                conn.execute(
                    "INSERT INTO watermark_refresh_runs(snapshot_no, table_name, "
                    "operator, outcome, message) VALUES (?,?,?,?,?)",
                    (snapshot_no, table, operator, "成功", "已收拢类型"),
                )
                refreshed.append({
                    "snapshot_no": snapshot_no,
                    "table_name": table,
                    "field_name": binding["field_name"],
                    "contract_type": resolved["contract_type"],
                    "wash_type_before": resolved["wash_type"],
                    "resolved_type": resolved["resolved_type"],
                    "operator": operator,
                })
                items.append(_item(conn, binding))
    except WatermarkError as exc:
        _record_failed_run(table, operator, exc.message, db_path)
        raise
    finally:
        _release_lock(holder, db_path)

    full = status(db_path=db_path)
    return {
        "refreshed": refreshed[0],
        "refreshed_items": refreshed,
        "item": items[0] if items else None,
        "status": full,
    }


# ---------------------------------------------------------------------------
# 推进（按勘定类型）
# ---------------------------------------------------------------------------

def advance(
    next_val,
    table_name: str | None = None,
    db_path: str | None = None,
) -> dict:
    with get_db(db_path) as conn:
        binding = _binding(conn, table_name)
        table = binding["table_name"]
        resolved = _resolve(conn, binding)
        snapshot = _latest_snapshot(conn, table)
        if not snapshot:
            raise WatermarkError("尚未刷新水位类型，暂不能推进", 409)

        snap_type = _canonical_type(snapshot["resolved_type"])
        if snap_type != resolved["resolved_type"]:
            raise WatermarkError("契约水位类型已变更，请先刷新", 409)

        # 清洗侧必须已按快照收拢；否则按旧类型推进就会重复或漏扫。
        wash_row = conn.execute(
            "SELECT field_type FROM watermark_wash_config WHERE table_name=?",
            (table,),
        ).fetchone()
        if not wash_row or _canonical_type(wash_row["field_type"]) != snap_type:
            raise WatermarkError("清洗水位类型未收拢，请先刷新", 409)

        try:
            _, next_canonical = parse_watermark(next_val, snap_type)
        except (TypeError, ValueError):
            raise WatermarkError("水位值不符合勘定类型", 409)

        state_row = _state(conn, table)
        current_canonical = None
        if state_row:
            try:
                _, current_canonical = parse_watermark(state_row["value"], snap_type)
            except (TypeError, ValueError):
                raise WatermarkError("现水位与类型不符，暂不能推进", 409)

        if current_canonical is not None and next_canonical <= current_canonical:
            raise WatermarkError("水位重复或回退", 409)

        conn.execute(
            "INSERT INTO watermark_state(table_name, value, updated_at) "
            "VALUES (?,?,datetime('now')) "
            "ON CONFLICT(table_name) DO UPDATE SET "
            "value=excluded.value, updated_at=excluded.updated_at",
            (table, next_canonical),
        )
        item = _item(conn, binding)
        result = {
            "ok": True,
            "table_name": table,
            "type": snap_type,
            "from": current_canonical,
            "to": next_canonical,
            "snapshot_no": snapshot["snapshot_no"],
            "item": item,
        }
    # 事务提交后再取整台状态，避免另开连接读到推进前的旧水位。
    result["status"] = status(db_path=db_path)
    return result
