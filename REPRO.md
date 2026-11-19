# 复现纸：水位列改时间戳后清洗仍按整型推进

题目：`schema-bug-11`。水位列已从整型改成时间戳，清洗仍按整型推进水位，
导致入库重复或漏扫。本纸记录现场、改动与核对要点。

## 一、缺陷现场（改前）

| 位置 | 现象 |
|------|------|
| `app/db.py` | `meta` 里 `wm_type_contract='TIMESTAMP'`、`wm_type_wash='INT'` 两个键各说各话 |
| `app/services/watermarks.py` | `advance()` 用 `int(next_val)` 强转，失败时静默返回 `{"ok": false, "kept": ...}` |
| 页面 | 没有水位台 / 字段详情 / 清洗附页 / 进度水线，四处无从对照 |
| 凭证 | 只有 `meta` 两个类型键，没有逐次刷新记录，页上无芯片 |

根因：**类型有两个来源（契约一份、清洗一份），推进只认整型路径**。
只要两边类型不一致，推进就会把时间戳截成整数或静默丢弃，水位停走，
清洗窗口重叠 → 重复；跳过 → 漏扫。

## 二、类型凭证的选边

只选**独立水位类型快照表**这一侧，落地为 `watermark_type_snapshots`：

- 每次值班刷新产生一行，含 `snapshot_no`、契约版本、契约类型、
  刷新前清洗声明、`resolved_type`、操作人、时间；
- 刷新同时把 `watermark_wash_config` 收拢到 `resolved_type`；
- **不生成只读类型包**，也不保留 `meta` 里的类型键（初始化时显式删除）。
  两套落库形态不并存。

类型解析的唯一权威是现行契约里的水位字段；清洗侧只是待收拢的声明值。

## 三、改动要点

1. `app/db.py`
   - 新增 `watermark_type_snapshots`（凭证）、`watermark_refresh_runs`（本地可查的刷新流水）、
     `watermark_bindings`、`watermark_wash_config`、`watermark_state`、
     `watermark_refresh_lock`（单行锁）。
   - 初始化删除 `wm_type_contract` / `wm_type_wash` / `wm_value` 三个旧键。
2. `app/services/watermarks.py`
   - `parse_watermark(value, type)`：`INT` 与 `TIMESTAMP` 分开解析，
     时间戳统一归一到 `YYYY-MM-DD HH:MM:SS`（接受 `T`、`Z`、时区）。
   - `refresh()`：加锁 → 解析类型 → 写快照 → 收拢清洗类型 → 记流水。
     失败抛一句短话并落 `失败` 流水。
   - `advance()`：必须已有快照，且快照类型 = 契约类型 = 清洗类型；
     值必须符合勘定类型；新水位必须严格大于现水位（挡住重复与回退）。
   - `status()`：水位台、字段详情、清洗附页、进度水线**同一份**返回。
3. `app/services/contracts.py`：`contract_detail()` 的字段详情复用 `status()`。
4. `app/main.py`：新增 `GET /api/watermarks`、`POST /api/watermarks/refresh`、
   `GET /api/contracts/{cid}/fields`；`POST /api/watermark/advance` 改按类型推进。
5. 前端：`static/index.html` 加「水位台」；`static/js/app.js` 渲染四个面；
   `static/css/schema.css` 加 `chip.bad` / `chip.water` / `.annex`。

## 四、核对要点（复验按这个顺序看）

1. **页面水位与契约水位同源**：刷新后 `sources.page == sources.contract == sources.snapshot == sources.wash`，
   `same_source` 为真；未刷新时 `page.type` 为 `null`，不得拿契约类型冒充页面水位。
2. **假类型即停**：没有快照时 `progress.verified.ok` 必须为 `false`，
   `reason` 为「未刷新」；清洗声明 `INT` 时不得被当作可用类型。
3. **芯片可查**：水位台芯片 = 响应 `snapshots[0].snapshot_no` = 本地库
   `SELECT snapshot_no FROM watermark_type_snapshots ORDER BY id DESC LIMIT 1`。
4. **四处收口一致**：水流台、字段详情、清洗附页、进度水线拿到的
   `snapshot_no` 与类型必须完全相同。
5. **推进按类型**：`next=500` 在 `TIMESTAMP` 下返回 409「水位值不符合勘定类型」，
   不再被 `int()` 强转；重复或回退返回 409。
6. **缺锁即挡**：一方持有刷新锁时，另一方刷新返回 409「刷新进行中，请稍后」，
   且 `watermark_type_snapshots` 只增加一行。
7. **失败可见**：未绑定水位列时刷新返回 409「未绑定水位列」，
   `watermark_refresh_runs` 留下 `失败` 记录。

## 五、本地命令

```bash
/tmp/venv/bin/python -m pytest -q          # 12 passed
```

若只改附页措辞而不改类型写库，第 1、2、4、5 条会重新失败——
这版是改类型写库与推进路径，不是改文案。
