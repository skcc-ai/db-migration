"""마이그레이션 계획 수립과 실행 오케스트레이션."""

from __future__ import annotations

import time

import psycopg
from psycopg import sql

from . import db
from .config import MigrationConfig
from .copier import copy_table
from .models import (
    Event,
    EventListener,
    MigrationPlan,
    MigrationResult,
    TablePlan,
    TableResult,
)
from .ordering import resolve_order


class MigrationError(Exception):
    """계획 단계에서 실행을 진행할 수 없을 때 발생."""


def _noop(_: Event) -> None:
    pass


def _first_line(exc: BaseException) -> str:
    """예외 메시지의 첫 줄만 반환 (Postgres 에러는 여러 줄인 경우가 많음)."""
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__


def _select_tables(config: MigrationConfig, source_tables: list[str]) -> list[str]:
    """설정에 따라 복사 대상 테이블 이름 목록을 결정. tables 에 명시된 테이블이 소스에 없으면 에러."""
    source_set = set(source_tables)
    missing = [s.name for s in config.tables if s.name not in source_set]
    if missing:
        raise MigrationError("tables 에 지정된 테이블이 소스 스키마에 없습니다: " + ", ".join(missing))
    if config.copy_all:
        excluded = set(config.exclude)
        return [t for t in source_tables if t not in excluded]
    return [s.name for s in config.tables]


def build_plan(
    config: MigrationConfig,
    src: psycopg.Connection,
    dst: psycopg.Connection,
    *,
    count_rows: bool = False,
) -> MigrationPlan:
    """소스/대상 메타데이터를 읽어 실행 계획을 만든다. 아무것도 쓰지 않는다."""
    warnings: list[str] = []
    src_schema = config.source.schema
    dst_schema = config.destination.schema

    if not db.schema_exists(src, src_schema):
        raise MigrationError(f"소스 스키마가 없습니다: {src_schema}")
    if not db.schema_exists(dst, dst_schema):
        raise MigrationError(f"대상 스키마가 없습니다: {dst_schema}")

    source_tables = db.list_tables(src, src_schema)
    dest_tables = set(db.list_tables(dst, dst_schema))
    selected = _select_tables(config, source_tables)

    if config.copy_all:
        for name in config.exclude:
            if name not in source_tables:
                warnings.append(f"exclude 에 지정된 {name} 은 소스 스키마에 없습니다")
    for name in config.order:
        if name not in selected:
            warnings.append(f"order 에 지정된 {name} 은 복사 대상이 아니므로 무시합니다")

    # 의존성: 소스와 대상 FK 를 합쳐서 사용 (대상 제약이 삽입 순서를 결정하지만, 소스만 FK 가 있는 경우도 안전하게)
    selected_set = set(selected)
    deps = set(db.get_foreign_keys(src, src_schema)) | set(db.get_foreign_keys(dst, dst_schema))
    deps_in_scope = [(c, p) for c, p in deps if c in selected_set and p in selected_set]
    order = resolve_order(selected_set, deps_in_scope, config.order)

    parents_of: dict[str, set[str]] = {t: set() for t in selected}
    for child, parent in deps_in_scope:
        parents_of[child].add(parent)

    plans: list[TablePlan] = []
    for name in order:
        spec = config.table_spec(name)
        plan = TablePlan(
            name=name,
            mode=config.effective_mode(name),
            where=spec.where,
            parents=tuple(sorted(parents_of[name])),
        )
        plans.append(plan)

        if name not in dest_tables:
            if config.copy_all and spec.name not in {s.name for s in config.tables}:
                plan.precheck_status = "skipped"
                plan.precheck_message = "대상 스키마에 테이블이 없어 건너뜀"
                warnings.append(f"{name}: 대상 스키마에 테이블이 없어 건너뜁니다")
            else:
                plan.precheck_status = "failed"
                plan.precheck_message = "대상 스키마에 테이블이 없음"
            continue

        src_cols = db.get_columns(src, src_schema, name)
        dst_cols = {c.name: c for c in db.get_columns(dst, dst_schema, name)}

        # 소스의 generated 컬럼은 값이 계산되므로 제외. 대상에서 generated 인 컬럼도 제외.
        columns: list[str] = []
        missing: list[str] = []
        for col in src_cols:
            if col.is_generated:
                continue
            dst_col = dst_cols.get(col.name)
            if dst_col is None:
                missing.append(col.name)
            elif not dst_col.is_generated:
                columns.append(col.name)
        if missing:
            plan.precheck_status = "failed"
            plan.precheck_message = "대상 테이블에 없는 컬럼: " + ", ".join(missing)
            continue
        if not columns:
            plan.precheck_status = "failed"
            plan.precheck_message = "복사할 컬럼이 없음"
            continue

        plan.columns = tuple(columns)
        plan.has_identity_always = any(c.identity == "a" for c in dst_cols.values())

        if plan.mode == "upsert":
            conflict = db.get_conflict_columns(dst, dst_schema, name)
            if conflict is None or not set(conflict).issubset(columns):
                plan.precheck_status = "failed"
                plan.precheck_message = "upsert 에 사용할 PK 또는 unique 제약이 대상 테이블에 없음"
                continue
            plan.conflict_columns = conflict

        if config.reset_sequences:
            plan.sequences = tuple(db.get_owned_sequences(dst, dst_schema, name, columns))

        if plan.where and count_rows:
            try:
                plan.estimated_rows = db.count_rows(src, src_schema, name, plan.where)
            except psycopg.Error as exc:
                # where 절 자체가 잘못된 경우: 이 테이블만 실패로 표시하고 계속 진행
                src.rollback()
                plan.precheck_status = "failed"
                plan.precheck_message = "where 절 오류: " + _first_line(exc)
                continue
        elif not plan.where:
            plan.estimated_rows = db.estimate_rows(src, src_schema, name)

    truncate_targets = [
        p.name for p in plans if p.mode == "truncate" and p.precheck_status is None
    ]
    return MigrationPlan(tables=plans, truncate_targets=truncate_targets, warnings=warnings)


