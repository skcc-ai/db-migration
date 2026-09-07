"""YAML 설정 파일 로드 및 검증."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from psycopg.conninfo import make_conninfo

CopyMode = Literal["truncate", "append", "upsert"]
VALID_MODES: tuple[str, ...] = ("truncate", "append", "upsert")

# ${VAR} 또는 ${VAR:-기본값}. 기본값 안에는 } 를 쓸 수 없다.
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(Exception):
    """설정 파일이 잘못되었을 때 발생."""


@dataclass(frozen=True)
class DbTarget:
    """소스 또는 대상 DB 접속 정보. dsn 은 개별 항목으로부터 조합된 최종 접속 문자열."""

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
    progress_interval: float = 5.0  # 복사 중 진행 상황 출력 간격(초). 0 이면 출력 안 함
    stall_timeout: float = 300.0  # 이 시간(초) 동안 데이터가 전혀 없으면 테이블 실패 처리. 0 이면 무제한 대기

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
    """문자열 안의 ${VAR} 를 환경변수로 치환.

    ${VAR:-기본값} 형태면 변수가 없거나 빈 문자열일 때 기본값을 쓴다.
    기본값 없이 변수도 없으면 에러.
    """
    if isinstance(value, str):

        def repl(m: re.Match[str]) -> str:
            name, default = m.group(1), m.group(2)
            current = os.environ.get(name)
            if current:
                return current
            if default is not None:
                return default
            raise ConfigError(f"환경변수 {name} 가 설정되어 있지 않습니다")

        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _substitute_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_env(v) for v in value]
    return value


_CONN_KEYS = ("host", "port", "user", "password", "database")


def _parse_target(raw: Any, label: str) -> DbTarget:
    """host/port/user/password/database 개별 항목 또는 dsn 으로 접속 정보를 만든다.

    dsn 과 개별 항목을 같이 쓰면 개별 항목이 dsn 값을 덮어쓴다.
    """
    if not isinstance(raw, dict):
        raise ConfigError(f"{label} 항목은 host, database 등을 가진 객체여야 합니다")

    schema = raw.get("schema", "public")
    if not isinstance(schema, str) or not schema:
        raise ConfigError(f"{label}.schema 는 비어있지 않은 문자열이어야 합니다")

    dsn = raw.get("dsn")
    if dsn is not None and (not isinstance(dsn, str) or not dsn):
        raise ConfigError(f"{label}.dsn 은 비어있지 않은 문자열이어야 합니다")

    parts: dict[str, Any] = {}
    for key in _CONN_KEYS:
        value = raw.get(key)
        if value is None or value == "":
            continue
        if key == "port":
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise ConfigError(f"{label}.port 는 숫자여야 합니다 (입력값: {value!r})") from None
        elif not isinstance(value, str):
            value = str(value)
        # psycopg 는 database 대신 dbname 키워드를 쓴다
        parts["dbname" if key == "database" else key] = value

    if dsn is None and "dbname" not in parts:
        raise ConfigError(f"{label}.database 가 필요합니다 (또는 dsn 을 지정하세요)")

    try:
        conninfo = make_conninfo(dsn or "", **parts)
    except Exception as exc:  # psycopg.ProgrammingError 등
        raise ConfigError(f"{label} 접속 정보가 잘못되었습니다: {exc}") from None
    return DbTarget(dsn=conninfo, schema=schema)


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


def _parse_number(raw: Any, label: str, default: float) -> float:
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < 0:
        raise ConfigError(f"{label} 는 0 이상의 숫자여야 합니다")
    return float(raw)


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
        progress_interval=_parse_number(data.get("progress_interval"), "progress_interval", 5.0),
        stall_timeout=_parse_number(data.get("stall_timeout"), "stall_timeout", 300.0),
    )

    if not config.copy_all and not config.tables:
        raise ConfigError("copy_all 이 false 이면 tables 에 복사할 테이블을 지정해야 합니다")
    if len(set(config.order)) != len(config.order):
        raise ConfigError("order 에 중복된 테이블이 있습니다")
    return config


def load_env_file(config_path: Path, env_file: str | Path | None = None) -> Path | None:
    """.env 파일을 읽어 환경변수로 올린다. 이미 설정된 환경변수는 덮어쓰지 않는다.

    env_file 을 지정하면 그 파일을 (없으면 에러), 지정하지 않으면 설정 파일과 같은 폴더의 .env,
    그 다음 현재 작업 폴더의 .env 순서로 찾아 처음 발견한 파일 하나를 읽는다.
    읽은 파일 경로를 반환하고, 없으면 None.
    """
    if env_file is not None:
        env_path = Path(env_file)
        if not env_path.exists():
            raise ConfigError(f"env 파일을 찾을 수 없습니다: {env_path}")
        load_dotenv(env_path, override=False)
        return env_path

    for candidate in (config_path.resolve().parent / ".env", Path.cwd() / ".env"):
        if candidate.exists():
            load_dotenv(candidate, override=False)
            return candidate
    return None


def load_config(path: str | Path, env_file: str | Path | None = None) -> MigrationConfig:
    """YAML 파일을 읽어 MigrationConfig 로 변환. 먼저 .env 를 읽어 ${VAR} 치환에 사용한다."""
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"설정 파일을 찾을 수 없습니다: {path}")
    load_env_file(path, env_file)
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return parse_config(data)
