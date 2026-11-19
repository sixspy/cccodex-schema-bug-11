import asyncio

import pytest


async def _status(authed):
    return await (await authed.get("/api/watermarks")).json()


async def test_refresh_folds_type_and_converges(authed):
    data = await _status(authed)
    item = data["items"][0]
    # 刷新前：清洗侧仍声明 INT，与契约 TIMESTAMP 不同源，页面水位不得报假通过。
    assert item["sources"]["contract"] == "TIMESTAMP"
    assert item["sources"]["wash"] == "INT"
    assert item["same_source"] is False
    assert item["progress"]["verified"]["ok"] is False

    refreshed = await authed.post(
        "/api/watermarks/refresh", json={"table": "dw_payments_d"}
    )
    assert refreshed.status == 200
    body = await refreshed.json()
    assert body["refreshed"]["resolved_type"] == "TIMESTAMP"
    assert body["refreshed"]["snapshot_no"].startswith("WMS-")

    data = await _status(authed)
    item = data["items"][0]
    assert item["same_source"] is True
    assert item["sources"]["page"] == item["sources"]["contract"] == "TIMESTAMP"
    assert item["chip"] == item["snapshot_no"]
    # 四处读到同一快照编号与同一类型
    assert item["page"]["snapshot_no"] == item["snapshot_no"]
    assert item["progress"]["snapshot_no"] == item["snapshot_no"]
    assert item["field"]["field_type"] == "TIMESTAMP"
    assert item["wash"]["effective_type"] == "TIMESTAMP"
    assert item["progress"]["type"] == "TIMESTAMP"


async def test_advance_rejects_int_coercion_and_regression(authed):
    await authed.post("/api/watermarks/refresh", json={"table": "dw_payments_d"})

    ok = await authed.post(
        "/api/watermark/advance",
        json={"table": "dw_payments_d", "next": "2026-11-19 08:00:00"},
    )
    assert ok.status == 200
    body = await ok.json()
    assert body["type"] == "TIMESTAMP"
    assert body["to"] == "2026-11-19 08:00:00"

    # 整型不再被 int() 静默强转成时间戳形态的水位
    bad = await authed.post(
        "/api/watermark/advance", json={"table": "dw_payments_d", "next": 500}
    )
    assert bad.status == 409

    # 重复或回退必须挡住，否则会漏扫
    back = await authed.post(
        "/api/watermark/advance",
        json={"table": "dw_payments_d", "next": "2026-11-18 00:00:00"},
    )
    assert back.status == 409


async def test_advance_requires_refreshed_type(authed):
    resp = await authed.post(
        "/api/watermark/advance",
        json={"table": "dw_payments_d", "next": "2026-11-19 08:00:00"},
    )
    assert resp.status == 409
    assert "刷新" in (await resp.json())["error"]


async def test_field_detail_uses_same_snapshot(authed):
    refreshed = await (
        await authed.post("/api/watermarks/refresh", json={"table": "dw_payments_d"})
    ).json()
    snap = refreshed["refreshed"]["snapshot_no"]

    items = (await (await authed.get("/api/contracts")).json())["items"]
    payments = next(c for c in items if c["table_name"] == "dw_payments_d")
    detail = await (await authed.get(f"/api/contracts/{payments['id']}/fields")).json()
    paid = next(f for f in detail["fields"] if f["field_name"] == "paid_at")
    assert paid["field_type"] == "TIMESTAMP"
    assert paid["watermark"]["type"] == "TIMESTAMP"
    assert paid["watermark"]["snapshot_no"] == snap


async def test_failed_refresh_returns_short_message(aiohttp_client, tmp_path):
    from app.db import get_db
    from app.main import create_app

    db = str(tmp_path / "empty.db")
    app = create_app(db)
    client = await aiohttp_client(app)
    await client.post("/api/login", json={"username": "admin", "password": "123456"})
    with get_db(db) as conn:
        conn.execute("DELETE FROM watermark_bindings")

    resp = await client.post("/api/watermarks/refresh", json={})
    assert resp.status == 409
    text = (await resp.json())["error"]
    assert text == "未绑定水位列"
    # 失败也要在本地库留下可见记录
    with get_db(db) as conn:
        runs = conn.execute(
            "SELECT outcome, message FROM watermark_refresh_runs"
        ).fetchall()
    assert runs and runs[-1]["outcome"] == "失败"


async def test_concurrent_refresh_writes_once(aiohttp_client, tmp_path, monkeypatch):
    import threading

    from app.db import get_db
    from app.main import create_app
    from app.services import watermarks

    db = str(tmp_path / "race.db")
    app = create_app(db)
    client = await aiohttp_client(app)
    await client.post("/api/login", json={"username": "admin", "password": "123456"})

    entered = threading.Event()
    release = threading.Event()
    original = watermarks._resolve

    def slow_resolve(conn, binding):
        entered.set()
        release.wait(5)
        return original(conn, binding)

    monkeypatch.setattr(watermarks, "_resolve", slow_resolve)

    def first():
        try:
            return watermarks.refresh("dw_payments_d", "左值班", db)
        except Exception as exc:  # pragma: no cover - 仅用于收集结果
            return exc

    fut = asyncio.get_running_loop().run_in_executor(None, first)
    assert entered.wait(5), "左侧值班未进入刷新"

    # 左侧持锁期间，右侧值班同时点刷新必须被挡住，不得再写一份类型。
    try:
        with pytest.raises(watermarks.WatermarkError) as excinfo:
            watermarks.refresh("dw_payments_d", "右值班", db)
        assert excinfo.value.status_code == 409
    finally:
        release.set()

    result = await fut
    assert isinstance(result, dict)
    assert result["refreshed"]["snapshot_no"].startswith("WMS-")

    with get_db(db) as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM watermark_type_snapshots"
        ).fetchone()["n"]
    assert count == 1


async def test_refresh_without_table_collects_all_bindings(authed):
    body = await (
        await authed.post("/api/watermarks/refresh", json={"operator": "值班"})
    ).json()
    assert len(body["refreshed_items"]) == 1
    assert body["refreshed"]["resolved_type"] == "TIMESTAMP"
    data = await _status(authed)
    assert all(it["same_source"] for it in data["items"])


async def test_duplicate_click_over_http_keeps_single_type(authed):
    """页面上的刷新芯片取本地库快照；连点两次只应多一份凭证。"""

    first = await authed.post(
        "/api/watermarks/refresh", json={"table": "dw_payments_d", "operator": "左值班"}
    )
    assert first.status == 200
    second = await authed.post(
        "/api/watermarks/refresh", json={"table": "dw_payments_d", "operator": "右值班"}
    )
    assert second.status == 200

    data = await (await authed.get("/api/watermarks")).json()
    item = data["items"][0]
    latest = data["snapshots"][0]
    # 水位台芯片 = 库中最新快照编号；两套类型不并存，全部是契约解析出的同一类型。
    assert item["chip"] == latest["snapshot_no"]
    assert {s["resolved_type"] for s in data["snapshots"]} == {"TIMESTAMP"}
    assert item["wash"]["effective_type"] == "TIMESTAMP"


@pytest.mark.parametrize("value,expected", [
    ("2026-11-19T08:00:00Z", "2026-11-19 08:00:00"),
    ("2026-11-19 08:00:00", "2026-11-19 08:00:00"),
])
def test_timestamp_parsing_accepts_iso(value, expected):
    from app.services.watermarks import parse_watermark

    _, canonical = parse_watermark(value, "TIMESTAMP")
    assert canonical == expected
