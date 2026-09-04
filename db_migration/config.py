"""YAML 설정 파일 로드 및 검증."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

CopyMode = Literal["truncate", "append", "upsert"]
VALID_MODES: tuple[str, ...] = ("truncate", "append", "upsert")

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(Exception):
    """설정 파일이 잘못되었을 때 발생."""


@dataclass(frozen=True)
class DbTarget:
    """소스 또는 대상 DB 접속 정보."""

    dsn: str
    schema: str


@dataclass(frozen=True)
class TableSpec:
    """테이블별 복사 설정."""

    name: str
    where: str | None = None
    mode: CopyMode | None = None  # None이면 전역 mode 사용


@dataclass(frozen=True)
class MigrationConfig:
    """마이그레이션 전체 설정."""

    source: DbTarget
    destination: DbTarget
    mode: CopyMode = "append"
    copy_all: bool = False
    exclude: tuple[str, ...] = ()
    tables: tuple[TableSpec, ...] = ()
    order: tuple[str, ...] = ()
    truncate_cascade: bool = False
    disable_triggers: bool = False
    reset_sequences: bool = True
    count_rows_on_dry_run: bool = True

    def table_spec(self, name: str) -> TableSpec:
        """이름으로 테이블 설정을 찾고, 없으면 기본 설정을 반환."""
        for spec in self.tables:
            if spec.name == name:
                return spec
        return TableSpec(name=name)

    def effective_mode(self, name: str) -> CopyMode:
        """테이블별 override를 반영한 최종 복사 모드."""
        return self.table_spec(name).mode or self.mode


def _substitute_env(value: Any) -> Any:
    """문자열 안의 ${VAR} 를 환경변수로 치환. 없는 변수는 에러."""
    if isinstance(value, str):

        def repl(m: re.Match[str]) -> str:
            name = m.group(1)
            if name not in os.environ:
                raise ConfigError(f"환경변수 {name} 가 설정되어 있지 않습니다")
            return os.environ[name]

        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _substitute_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_env(v) for v in value]
    return value


def _parse_target(raw: Any, label: str) -> DbTarget:
    if not isinstance(raw, dict):
        raise ConfigError(f"{label} 항목은 dsn, schema 를 가진 객체여야 합니다")
    dsn = raw.get("dsn")
    schema = raw.get("schema", "public")
    if not dsn or not isinstance(dsn, str):
        raise ConfigError(f"{label}.dsn 이 필요합니다")
    if not isinstance(schema, str) or not schema:
        raise ConfigError(f"{label}.schema 는 비어있지 않은 문자열이어야 합니다")
    return DbTarget(dsn=dsn, schema=schema)


def _parse_mode(raw: Any, label: str) -> CopyMode:
    if raw not in VALID_MODES:
        raise ConfigError(f"{label} 는 {', '.join(VALID_MODES)} 중 하나여야 합니다 (입력값: {raw!r})")
    return raw  # type: ignore[return-value]


def _parse_tables(raw: Any) -> tuple[TableSpec, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError("tables 는 목록이어야 합니다")
    specs: list[TableSpec] = []
    seen: set[str] = set()
    for item in raw:
        # 문자열 하나만 적으면 이름만 지정한 것으로 취급
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict) or not item.get("name"):
            raise ConfigError(f"tables 항목이 잘못되었습니다: {item!r}")
        name = str(item["name"])
        if name in seen:
            raise ConfigError(f"tables 에 {name} 이 중복 지정되었습니다")
        seen.add(name)
        where = item.get("where")
        if where is not None and not isinstance(where, str):
            raise ConfigError(f"tables[{name}].where 는 문자열이어야 합니다")
        mode = item.get("mode")
        specs.append(
            TableSpec(
                name=name,
                where=where.strip() if where else None,
                mode=_parse_mode(mode, f"tables[{name}].mode") if mode is not None else None,
            )
        )
    return tuple(specs)


def _parse_str_list(raw: Any, label: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise ConfigError(f"{label} 는 문자열 목록이어야 합니다")
    return tuple(raw)


def _parse_bool(raw: Any, label: str, default: bool) -> bool:
    if raw is None:
        return default
    if not isinstance(raw, bool):
        raise ConfigError(f"{label} 는 true/false 여야 합니다")
    return raw


def parse_config(data: dict[str, Any]) -> MigrationConfig:
    """dict 형태의 설정을 검증하여 MigrationConfig 로 변환."""
    if not isinstance(data, dict):
        raise ConfigError("설정 파일 최상위는 객체여야 합니다")
    data = _substitute_env(data)

    config = MigrationConfig(
        source=_parse_target(data.get("source"), "source"),
        destination=_parse_target(data.get("destination"), "destination"),
        mode=_parse_mode(data.get("mode", "append"), "mode"),
        copy_all=_parse_bool(data.get("copy_all"), "copy_all", False),
        exclude=_parse_str_list(data.get("exclude"), "exclude"),
        tables=_parse_tables(data.get("tables")),
        order=_parse_str_list(data.get("order"), "order"),
        truncate_cascade=_parse_bool(data.get("truncate_cascade"), "truncate_cascade", False),
        disable_triggers=_parse_bool(data.get("disable_triggers"), "disable_triggers", False),
        reset_sequences=_parse_bool(data.get("reset_sequences"), "reset_sequences", True),
        count_rows_on_dry_run=_parse_bool(
            data.get("count_rows_on_dry_run"), "count_rows_on_dry_run", True
        ),
    )

    if not config.copy_all and not config.tables:
        raise ConfigError("copy_all 이 false 이면 tables 에 복사할 테이블을 지정해야 합니다")
    if len(set(config.order)) != len(config.order):
        raise ConfigError("order 에 중복된 테이블이 있습니다")
    return config


def load_config(path: str | Path) -> MigrationConfig:
    """YAML 파일을 읽어 MigrationConfig 로 변환."""
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"설정 파일을 찾을 수 없습니다: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return parse_config(data)
