# 轨迹围栏事件 API（Geofence Trajectory Events）

基于 **FastAPI + SQLite + Shapely** 的地理围栏事件服务：

- 创建**带孔多边形**围栏（GeoJSON Polygon，支持 holes），每围栏可配置判定参数
- 批量提交轨迹点（设备 ID + 时间戳 + 经纬度），**乱序点按设备时间自动重排**
- 边界抖动去抖：可配置的**进入/离开缓冲距离**（迟滞带）+ **停留时长**（dwell）
- 产出 `enter` / `leave` / `stay` 事件；**重复提交轨迹点不会重复产出事件**（幂等）
- 按时间范围/设备/围栏/类型查询事件，查询设备当前状态
- **围栏容量核算**：按生效时刻维护容量上限规则，对任意 `[start, end)` 窗口还原每台设备的
  在场区间，输出恒定状态分段与超限分段（上限/实际人数/超额数/在场设备）

## 快速开始（Docker Compose 一键启动）

```bash
docker compose up --build
```

- 服务端口：**8000**（http://localhost:8000）
- 交互式 API 文档：http://localhost:8000/docs
- SQLite 数据持久化在名为 `geofence-data` 的卷中

停止并清理：`docker compose down`（加 `-v` 同时删除数据卷）

## 判定逻辑说明

1. **投影**：以围栏质心为原点做等距圆柱投影，把经纬度转为米制坐标后用 Shapely 计算点到多边形的距离 `d`（点在多边形内 `d=0`；**孔洞内 `d>0`，天然视为外部**）。
2. **缓冲迟滞**：
   - `d <= enter_buffer_m` → 判定为内部（可略提前触发进入）
   - `d > exit_buffer_m` → 判定为外部
   - 两者之间为迟滞灰区 → **维持上一状态**，抑制边界来回抖动
3. **停留去抖（dwell）**：状态迁移条件首次满足时开始计时，持续满足 `enter_dwell_s` / `exit_dwell_s` 才确认迁移并产出事件；期间条件被打破则作废。事件时间取**条件首次满足的轨迹时刻**。
4. **stay**：进入围栏后连续停留达到 `stay_min_s` 产出一次 `stay`（每次访问最多一次）。
5. **乱序与幂等**：轨迹点按 `(device_id, ts)` 唯一约束去重；每次摄入后按设备全量轨迹（时间升序）重算事件，因此乱序到达与按序到达结果一致，重复提交结果不变。

## 容量核算逻辑说明

1. **容量规则**：每条规则为 `(fence_id, effective_from) -> capacity`，`capacity` 为正整数；`(fence_id, effective_from)` 唯一，同一生效时刻不得重复。某时刻生效的上限取**生效时刻不晚于该时刻的最后一条规则**；此前无规则则该时段不限容量（`capacity: null`）。
2. **在场区间还原**：查询 `[start, end)` 时，从该围栏的 `enter`/`leave` 事件（`stay` 不参与）还原每台设备的**半开**在场区间 `[enter_ts, leave_ts)`：`start` 之前已进入的截断到 `start`，`end` 时仍未离开的截断到 `end`。
3. **原子边界**：设备进出与规则切换落在同一时刻时按**一个边界**处理——半开语义下 `t` 时刻离开的设备自 `t` 起不在场、进入的自 `t` 起在场、`t` 生效的规则自 `t` 起适用，全部变化同时生效，不产生中间态或零长度分段。
4. **恒定分段**：相邻边界之间 `(capacity, devices)` 恒定，相同状态的相邻分段被合并，返回**尽可能长**的分段；`occupancy > capacity` 的分段进入 `violations`，附上限、实际人数、超额数（`excess = occupancy - capacity`）与按 ID 排序的在场设备。
5. **实时重算**：占用在查询时基于当前事件表实时计算；补录乱序轨迹点会触发该设备事件重算，再次查询立即反映新结果，不残留旧的占用或超限片段。

## 示例请求

### 1. 创建带孔围栏

```bash
curl -X POST http://localhost:8000/fences \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "园区A（中央湖泊为孔洞）",
    "geometry": {
      "type": "Polygon",
      "coordinates": [
        [[116.30, 39.90], [116.32, 39.90], [116.32, 39.92], [116.30, 39.92], [116.30, 39.90]],
        [[116.308, 39.908], [116.312, 39.908], [116.312, 39.912], [116.308, 39.912], [116.308, 39.908]]
      ]
    },
    "config": {
      "enter_buffer_m": 0,
      "exit_buffer_m": 50,
      "enter_dwell_s": 10,
      "exit_dwell_s": 10,
      "stay_min_s": 600
    }
  }'
```

返回 `201` 及围栏对象（含 `id`，下文假设为 `1`）。

### 2. 提交轨迹点（乱序、含重复）

```bash
curl -X POST http://localhost:8000/points \
  -H 'Content-Type: application/json' \
  -d '{
    "points": [
      {"device_id": "dev-001", "ts": "2026-09-22T08:00:20Z", "lat": 39.910, "lon": 116.305},
      {"device_id": "dev-001", "ts": "2026-09-22T08:00:00Z", "lat": 39.900, "lon": 116.300},
      {"device_id": "dev-001", "ts": "2026-09-22T08:00:10Z", "lat": 39.905, "lon": 116.302},
      {"device_id": "dev-001", "ts": "2026-09-22T08:00:20Z", "lat": 39.910, "lon": 116.305}
    ]
  }'
```

