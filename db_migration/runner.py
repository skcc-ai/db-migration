"""마이그레이션 계획 수립과 실행 오케스트레이션."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, TypeVar

import psycopg
from psycopg import sql

from . import db
from .config import MigrationConfig
from .copier import Progress, StallError, copy_table, dump_table, load_table
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


def _fmt_bytes(n: int) -> str:
    """바이트 수를 읽기 쉬운 단위로."""
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


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


class _Connections:
    """소스/대상 연결을 보관하고, 끊긴 연결을 다시 맺는다."""

    def __init__(self, config: MigrationConfig, src: psycopg.Connection, dst: psycopg.Connection) -> None:
        self.config = config
        self.src = src
        self.dst = dst

    @staticmethod
    def _is_broken(conn: psycopg.Connection) -> bool:
        return conn.closed or conn.broken

    def reconnect_if_broken(self, on_event: EventListener) -> None:
        """끊긴 연결을 감지해 다시 연결한다. 소스 재접속 시 스냅샷이 새로 잡힌다."""
        if self._is_broken(self.src):
            try:
                self.src.close()
            except Exception:  # noqa: BLE001
                pass
            self.src = db.connect(self.config.source.dsn, self.config.source.schema)
            _begin_source_snapshot(self.src)
            on_event(Event("warning", "소스 연결이 끊겨 다시 연결했습니다. 이후 테이블은 새 스냅샷에서 읽습니다"))
        if self._is_broken(self.dst):
            try:
                self.dst.close()
            except Exception:  # noqa: BLE001
                pass
            self.dst = db.connect(self.config.destination.dsn, self.config.destination.schema)
            on_event(Event("warning", "대상 연결이 끊겨 다시 연결했습니다"))

    def close(self) -> None:
        for conn in (self.src, self.dst):
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def _begin_source_snapshot(src: psycopg.Connection) -> None:
    """소스를 REPEATABLE READ 읽기 전용으로 설정. psycopg 가 첫 쿼리에서 암묵적으로 트랜잭션을 연다."""
    src.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    src.read_only = True


def _safe_rollback(conn: psycopg.Connection) -> None:
    """끊긴 연결에서도 예외 없이 롤백을 시도한다."""
    try:
        if not (conn.closed or conn.broken):
            conn.rollback()
    except Exception:  # noqa: BLE001
        pass


T = TypeVar("T")

# 재접속 후 다시 시도할 가치가 있는 오류. SQL 오류(제약 위반 등)는 다시 해도 같으므로 제외한다.
_RETRYABLE = (StallError, psycopg.OperationalError, psycopg.InterfaceError, ConnectionError, TimeoutError)


@dataclass
class _TableContext:
    """테이블 하나를 복사하는 동안 공유되는 것들."""

    config: MigrationConfig
    conns: _Connections
    table: TablePlan
    on_event: EventListener
    started: float

    def progress_handler(
        self, verb: str, waiting_for: dict[str, str]
    ) -> Callable[[Progress], None] | None:
        """progress_interval 마다 호출될 콜백.

        verb 는 '전송', '내려받기' 처럼 지금 하는 일, waiting_for 는 phase(read/write)별로
        데이터가 멈췄을 때 무엇을 기다리는 중인지 설명하는 문구.
        """
        if self.config.progress_interval <= 0:
            return None
        name, t0, interval = self.table.name, self.started, self.config.progress_interval

        def on_progress(p: Progress) -> None:
            elapsed = time.monotonic() - t0
            if p.idle_seconds >= interval:
                # 데이터가 흐르지 않는 상태. 어느 쪽을 기다리는지 같이 보여준다.
                waiting = waiting_for[p.phase]
                message = (
                    f"{p.rows:,} 행 / {_fmt_bytes(p.bytes)} {verb} 후 {p.idle_seconds:.0f}초째 데이터 없음, "
                    f"{waiting} 대기 중 ({elapsed:.0f}s)"
                )
            else:
                message = f"{p.rows:,} 행 / {_fmt_bytes(p.bytes)} {verb} 중 ({elapsed:.0f}s)"
            self.on_event(Event("table_progress", message, table=name, rows=p.rows, bytes=p.bytes))

        return on_progress

    def phase(self, message: str) -> None:
        self.on_event(Event("table_progress", message, table=self.table.name))

    def with_retries(self, what: str, fn: Callable[[], T], *, source: bool, dest: bool) -> T:
        """fn 을 실행하고, 연결 오류로 실패하면 재접속한 뒤 retries 만큼 다시 시도한다.

        source / dest 는 이 단계가 사용하는 연결. 실패 시 그 연결만 롤백해서, 예를 들어 올리기가
        실패했을 때 소스 스냅샷까지 버리지 않는다.
        """
        retries = self.config.retries
        for attempt in range(retries + 1):
            try:
                return fn()
            except _RETRYABLE as exc:
                if dest:
                    _safe_rollback(self.conns.dst)
                if source:
                    _safe_rollback(self.conns.src)
                if attempt >= retries:
                    raise
                self.on_event(
                    Event(
                        "warning",
                        f"{self.table.name}: {what} 실패 ({_first_line(exc)}). "
                        f"다시 시도합니다 ({attempt + 1}/{retries})",
                        table=self.table.name,
                    )
                )
                self.conns.reconnect_if_broken(self.on_event)
        raise AssertionError("unreachable")


def _copy_streaming(ctx: _TableContext) -> int:
    """소스에서 대상으로 직접 스트리밍. 실패하면 처음부터 다시 한다."""
    config, table = ctx.config, ctx.table

    def attempt() -> int:
        rows = copy_table(
            ctx.conns.src,
            ctx.conns.dst,
            config.source.schema,
            config.destination.schema,
            table,
            disable_triggers=config.disable_triggers,
            reset_sequences=config.reset_sequences,
            on_progress=ctx.progress_handler("전송", {"read": "소스에서 다음 데이터 수신", "write": "대상으로 전송"}),
            on_phase=ctx.phase,
            progress_interval=config.progress_interval,
            stall_timeout=config.stall_timeout,
        )
        ctx.conns.dst.commit()
        return rows

    return ctx.with_retries("복사", attempt, source=True, dest=True)


def _spool_path(config: MigrationConfig, table: str) -> Path:
    """테이블의 내려받기 파일 경로. 같은 폴더에 다른 소스 스키마의 파일이 섞여도 구분되도록 스키마를 붙인다."""
    return config.effective_spool_dir() / f"{config.source.schema}.{table}.copy"


def _spool_meta_path(path: Path) -> Path:
    return path.with_name(path.name + ".meta")


def _reusable_spool(path: Path, table: TablePlan) -> dict | None:
    """이전 실행에서 내려받은 완전한 파일이 있고 컬럼/where 가 같으면 그 메타를 반환."""
    meta_path = _spool_meta_path(path)
    if not path.exists() or not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if meta.get("columns") != list(table.columns) or meta.get("where") != table.where:
        return None
    return meta


def _copy_via_file(ctx: _TableContext) -> int:
    """소스 → 로컬 파일 → 대상. 내려받기와 올리기를 따로 재시도한다.

    올리기가 성공하면 파일을 지운다 (keep_spool_files 면 남김). 실패하면 남겨 두어 다음 실행에서 재사용한다.
    """
    config, table = ctx.config, ctx.table
    path = _spool_path(config, table.name)
    meta_path = _spool_meta_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    meta = _reusable_spool(path, table)
    if meta is not None:
        ctx.on_event(
            Event(
                "warning",
                f"{table.name}: 이전에 내려받은 파일을 재사용합니다 ({meta['rows']:,} 행, {meta['dumped_at']} 기준). "
                f"새로 받으려면 {path} 를 지우세요",
                table=table.name,
            )
        )
    else:
        ctx.phase(f"소스에서 내려받는 중 → {path}")
        t0 = time.monotonic()

        def download() -> int:
            return dump_table(
                ctx.conns.src,
                config.source.schema,
                table,
                path,
                on_progress=ctx.progress_handler("내려받기", {"read": "소스에서 다음 데이터 수신", "write": "파일 쓰기"}),
                progress_interval=config.progress_interval,
                stall_timeout=config.stall_timeout,
            )

        rows = ctx.with_retries("내려받기", download, source=True, dest=False)
        meta = {
            "table": table.name,
            "columns": list(table.columns),
            "where": table.where,
            "rows": rows,
            "dumped_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        ctx.phase(
            f"내려받기 완료 ({rows:,} 행, {_fmt_bytes(path.stat().st_size)}, {time.monotonic() - t0:.0f}s), "
            "대상으로 올리는 중"
        )

    def upload() -> int:
        rows = load_table(
            ctx.conns.dst,
            config.destination.schema,
            table,
            path,
            disable_triggers=config.disable_triggers,
            reset_sequences=config.reset_sequences,
            on_progress=ctx.progress_handler("올리기", {"read": "파일 읽기", "write": "대상 처리"}),
            on_phase=ctx.phase,
            progress_interval=config.progress_interval,
            stall_timeout=config.stall_timeout,
        )
        ctx.conns.dst.commit()
        return rows

    rows = ctx.with_retries("올리기", upload, source=False, dest=True)

    if not config.keep_spool_files:
        path.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
    return rows


def execute_plan(
    config: MigrationConfig,
    plan: MigrationPlan,
    src: psycopg.Connection,
    dst: psycopg.Connection,
    on_event: EventListener = _noop,
    conns: _Connections | None = None,
) -> MigrationResult:
    """계획을 실행한다. 테이블마다 별도 트랜잭션이며, 실패한 테이블과 그 하위 테이블은 건너뛴다.

    연결이 끊기면 (stall_timeout 포함) 다시 연결해서 다음 테이블을 계속 진행한다.
    재접속으로 연결 객체가 바뀔 수 있으므로, 호출자가 conns 를 넘기면 거기에 최신 연결이 남는다.
    """
    for w in plan.warnings:
        on_event(Event("warning", w))

    _truncate(config, dst, plan.truncate_targets, on_event)

    if conns is None:
        conns = _Connections(config, src, dst)
    # 소스는 하나의 REPEATABLE READ 스냅샷에서 읽어 테이블 간 일관성을 유지
    _begin_source_snapshot(conns.src)

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

            via = " (파일 경유)" if config.transfer == "file" else ""
            on_event(Event("table_start", f"{table.mode} 복사 시작{via}", table=table.name))
            started = time.monotonic()
            ctx = _TableContext(config, conns, table, on_event, started)

            try:
                if config.transfer == "file":
                    rows = _copy_via_file(ctx)
                else:
                    rows = _copy_streaming(ctx)
                result = TableResult(table.name, "success", rows=rows, elapsed=time.monotonic() - started)
            except Exception as exc:  # noqa: BLE001 - 테이블 단위로 격리하고 계속 진행
                _safe_rollback(conns.dst)
                # 소스 쪽 COPY 가 중간에 끊기면 트랜잭션이 깨질 수 있으므로 정리한다 (다음 쿼리에서 새 스냅샷)
                _safe_rollback(conns.src)
                failed.add(table.name)
                result = TableResult(
                    table.name, "failed", elapsed=time.monotonic() - started, message=_first_line(exc)
                )
                results.append(result)
                on_event(Event("table_done", result.message, table=table.name, result=result))
                conns.reconnect_if_broken(on_event)
                continue
            results.append(result)
            on_event(Event("table_done", result.message, table=table.name, result=result))
    finally:
        _safe_rollback(conns.src)

    return MigrationResult(results=results, warnings=list(plan.warnings))


def run_migration(
    config: MigrationConfig,
    *,
    dry_run: bool = False,
    on_event: EventListener = _noop,
) -> tuple[MigrationPlan, MigrationResult | None]:
    """설정으로 연결을 열고 계획을 세운 뒤, dry_run 이 아니면 실행한다."""
    src = db.connect(config.source.dsn, config.source.schema)
    try:
        dst = db.connect(config.destination.dsn, config.destination.schema)
    except Exception:
        src.close()
        raise
    conns = _Connections(config, src, dst)
    try:
        plan = build_plan(config, src, dst, count_rows=dry_run and config.count_rows_on_dry_run)
        src.rollback()  # 메타데이터 조회로 열린 트랜잭션 정리
        dst.rollback()
        if dry_run:
            for w in plan.warnings:
                on_event(Event("warning", w))
            return plan, None
        result = execute_plan(config, plan, src, dst, on_event, conns=conns)
        return plan, result
    finally:
        conns.close()
