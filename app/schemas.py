from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class FenceConfigSchema(BaseModel):
    """围栏判定参数。enter_buffer_m 必须 <= exit_buffer_m 才能构成有效迟滞带。"""

    enter_buffer_m: float = Field(0.0, ge=0, description="进入缓冲距离（米）")
    exit_buffer_m: float = Field(0.0, ge=0, description="离开缓冲距离（米）")
    enter_dwell_s: float = Field(0.0, ge=0, description="进入确认停留时长（秒）")
    exit_dwell_s: float = Field(0.0, ge=0, description="离开确认停留时长（秒）")
    stay_min_s: float = Field(300.0, ge=0, description="产生 stay 事件的最小连续停留时长（秒）")

    @model_validator(mode="after")
    def check_buffers(self):
        if self.enter_buffer_m > self.exit_buffer_m:
            raise ValueError("enter_buffer_m 必须小于等于 exit_buffer_m")
        return self


class FenceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    geometry: dict = Field(description="GeoJSON Polygon，coordinates[0] 为外环，其余为孔洞")
    config: FenceConfigSchema = FenceConfigSchema()


class FenceOut(BaseModel):
    id: int
    name: str
    geometry: dict
    config: FenceConfigSchema
    created_at: datetime


class PointIn(BaseModel):
    device_id: str = Field(min_length=1, max_length=100)
    ts: datetime = Field(description="ISO 8601 时间戳，缺省时区按 UTC 处理")
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class PointBatch(BaseModel):
    points: list[PointIn] = Field(min_length=1, max_length=10000)


class IngestResult(BaseModel):
    accepted: int
    duplicates: int
    devices_updated: int


class EventOut(BaseModel):
    id: int
    device_id: str
    fence_id: int
    type: str
    ts: datetime


class FenceStateOut(BaseModel):
    fence_id: int
    fence_name: str
    state: Literal["inside", "outside"]
    since: datetime | None


class DeviceStatusOut(BaseModel):
    device_id: str
    fences: list[FenceStateOut]


class CapacityRuleCreate(BaseModel):
    effective_from: datetime = Field(description="规则生效时刻（ISO 8601，缺省时区按 UTC 处理）")
    capacity: int = Field(gt=0, description="容量上限（正整数）")


class CapacityRuleOut(BaseModel):
    id: int
    fence_id: int
    effective_from: datetime
    capacity: int


class OccupancySegmentOut(BaseModel):
    start: datetime
    end: datetime
    capacity: int | None  # null 表示该时段无生效规则（不限容量）
    occupancy: int
    devices: list[str]  # 在场设备，按 ID 排序


class CapacityViolationOut(BaseModel):
    start: datetime
    end: datetime
    capacity: int
    occupancy: int
    excess: int
    devices: list[str]  # 在场设备，按 ID 排序


class OccupancyOut(BaseModel):
    fence_id: int
    start: datetime
    end: datetime
    segments: list[OccupancySegmentOut]
    violations: list[CapacityViolationOut]