返回：`{"accepted": 3, "duplicates": 1, "devices_updated": 1}`

### 3. 按时间范围查询事件

```bash
curl 'http://localhost:8000/events?device_id=dev-001&start=2026-09-22T00:00:00Z&end=2026-09-23T00:00:00Z'
# 可选过滤：fence_id=1&type=enter&limit=100&offset=0
```

### 4. 查询设备当前状态

```bash
curl http://localhost:8000/devices/dev-001/status
```

返回示例：

```json
{
  "device_id": "dev-001",
  "fences": [
    {"fence_id": 1, "fence_name": "园区A（中央湖泊为孔洞）", "state": "inside", "since": "2026-09-22T08:00:10Z"}
  ]
}
```

### 5. 新增容量规则

```bash
curl -X POST http://localhost:8000/fences/1/capacity-rules \
  -H 'Content-Type: application/json' \
  -d '{"effective_from": "2026-09-22T08:00:00Z", "capacity": 2}'
```

- `capacity` 必须为**正整数**；`effective_from` 为规则生效时刻（缺省时区按 UTC）
- 同一围栏同一生效时刻重复提交返回 `409`
- `GET /fences/1/capacity-rules` 按生效时刻升序列出规则；`DELETE /fences/1/capacity-rules/{rule_id}` 删除

### 6. 查询容量占用分段

```bash
curl 'http://localhost:8000/fences/1/occupancy?start=2026-09-22T08:00:00Z&end=2026-09-22T09:00:00Z'
```

返回示例（`segments` 为恒定状态分段，`violations` 为其中的超限分段）：

```json
{
  "fence_id": 1,
  "start": "2026-09-22T08:00:00Z",
  "end": "2026-09-22T09:00:00Z",
  "segments": [
    {"start": "2026-09-22T08:00:00Z", "end": "2026-09-22T08:10:00Z", "capacity": 2, "occupancy": 1, "devices": ["dev-001"]},
    {"start": "2026-09-22T08:10:00Z", "end": "2026-09-22T08:25:00Z", "capacity": 2, "occupancy": 3, "devices": ["dev-001", "dev-002", "dev-003"]}
  ],
  "violations": [
    {"start": "2026-09-22T08:10:00Z", "end": "2026-09-22T08:25:00Z", "capacity": 2, "occupancy": 3, "excess": 1, "devices": ["dev-001", "dev-002", "dev-003"]}
  ]
}
```

`capacity` 为 `null` 表示该时段无生效规则（不限容量），此时不会产生超限。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/fences` | 创建围栏（GeoJSON Polygon，支持孔洞，附判定配置） |
| GET | `/fences` / `/fences/{id}` | 围栏列表 / 详情 |
| DELETE | `/fences/{id}` | 删除围栏及其事件、状态、容量规则 |
| POST | `/fences/{id}/capacity-rules` | 新增容量规则（生效时刻 + 正整数上限，同时刻重复返回 409） |
| GET | `/fences/{id}/capacity-rules` | 容量规则列表（按生效时刻升序） |
| DELETE | `/fences/{id}/capacity-rules/{rule_id}` | 删除容量规则 |
| GET | `/fences/{id}/occupancy` | 容量占用核算（`start`/`end` 必填，半开区间 `[start, end)`） |
| POST | `/points` | 批量提交轨迹点（乱序重排、重复幂等） |
| GET | `/events` | 事件查询（device_id / fence_id / type / start / end / limit / offset） |
| GET | `/devices/{device_id}/status` | 设备当前相对各围栏的状态 |
| GET | `/health` | 健康检查 |

## 围栏配置参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `enter_buffer_m` | 0 | 进入缓冲距离（米），距多边形 ≤ 该值即视为内部 |
| `exit_buffer_m` | 0 | 离开缓冲距离（米），距多边形 > 该值才视为外部；须 ≥ `enter_buffer_m` |
| `enter_dwell_s` | 0 | 进入确认所需持续时长（秒） |
| `exit_dwell_s` | 0 | 离开确认所需持续时长（秒） |
| `stay_min_s` | 300 | 产生 `stay` 事件的最小连续停留时长（秒） |

## 本地开发与测试

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

pytest                      # 运行测试
uvicorn app.main:app --reload --port 8000   # 本地启动
```

数据库位置由环境变量 `DATABASE_URL` 控制，默认 `sqlite:///./geofence.db`。

## 项目结构

```
app/
  main.py      # FastAPI 路由与摄入/重算流程、容量规则与占用核算接口
  engine.py    # 投影、缓冲迟滞 + dwell 状态机（事件判定核心）、容量占用分段计算
  models.py    # SQLAlchemy 模型（fences / points / events / device_state / capacity_rules）
  schemas.py   # Pydantic 请求/响应模型
  database.py  # SQLite 连接与会话
tests/         # pytest 端到端测试（孔洞、乱序、幂等、抖动、停留、查询、容量核算）
Dockerfile / docker-compose.yml
```

## 说明与取舍

- 事件按设备全量轨迹重算并整体替换，保证乱序/重复提交下结果一致；事件 `id` 可能随重算变化，请以 `(device_id, fence_id, type, ts)` 识别事件。
- 同一设备同一时间戳的轨迹点视为重复（保留首条）。
- 距离计算采用以围栏质心为原点的局部等距圆柱投影近似，围栏尺度（公里级）下误差可忽略。