def _truncate(
    config: MigrationConfig, dst: psycopg.Connection, targets: list[str], on_event: EventListener
) -> None:
    """truncate 모드 테이블을 한 문장으로 비운다. 외부 참조가 있으면 cascade 옵션이 없는 한 에러."""
    if not targets:
        return
    schema = config.destination.schema
    external = db.get_external_referencers(dst, schema, set(targets))
    if external and not config.truncate_cascade:
        detail = ", ".join(f"{child} -> {parent}" for child, parent in external)
        raise MigrationError(
            "복사 대상이 아닌 테이블이 truncate 대상 테이블을 FK 로 참조하고 있어 TRUNCATE 할 수 없습니다: "
            f"{detail} (truncate_cascade: true 로 설정하면 참조 테이블도 함께 비웁니다)"
        )
    if external:
        for child, parent in external:
            on_event(Event("warning", f"CASCADE 로 {child} 도 함께 비워집니다 ({parent} 참조)"))

    stmt = sql.SQL("TRUNCATE TABLE {}").format(
        sql.SQL(", ").join(db.qualified(schema, t) for t in targets)
    )
    if config.truncate_cascade:
        stmt = stmt + sql.SQL(" CASCADE")
    try:
        with dst.cursor() as cur:
            cur.execute(stmt)
        dst.commit()
    except Exception:
        dst.rollback()
        raise
    on_event(Event("truncate", f"TRUNCATE 완료: {', '.join(targets)}"))


def execute_plan(
    config: MigrationConfig,
    plan: MigrationPlan,
    src: psycopg.Connection,
    dst: psycopg.Connection,
    on_event: EventListener = _noop,
) -> MigrationResult:
    """계획을 실행한다. 테이블마다 별도 트랜잭션이며, 실패한 테이블과 그 하위 테이블은 건너뛴다."""
    for w in plan.warnings:
        on_event(Event("warning", w))

    _truncate(config, dst, plan.truncate_targets, on_event)

    # 소스는 하나의 REPEATABLE READ 스냅샷에서 읽어 테이블 간 일관성을 유지.
    # psycopg 가 첫 쿼리에서 암묵적으로 트랜잭션을 열므로 속성만 설정한다.
    src.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    src.read_only = True

    results: list[TableResult] = []
    failed: set[str] = set()

    try:
        for table in plan.tables:
            if table.precheck_status is not None:
                result = TableResult(table.name, table.precheck_status, message=table.precheck_message)
                if table.precheck_status == "failed":
                    failed.add(table.name)
                results.append(result)
                on_event(Event("table_done", result.message, table=table.name, result=result))
                continue

            failed_parents = sorted(p for p in table.parents if p in failed)
            if failed_parents:
                result = TableResult(
                    table.name, "skipped", message="상위 테이블 실패로 건너뜀: " + ", ".join(failed_parents)
                )
                failed.add(table.name)  # 하위 테이블에도 전파
                results.append(result)
                on_event(Event("table_done", result.message, table=table.name, result=result))
                continue

            on_event(Event("table_start", f"{table.mode} 복사 시작", table=table.name))
            started = time.monotonic()
            try:
                rows = copy_table(
                    src,
                    dst,
                    config.source.schema,
                    config.destination.schema,
                    table,
                    disable_triggers=config.disable_triggers,
                    reset_sequences=config.reset_sequences,
                )
                dst.commit()
                result = TableResult(table.name, "success", rows=rows, elapsed=time.monotonic() - started)
            except Exception as exc:  # noqa: BLE001 - 테이블 단위로 격리하고 계속 진행
                dst.rollback()
                # 소스 쪽 COPY 가 중간에 끊기면 트랜잭션이 깨질 수 있으므로 정리한다 (다음 쿼리에서 새 스냅샷)
                src.rollback()
                failed.add(table.name)
                result = TableResult(
                    table.name, "failed", elapsed=time.monotonic() - started, message=_first_line(exc)
                )
            results.append(result)
            on_event(Event("table_done", result.message, table=table.name, result=result))
    finally:
        src.rollback()

    return MigrationResult(results=results, warnings=list(plan.warnings))


def run_migration(
    config: MigrationConfig,
    *,
    dry_run: bool = False,
    on_event: EventListener = _noop,
) -> tuple[MigrationPlan, MigrationResult | None]:
    """설정으로 연결을 열고 계획을 세운 뒤, dry_run 이 아니면 실행한다."""
    with (
        db.connect(config.source.dsn, config.source.schema) as src,
        db.connect(config.destination.dsn, config.destination.schema) as dst,
    ):
        plan = build_plan(config, src, dst, count_rows=dry_run and config.count_rows_on_dry_run)
        src.rollback()  # 메타데이터 조회로 열린 트랜잭션 정리
        dst.rollback()
        if dry_run:
            for w in plan.warnings:
                on_event(Event("warning", w))
            return plan, None
        result = execute_plan(config, plan, src, dst, on_event)
        return plan, result
