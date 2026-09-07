"""테이블 하나를 소스에서 대상으로 복사.

두 가지 전송 방식을 지원한다.
- stream: 소스 COPY TO 출력을 대상 COPY FROM 입력으로 그대로 흘려보낸다 (copy_table).
- file  : 소스를 로컬 파일로 내려받은 뒤 (dump_table) 그 파일을 대상으로 올린다 (load_table).
          두 단계가 분리되어 있어 한쪽 연결이 끊겨도 그 단계만 다시 하면 된다.
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

import psycopg
from psycopg import sql

from .db import qualified
from .models import TablePlan

# 파일에서 대상으로 올릴 때 한 번에 읽는 크기
_FILE_CHUNK = 1024 * 1024


def _column_list(columns: tuple[str, ...]) -> sql.Composed:
    return sql.SQL(", ").join(sql.Identifier(c) for c in columns)


def _source_copy_sql(schema: str, plan: TablePlan) -> sql.Composed:
    select = sql.SQL("SELECT {} FROM {}").format(
        _column_list(plan.columns), qualified(schema, plan.name)
    )
    if plan.where:
        select = select + sql.SQL(" WHERE ") + sql.SQL(plan.where)
    return sql.SQL("COPY ({}) TO STDOUT").format(select)


@dataclass
class Progress:
    """전송 진행 상태 스냅샷."""

    rows: int
    bytes: int
    phase: Literal["read", "write"]  # 지금 어느 쪽을 기다리는 중인지 (read: 소스/파일 읽기, write: 대상/파일 쓰기)
    idle_seconds: float  # 마지막으로 데이터가 흐른 뒤 지난 시간


ProgressCallback = Callable[[Progress], None]


class StallError(Exception):
    """stall_timeout 동안 데이터가 흐르지 않아 전송을 강제 중단했을 때 발생."""


def _shutdown_socket(conn: psycopg.Connection) -> None:
    """다른 스레드에서 소켓 대기 중인 연결을 깨우기 위해 소켓을 닫는다.

    close() 와 달리 shutdown() 은 블로킹된 recv/select 를 확실히 깨운다.
    fd 소유권은 psycopg 에 있으므로 detach 로 파이썬 소켓 객체만 버린다.
    """
    try:
        sock = socket.socket(fileno=conn.pgconn.socket)
    except OSError:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    finally:
        sock.detach()


class _StreamState:
    """전송 스레드와 감시 스레드가 공유하는 카운터."""

    def __init__(self) -> None:
        self.rows = 0
        self.bytes = 0
        self.phase: Literal["read", "write"] = "read"
        self.last_data_at = time.monotonic()

    def record(self, chunk: bytes | memoryview) -> None:
        """chunk 하나를 전송한 뒤 카운터를 갱신.

        텍스트 COPY 포맷은 행마다 개행 하나이고 데이터 안의 개행은 이스케이프되므로,
        개행 수를 세면 파싱 없이 행 수를 알 수 있다. psycopg 는 memoryview 로 주므로 bytes 로 바꿔 센다.
        """
        self.rows += bytes(chunk).count(b"\n")
        self.bytes += len(chunk)
        self.last_data_at = time.monotonic()

    def snapshot(self) -> Progress:
        return Progress(
            rows=self.rows,
            bytes=self.bytes,
            phase=self.phase,
            idle_seconds=time.monotonic() - self.last_data_at,
        )


class _Watchdog:
    """전송 중 진행 보고와 정지 감지를 담당하는 감시 스레드.

    progress_interval 초마다 진행 상황을 보고하고, stall_timeout 초 동안 데이터가 전혀 흐르지 않으면
    지정된 연결의 소켓을 닫아 본 스레드를 깨운다. 소켓 대기로 본 스레드가 막혀 있어도
    감시 스레드는 계속 돌기 때문에 무한 대기가 없다.
    """

    def __init__(
        self,
        state: _StreamState,
        connections: list[psycopg.Connection],
        on_progress: ProgressCallback | None,
        progress_interval: float,
        stall_timeout: float,
    ) -> None:
        self.state = state
        self.connections = connections
        self.on_progress = on_progress
        self.progress_interval = progress_interval
        self.stall_timeout = stall_timeout
        self.stalled = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def _report_enabled(self) -> bool:
        return self.on_progress is not None and self.progress_interval > 0

    def _run(self) -> None:
        # 1초 단위로 깨어나 보고 주기와 정지 여부를 확인한다.
        tick = min(1.0, self.progress_interval) if self._report_enabled else 1.0
        last_report = time.monotonic()
        while not self._stop.wait(tick):
            snap = self.state.snapshot()
            if self.stall_timeout > 0 and snap.idle_seconds >= self.stall_timeout:
                self.stalled.set()
                for conn in self.connections:
                    _shutdown_socket(conn)
                return
            if self._report_enabled and time.monotonic() - last_report >= self.progress_interval:
                self.on_progress(snap)  # type: ignore[misc]
                last_report = time.monotonic()

    def __enter__(self) -> _Watchdog:
        if self._report_enabled or self.stall_timeout > 0:
            self._thread = threading.Thread(target=self._run, name="copy-watchdog", daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def translate(self, exc: BaseException, waiting: dict[str, str]) -> BaseException:
        """정지 감지로 소켓을 닫아서 난 예외라면 StallError 로 바꾼다. 아니면 원래 예외를 돌려준다."""
        if not self.stalled.is_set():
            return exc
        snap = self.state.snapshot()
        return StallError(
            f"{self.stall_timeout:.0f}초 동안 데이터가 없어 중단 ({waiting[snap.phase]} 대기 중, "
            f"{snap.rows:,} 행 전송된 상태). 연결을 끊고 재접속합니다"
        )


def _stream(
    src_cur: psycopg.Cursor,
    dst_cur: psycopg.Cursor,
    src_sql: sql.Composed,
    dst_sql: sql.Composed,
    on_progress: ProgressCallback | None = None,
    progress_interval: float = 5.0,
    stall_timeout: float = 0.0,
) -> None:
    """소스 COPY TO 출력을 대상 COPY FROM 입력으로 그대로 흘려보낸다."""
    state = _StreamState()
    connections = [src_cur.connection, dst_cur.connection]
    with _Watchdog(state, connections, on_progress, progress_interval, stall_timeout) as wd:
        try:
            with src_cur.copy(src_sql) as src_copy, dst_cur.copy(dst_sql) as dst_copy:
                for chunk in src_copy:
                    state.phase = "write"
                    dst_copy.write(chunk)
                    state.record(chunk)
                    state.phase = "read"
        except Exception as exc:
            raise wd.translate(exc, {"read": "소스 수신", "write": "대상 전송"}) from exc


def dump_table(
    src: psycopg.Connection,
    src_schema: str,
    plan: TablePlan,
    path: Path,
    on_progress: ProgressCallback | None = None,
    progress_interval: float = 5.0,
    stall_timeout: float = 0.0,
) -> int:
    """소스 테이블을 COPY 텍스트 포맷으로 path 에 내려받고 행 수를 반환.

    쓰는 동안은 path + ".part" 에 쓰고 끝나면 path 로 이름을 바꾸므로, path 가 존재하면 완전한 파일이다.
    실패하면 .part 파일은 지운다.
    """
    part = path.with_name(path.name + ".part")
    state = _StreamState()
    try:
        with (
            src.cursor() as cur,
            part.open("wb") as f,
            _Watchdog(state, [src], on_progress, progress_interval, stall_timeout) as wd,
        ):
            try:
                with cur.copy(_source_copy_sql(src_schema, plan)) as src_copy:
                    for chunk in src_copy:
                        state.phase = "write"
                        f.write(chunk)
                        state.record(chunk)
                        state.phase = "read"
            except Exception as exc:
                raise wd.translate(exc, {"read": "소스 수신", "write": "파일 쓰기"}) from exc
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    part.replace(path)
    return state.rows


def _upload_file(
    dst_cur: psycopg.Cursor,
    dst_sql: sql.Composed,
    path: Path,
    on_progress: ProgressCallback | None,
    progress_interval: float,
    stall_timeout: float,
) -> None:
    """로컬 파일 내용을 대상 COPY FROM 으로 올린다."""
    state = _StreamState()
    with (
        path.open("rb") as f,
        _Watchdog(state, [dst_cur.connection], on_progress, progress_interval, stall_timeout) as wd,
    ):
        try:
            with dst_cur.copy(dst_sql) as dst_copy:
                while chunk := f.read(_FILE_CHUNK):
                    state.phase = "write"
                    dst_copy.write(chunk)
                    state.record(chunk)
                    state.phase = "read"
                # 파일을 다 읽은 뒤에는 대상이 COPY 를 마무리하기를 기다린다
                state.phase = "write"
        except Exception as exc:
            raise wd.translate(exc, {"read": "파일 읽기", "write": "대상 전송"}) from exc


def _upsert_sql(schema: str, plan: TablePlan, tmp_name: str) -> sql.Composed:
    assert plan.conflict_columns is not None
    target = qualified(schema, plan.name)
    cols = _column_list(plan.columns)
    conflict_set = set(plan.conflict_columns)
    update_cols = [c for c in plan.columns if c not in conflict_set]

    insert = sql.SQL("INSERT INTO {} ({}) ").format(target, cols)
    if plan.has_identity_always:
        insert = insert + sql.SQL("OVERRIDING SYSTEM VALUE ")
    insert = insert + sql.SQL("SELECT {} FROM {} ON CONFLICT ({}) ").format(
        cols, sql.Identifier(tmp_name), _column_list(plan.conflict_columns)
    )
    if update_cols:
        assignments = sql.SQL(", ").join(
            sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(c), sql.Identifier(c))
            for c in update_cols
        )
        insert = insert + sql.SQL("DO UPDATE SET ") + assignments
    else:
        insert = insert + sql.SQL("DO NOTHING")
    return insert


# 대상 커서와 COPY FROM 문장을 받아 실제 데이터를 밀어 넣는 함수
_Feeder = Callable[[psycopg.Cursor, sql.Composed], None]


def _write_table(
    dst: psycopg.Connection,
    dst_schema: str,
    plan: TablePlan,
    feed: _Feeder,
    disable_triggers: bool,
    reset_sequences: bool,
    on_phase: Callable[[str], None] | None,
) -> int:
    """대상 쪽 처리(트리거, upsert 임시 테이블, 시퀀스)를 감싸고 feed 로 데이터를 넣는다. 행 수를 반환.

    대상 쪽 트랜잭션은 호출자가 시작/커밋/롤백을 관리한다. 이 함수는 예외를 그대로 던진다.
    """
    target = qualified(dst_schema, plan.name)

    with dst.cursor() as dst_cur:
        if disable_triggers:
            dst_cur.execute(sql.SQL("ALTER TABLE {} DISABLE TRIGGER USER").format(target))

        if plan.mode == "upsert":
            # 임시 테이블에 COPY 한 뒤 ON CONFLICT 로 병합
            tmp_name = f"_migration_tmp_{plan.name}"
            dst_cur.execute(
                sql.SQL("CREATE TEMP TABLE {} ON COMMIT DROP AS SELECT {} FROM {} WHERE false").format(
                    sql.Identifier(tmp_name), _column_list(plan.columns), target
                )
            )
            dst_sql = sql.SQL("COPY {} ({}) FROM STDIN").format(
                sql.Identifier(tmp_name), _column_list(plan.columns)
            )
            feed(dst_cur, dst_sql)
            if on_phase is not None:
                on_phase(f"전송 완료 ({max(dst_cur.rowcount, 0):,} 행), 대상 테이블에 병합 중")
            dst_cur.execute(_upsert_sql(dst_schema, plan, tmp_name))
            rows = dst_cur.rowcount
        else:
            dst_sql = sql.SQL("COPY {} ({}) FROM STDIN").format(target, _column_list(plan.columns))
            feed(dst_cur, dst_sql)
            rows = dst_cur.rowcount

        if disable_triggers:
            dst_cur.execute(sql.SQL("ALTER TABLE {} ENABLE TRIGGER USER").format(target))

        if reset_sequences and plan.sequences:
            if on_phase is not None:
                on_phase("시퀀스 재설정 중")
            for seq_name, column in plan.sequences:
                # 테이블이 비어있으면 다음 값이 1 이 되도록 is_called=false 로 설정
                dst_cur.execute(
                    sql.SQL("SELECT setval(%s, COALESCE(MAX({col}), 1), MAX({col}) IS NOT NULL) FROM {tbl}").format(
                        col=sql.Identifier(column), tbl=target
                    ),
                    (seq_name,),
                )

    return max(rows, 0)


def copy_table(
    src: psycopg.Connection,
    dst: psycopg.Connection,
    src_schema: str,
    dst_schema: str,
    plan: TablePlan,
    disable_triggers: bool = False,
    reset_sequences: bool = True,
    on_progress: ProgressCallback | None = None,
    on_phase: Callable[[str], None] | None = None,
    progress_interval: float = 5.0,
    stall_timeout: float = 0.0,
) -> int:
    """소스에서 대상으로 스트리밍 방식으로 테이블 하나를 복사하고 복사된 행 수를 반환.

    on_progress 는 전송 중 progress_interval 초마다, on_phase 는 단계가 바뀔 때 호출된다.
    stall_timeout 초 동안 데이터가 없으면 StallError 를 내며, 이때 양쪽 연결은 끊긴 상태가 된다.
    """
    src_sql = _source_copy_sql(src_schema, plan)
    with src.cursor() as src_cur:

        def feed(dst_cur: psycopg.Cursor, dst_sql: sql.Composed) -> None:
            _stream(src_cur, dst_cur, src_sql, dst_sql, on_progress, progress_interval, stall_timeout)

        return _write_table(dst, dst_schema, plan, feed, disable_triggers, reset_sequences, on_phase)


def load_table(
    dst: psycopg.Connection,
    dst_schema: str,
    plan: TablePlan,
    path: Path,
    disable_triggers: bool = False,
    reset_sequences: bool = True,
    on_progress: ProgressCallback | None = None,
    on_phase: Callable[[str], None] | None = None,
    progress_interval: float = 5.0,
    stall_timeout: float = 0.0,
) -> int:
    """dump_table 로 내려받은 파일을 대상 테이블에 올리고 복사된 행 수를 반환.

    stall_timeout 초 동안 대상이 데이터를 받지 않으면 StallError 를 내며, 이때 대상 연결은 끊긴 상태가 된다.
    """

    def feed(dst_cur: psycopg.Cursor, dst_sql: sql.Composed) -> None:
        _upload_file(dst_cur, dst_sql, path, on_progress, progress_interval, stall_timeout)

    return _write_table(dst, dst_schema, plan, feed, disable_triggers, reset_sequences, on_phase)
