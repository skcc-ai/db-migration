"""테이블 하나를 소스에서 대상으로 COPY 스트리밍으로 복사."""

from __future__ import annotations

import psycopg
from psycopg import sql

from .db import qualified
from .models import TablePlan


def _column_list(columns: tuple[str, ...]) -> sql.Composed:
    return sql.SQL(", ").join(sql.Identifier(c) for c in columns)


def _source_copy_sql(schema: str, plan: TablePlan) -> sql.Composed:
    select = sql.SQL("SELECT {} FROM {}").format(
        _column_list(plan.columns), qualified(schema, plan.name)
    )
    if plan.where:
        select = select + sql.SQL(" WHERE ") + sql.SQL(plan.where)
    return sql.SQL("COPY ({}) TO STDOUT").format(select)


def _stream(src_cur: psycopg.Cursor, dst_cur: psycopg.Cursor, src_sql: sql.Composed, dst_sql: sql.Composed) -> None:
    """소스 COPY TO 출력을 대상 COPY FROM 입력으로 그대로 흘려보낸다."""
    with src_cur.copy(src_sql) as src_copy:
        with dst_cur.copy(dst_sql) as dst_copy:
            for chunk in src_copy:
                dst_copy.write(chunk)


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


def copy_table(
    src: psycopg.Connection,
    dst: psycopg.Connection,
    src_schema: str,
    dst_schema: str,
    plan: TablePlan,
    disable_triggers: bool = False,
    reset_sequences: bool = True,
) -> int:
    """계획대로 테이블 하나를 복사하고 복사된 행 수를 반환.

    대상 쪽 트랜잭션은 호출자가 시작/커밋/롤백을 관리한다. 이 함수는 예외를 그대로 던진다.
    """
    target = qualified(dst_schema, plan.name)
    src_sql = _source_copy_sql(src_schema, plan)

    with src.cursor() as src_cur, dst.cursor() as dst_cur:
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
            _stream(src_cur, dst_cur, src_sql, dst_sql)
            dst_cur.execute(_upsert_sql(dst_schema, plan, tmp_name))
            rows = dst_cur.rowcount
        else:
            dst_sql = sql.SQL("COPY {} ({}) FROM STDIN").format(target, _column_list(plan.columns))
            _stream(src_cur, dst_cur, src_sql, dst_sql)
            rows = dst_cur.rowcount

        if disable_triggers:
            dst_cur.execute(sql.SQL("ALTER TABLE {} ENABLE TRIGGER USER").format(target))

        if reset_sequences:
            for seq_name, column in plan.sequences:
                # 테이블이 비어있으면 다음 값이 1 이 되도록 is_called=false 로 설정
                dst_cur.execute(
                    sql.SQL("SELECT setval(%s, COALESCE(MAX({col}), 1), MAX({col}) IS NOT NULL) FROM {tbl}").format(
                        col=sql.Identifier(column), tbl=target
                    ),
                    (seq_name,),
                )

    return max(rows, 0)
