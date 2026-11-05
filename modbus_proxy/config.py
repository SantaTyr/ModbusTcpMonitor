"""配置定义、加载与校验。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any


@dataclass
class RecordConfig:
    enabled: bool = True
    dir: str = "captures"
    flush: bool = True
    rotate_mb: int = 64
    keep_days: int = 7

    @staticmethod
    def from_dict(data: dict) -> "RecordConfig":
        known = {f.name for f in fields(RecordConfig)}
        return RecordConfig(**{k: v for k, v in (data or {}).items() if k in known})


@dataclass
class Config:
    listen_host: str = "0.0.0.0"
    listen_port: int = 502
    device_host: str = "192.168.1.100"
    device_port: int = 502
    web_host: str = "0.0.0.0"
    web_port: int = 8080
    connect_timeout: float = 3.0
    response_timeout: float = 5.0
    idle_close: float = 60.0
    timeout_policy: str = "drop"          # drop | exception
    address_offset: int = 0
    auto_start: bool = True
    auto_open_browser: bool = True
    record: RecordConfig = field(default_factory=RecordConfig)

    @staticmethod
    def from_dict(data: dict) -> "Config":
        data = dict(data or {})
        rec = data.pop("record", {})
        known = {f.name for f in fields(Config)}
        cfg = Config(**{k: v for k, v in data.items() if k in known})
        cfg.record = RecordConfig.from_dict(rec)
        return cfg

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data

    def copy(self) -> "Config":
        return Config.from_dict(self.to_dict())


def load_config(path: Path) -> Config:
    if not path.exists():
        cfg = Config()
        save_config(path, cfg)
        return cfg
    with open(path, "r", encoding="utf-8-sig") as fh:
        raw = json.load(fh)
    return Config.from_dict(raw)


def save_config(path: Path, cfg: Config) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg.to_dict(), fh, ensure_ascii=False, indent=2)
    tmp.replace(path)


def validate(cfg: Config) -> list[str]:
    errors: list[str] = []
    if not cfg.listen_host.strip():
        errors.append("监听地址不能为空")
    if not cfg.device_host.strip():
        errors.append("设备地址不能为空")
    for name, value in (("监听端口", cfg.listen_port), ("设备端口", cfg.device_port), ("网页端口", cfg.web_port)):
        if not isinstance(value, int) or not (1 <= value <= 65535):
            errors.append(f"{name}必须是 1-65535 的整数")
    if not (0.2 <= float(cfg.connect_timeout) <= 120):
        errors.append("连接超时必须介于 0.2-120 秒")
    if not (0.2 <= float(cfg.response_timeout) <= 600):
        errors.append("应答超时必须介于 0.2-600 秒")
    if not (0 <= float(cfg.idle_close) <= 3600):
        errors.append("空闲断开时间必须介于 0-3600 秒")
    if cfg.timeout_policy not in ("drop", "exception"):
        errors.append("超时策略只能是 drop 或 exception")
    if cfg.address_offset not in (0, 1):
        errors.append("地址偏移只能是 0 或 1")
    if int(cfg.record.keep_days) < 1:
        errors.append("保留天数至少为 1")
    if int(cfg.record.rotate_mb) < 1:
        errors.append("单个日志文件上限至少为 1MB")
    return errors
