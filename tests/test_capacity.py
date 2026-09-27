"""容量规则与占用核算的端到端测试。"""
from datetime import datetime, timedelta, timezone

from test_api import BASE, PLAIN_FENCE, create_fence, pt, submit

INSIDE = (0.005, 0.005)   # PLAIN_FENCE 内部
OUTSIDE = (0.02, 0.02)    # PLAIN_FENCE 外部


def ts(sec):
    return (BASE + timedelta(seconds=sec)).isoformat()


def add_rule(client, fence_id, sec, capacity):
    r = client.post(
        f"/fences/{fence_id}/capacity-rules",
        json={"effective_from": ts(sec), "capacity": capacity},
    )
    assert r.status_code == 201, r.text
    return r.json()


def occupancy(client, fence_id, start_sec, end_sec):
    r = client.get(
        f"/fences/{fence_id}/occupancy",
        params={"start": ts(start_sec), "end": ts(end_sec)},
    )
    assert r.status_code == 200, r.text
    return r.json()


def seg_summary(body):
    """把分段压成便于断言的 (start_sec, end_sec, capacity, devices) 元组列表。"""
    out = []
    for s in body["segments"]:
        start = datetime.fromisoformat(s["start"])
        end = datetime.fromisoformat(s["end"])
        out.append((
            (start - BASE).total_seconds(),
            (end - BASE).total_seconds(),
            s["capacity"],
            tuple(s["devices"]),
        ))
    return out


# dev1: [10, 30) 在场；dev2: [5, 25) 在场；dev3: [15, ∞) 在场（不离开）
def submit_three_devices(client):
    submit(client, [
        pt("dev1", 0, *OUTSIDE), pt("dev1", 10, *INSIDE), pt("dev1", 30, *OUTSIDE),
        pt("dev2", 5, *INSIDE), pt("dev2", 25, *OUTSIDE),
        pt("dev3", 15, *INSIDE),
    ])


def test_capacity_rule_create_validate_and_dedup(client):
    fence_id = create_fence(client)

    rule = add_rule(client, fence_id, 0, 2)
    assert rule["fence_id"] == fence_id
    assert rule["capacity"] == 2
    assert datetime.fromisoformat(rule["effective_from"]) == BASE

    # 同一围栏同一生效时刻不得重复
    r = client.post(
        f"/fences/{fence_id}/capacity-rules",
        json={"effective_from": ts(0), "capacity": 5},
    )
    assert r.status_code == 409

    # 上限必须是正整数
    for bad in (0, -1, 1.5):
        r = client.post(
            f"/fences/{fence_id}/capacity-rules",
            json={"effective_from": ts(10), "capacity": bad},
        )
        assert r.status_code == 422, bad

    # 围栏不存在
    r = client.post(
        "/fences/9999/capacity-rules",
        json={"effective_from": ts(0), "capacity": 1},
    )
    assert r.status_code == 404

    # 列表按生效时刻升序
    add_rule(client, fence_id, 100, 3)
    add_rule(client, fence_id, 50, 1)
    rules = client.get(f"/fences/{fence_id}/capacity-rules").json()
    assert [(r["capacity"],) for r in rules] == [(2,), (1,), (3,)]
    assert client.get("/fences/9999/capacity-rules").status_code == 404

    # 删除
    rid = rules[0]["id"]
    assert client.delete(f"/fences/{fence_id}/capacity-rules/{rid}").status_code == 204
    remaining = client.get(f"/fences/{fence_id}/capacity-rules").json()
    assert [r["capacity"] for r in remaining] == [1, 3]
    assert client.delete(f"/fences/{fence_id}/capacity-rules/{rid}").status_code == 404

    # 删除围栏级联删除规则
    assert client.delete(f"/fences/{fence_id}").status_code == 204


def test_occupancy_segments_and_truncation(client):
    fence_id = create_fence(client)
    submit_three_devices(client)

    # 全窗口 [0, 50)：无规则 -> capacity 为 null，无超限
    body = occupancy(client, fence_id, 0, 50)
    assert body["fence_id"] == fence_id
    assert seg_summary(body) == [
        (0, 5, None, ()),
        (5, 10, None, ("dev2",)),
        (10, 15, None, ("dev1", "dev2")),
        (15, 25, None, ("dev1", "dev2", "dev3")),
        (25, 30, None, ("dev1", "dev3")),
        (30, 50, None, ("dev3",)),   # dev3 未离开，截断到 end
    ]
    assert body["violations"] == []

    # 窗口 [12, 28)：dev1/dev2 查询前已进入 -> 截断到 start
    body = occupancy(client, fence_id, 12, 28)
    assert seg_summary(body) == [
        (12, 15, None, ("dev1", "dev2")),
        (15, 25, None, ("dev1", "dev2", "dev3")),
        (25, 28, None, ("dev1", "dev3")),
    ]


def test_occupancy_rule_switching_and_violations(client):
    fence_id = create_fence(client)
    submit_three_devices(client)
    add_rule(client, fence_id, 0, 2)     # [0, 20) 上限 2
    add_rule(client, fence_id, 20, 3)    # [20, ∞) 上限 3

    body = occupancy(client, fence_id, 0, 50)
    assert seg_summary(body) == [
        (0, 5, 2, ()),
        (5, 10, 2, ("dev2",)),
        (10, 15, 2, ("dev1", "dev2")),
        (15, 20, 2, ("dev1", "dev2", "dev3")),  # 3 > 2 超限
        (20, 25, 3, ("dev1", "dev2", "dev3")),  # 规则切换后不再超限
        (25, 30, 3, ("dev1", "dev3")),
        (30, 50, 3, ("dev3",)),
    ]
    assert len(body["violations"]) == 1
    v = body["violations"][0]
    assert v["capacity"] == 2
    assert v["occupancy"] == 3
    assert v["excess"] == 1
    assert v["devices"] == ["dev1", "dev2", "dev3"]
    assert datetime.fromisoformat(v["start"]) == BASE + timedelta(seconds=15)
    assert datetime.fromisoformat(v["end"]) == BASE + timedelta(seconds=20)


