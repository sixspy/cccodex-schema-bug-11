from app.db import get_db


def contract_type(db_path: str | None = None) -> str:
    with get_db(db_path) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key='wm_type_contract'").fetchone()
        return row["value"] if row else "INT"


def wash_type(db_path: str | None = None) -> str:
    with get_db(db_path) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key='wm_type_wash'").fetchone()
        return row["value"] if row else "INT"


def advance(next_val, db_path: str | None = None):
    # silent: still coerce via int path
    with get_db(db_path) as conn:
        cur = conn.execute("SELECT value FROM meta WHERE key='wm_value'").fetchone()
        try:
            now = int(cur["value"]) if cur else 0
            nxt = int(next_val)
        except (TypeError, ValueError):
            return {"ok": False, "kept": cur["value"] if cur else "0"}
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('wm_value', ?)", (str(nxt),)
        )
        return {"ok": True, "from": now, "to": nxt, "type": wash_type(db_path)}
