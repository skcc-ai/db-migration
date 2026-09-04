"""실행 계획과 결과를 표현하는 데이터 구조."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Literal

from .config import CopyMode

Status = Literal["success", "failed", "skipped"]


@dataclass
class TablePlan:
    """테이블 하나에 대한 실행 계획."""

    name: str
    mode: CopyMode
    where: str | None
    columns: tuple[str, ...] = ()
    conflict_columns: tuple[str, ...] | None = None
    has_identity_always: bool = False  # 대상 테이블에 GENERATED ALWAYS AS IDENTITY 컬럼 존재 여부
    sequences: tuple[tuple[str, str], ...] = ()  # (시퀀스 이름, 컬럼)
    parents: tuple[str, ...] = ()  # 이 계획 안에서 먼저 복사되어야 하는 테이블
    estimated_rows: int | None = None
    # 계획 단계에서 판정된 상태. None 이면 실행 대상.
    precheck_status: Status | None = None
    precheck_message: str = ""


@dataclass
class MigrationPlan:
    """전체 실행 계획."""

    tables: list[TablePlan]
    truncate_targets: list[str]
    warnings: list[str] = field(default_factory=list)


@dataclass
class TableResult:
    """테이블 하나의 실행 결과."""

    name: str
    status: Status
    rows: int | None = None
    elapsed: float = 0.0
    message: str = ""


@dataclass
class MigrationResult:
    """전체 실행 결과."""

    results: list[TableResult]
    warnings: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return not any(r.status == "failed" for r in self.results)


@dataclass
class Event:
    """진행 상황 알림. UI 나 CLI 에서 구독한다."""

    kind: Literal["warning", "info", "table_start", "table_done", "truncate"]
    message: str
    table: str | None = None
    result: TableResult | None = None


EventListener = Callable[[Event], None]