def test_occupancy_atomic_boundary(client):
    """同一时刻的离开、进入与规则切换合并为一个边界，无零长度分段。"""
    fence_id = create_fence(client)
    submit(client, [
        pt("dev1", 10, *INSIDE), pt("dev1", 30, *OUTSIDE),   # dev1: [10, 30)
        pt("dev2", 30, *INSIDE),                              # dev2: [30, ∞)
    ])
    add_rule(client, fence_id, 0, 1)
    add_rule(client, fence_id, 30, 2)    # 规则切换与设备进出同在 t=30

    body = occupancy(client, fence_id, 0, 50)
    assert seg_summary(body) == [
        (0, 10, 1, ()),
        (10, 30, 1, ("dev1",)),
        (30, 50, 2, ("dev2",)),   # t=30 处原子切换：dev1 离场、dev2 在场、上限变 2
    ]
    assert all(s["start"] != s["end"] for s in body["segments"])
    assert body["violations"] == []


def test_occupancy_half_open_edges(client):
    """半开语义：start 时刻离开的不计，end 时刻进入的不计，start 时刻进入的计入。"""
    fence_id = create_fence(client)
    submit(client, [
        pt("dev1", 10, *INSIDE), pt("dev1", 20, *OUTSIDE),   # 恰在 start=20 离开
        pt("dev2", 40, *INSIDE),                              # 恰在 end=40 进入
        pt("dev3", 20, *INSIDE), pt("dev3", 40, *OUTSIDE),   # 恰覆盖整个窗口
    ])
    body = occupancy(client, fence_id, 20, 40)
    assert seg_summary(body) == [(20, 40, None, ("dev3",))]


def test_occupancy_ignores_stay_events(client):
    """stay 事件不影响在场区间还原。"""
    fence_id = create_fence(client, config={"stay_min_s": 10})
    submit(client, [pt("dev1", 0, *INSIDE), pt("dev1", 100, *INSIDE)])
    events = client.get("/events", params={"device_id": "dev1"}).json()
    assert [e["type"] for e in events] == ["enter", "stay"]

    body = occupancy(client, fence_id, 0, 50)
    assert seg_summary(body) == [(0, 50, None, ("dev1",))]


def test_occupancy_backfill_recomputes(client):
    """补录乱序轨迹点后再次查询必须反映重算结果，不残留旧的超限片段。"""
    fence_id = create_fence(client)
    add_rule(client, fence_id, 0, 1)
    submit(client, [
        pt("dev1", 10, *INSIDE), pt("dev1", 30, *OUTSIDE),   # dev1: [10, 30)
        pt("dev2", 20, *INSIDE), pt("dev2", 40, *OUTSIDE),   # dev2: [20, 40)
    ])

    body = occupancy(client, fence_id, 0, 50)
    assert [(
        datetime.fromisoformat(v["start"]) - BASE,
        datetime.fromisoformat(v["end"]) - BASE,
    ) for v in body["violations"]] == [
        (timedelta(seconds=20), timedelta(seconds=30)),      # dev1+dev2 同时在场
    ]

    # 乱序补录：dev2 在 t=22 其实在围栏外 -> 重算后 dev2 在场区间变为 [20, 22)
    submit(client, [pt("dev2", 22, *OUTSIDE)])

    body = occupancy(client, fence_id, 0, 50)
    assert seg_summary(body) == [
        (0, 10, 1, ()),
        (10, 20, 1, ("dev1",)),
        (20, 22, 1, ("dev1", "dev2")),
        (22, 30, 1, ("dev1",)),
        (30, 50, 1, ()),
    ]
    # 旧的 [22, 30) 超限片段必须消失，只保留重算后的 [20, 22)
    assert len(body["violations"]) == 1
    v = body["violations"][0]
    assert datetime.fromisoformat(v["start"]) == BASE + timedelta(seconds=20)
    assert datetime.fromisoformat(v["end"]) == BASE + timedelta(seconds=22)
    assert v["devices"] == ["dev1", "dev2"]
    assert v["excess"] == 1


def test_occupancy_invalid_window_and_missing_fence(client):
    fence_id = create_fence(client)
    r = client.get(
        f"/fences/{fence_id}/occupancy",
        params={"start": ts(50), "end": ts(50)},
    )
    assert r.status_code == 422
    r = client.get(
        f"/fences/{fence_id}/occupancy",
        params={"start": ts(60), "end": ts(50)},
    )
    assert r.status_code == 422
    r = client.get(
        "/fences/9999/occupancy",
        params={"start": ts(0), "end": ts(50)},
    )
    assert r.status_code == 404


def test_occupancy_rule_effective_at_window_start(client):
    """生效时刻恰等于 start 的规则从窗口起点即适用。"""
    fence_id = create_fence(client)
    submit(client, [pt("dev1", 5, *INSIDE), pt("dev2", 10, *INSIDE)])
    add_rule(client, fence_id, 10, 1)    # 恰在查询窗口起点生效

    body = occupancy(client, fence_id, 10, 50)
    assert seg_summary(body) == [(10, 50, 1, ("dev1", "dev2"))]
    assert body["violations"][0]["excess"] == 1
