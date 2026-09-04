"""PostgreSQL 메타데이터 조회 헬퍼."""

from __future__ import annotations

from dataclasses import dataclass

import psycopg
from psycopg import sql


@dataclass(frozen=True)
class ColumnInfo:
    """컬럼 정보."""

    name: str
    is_generated: bool  # GENERATED ALWAYS AS (...) STORED
    identity: str  # '' | 'a'(ALWAYS) | 'd'(BY DEFAULT)


def connect(dsn: str, schema: str) -> psycopg.Connection:
    """autocommit 꺼진 상태로 연결하고 search_path 를 schema 로 고정. 트랜잭션은 호출자가 관리.

    search_path 를 고정해 두면 where 절 안에서 스키마 없이 테이블명을 써도 해당 스키마로 해석된다.
    """
    conn = psycopg.connect(dsn, autocommit=False)
    with conn.cursor() as cur:
        cur.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
    conn.commit()
    return conn


def qualified(schema: str, table: str) -> sql.Composed:
    """"schema"."table" 형태의 식별자."""
    return sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(table))


def qualified_text(schema: str, table: str) -> str:
    """regclass 캐스팅용 문자열 ("schema"."table")."""
    return f'"{schema}"."{table}"'


def schema_exists(conn: psycopg.Connection, schema: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (schema,))
        return cur.fetchone() is not None


def list_tables(conn: psycopg.Connection, schema: str) -> list[str]:
    """스키마 안의 일반 테이블 및 파티션 부모 테이블 목록 (파티션 자식은 제외)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.relname
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s
              AND c.relkind IN ('r', 'p')
              AND NOT c.relispartition
            ORDER BY c.relname
            """,
            (schema,),
        )
        return [row[0] for row in cur.fetchall()]


def get_columns(conn: psycopg.Connection, schema: str, table: str) -> list[ColumnInfo]:
    """테이블 컬럼 목록 (attnum 순)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.attname, a.attgenerated <> '', a.attidentity
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND c.relname = %s
              AND a.attnum > 0 AND NOT a.attisdropped
            ORDER BY a.attnum
            """,
            (schema, table),
        )
        return [ColumnInfo(name=r[0], is_generated=r[1], identity=r[2]) for r in cur.fetchall()]


def get_conflict_columns(conn: psycopg.Connection, schema: str, table: str) -> tuple[str, ...] | None:
    """upsert 의 ON CONFLICT 대상 컬럼. PK 우선, 없으면 첫 unique 제약. 둘 다 없으면 None."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT con.contype, array_agg(a.attname ORDER BY k.ord)
            FROM pg_constraint con
            JOIN pg_class c ON c.oid = con.conrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
            JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
            WHERE n.nspname = %s AND c.relname = %s
              AND con.contype IN ('p', 'u')
              AND NOT con.condeferrable
            GROUP BY con.oid, con.contype, con.conname
            ORDER BY (con.contype = 'p') DESC, con.conname
            LIMIT 1
            """,
            (schema, table),
        )
        row = cur.fetchone()
        return tuple(row[1]) if row else None


def get_foreign_keys(conn: psycopg.Connection, schema: str) -> list[tuple[str, str]]:
    """스키마 내부 FK 목록을 (자식 테이블, 부모 테이블) 쌍으로 반환. 자기 참조는 제외."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT child.relname, parent.relname
            FROM pg_constraint con
            JOIN pg_class child ON child.oid = con.conrelid
            JOIN pg_class parent ON parent.oid = con.confrelid
            JOIN pg_namespace cn ON cn.oid = child.relnamespace
            JOIN pg_namespace pn ON pn.oid = parent.relnamespace
            WHERE con.contype = 'f'
              AND cn.nspname = %s AND pn.nspname = %s
              AND child.oid <> parent.oid
            """,
            (schema, schema),
        )
        return [(r[0], r[1]) for r in cur.fetchall()]


def get_external_referencers(
    conn: psycopg.Connection, schema: str, tables: set[str]
) -> list[tuple[str, str]]:
    """tables 집합 밖에서 tables 안의 테이블을 FK 로 참조하는 (참조 테이블, 피참조 테이블) 목록.

    TRUNCATE 사전 검사용. 참조 테이블은 "schema.table" 형태.
    """
    if not tables:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT cn.nspname || '.' || child.relname, parent.relname
            FROM pg_constraint con
            JOIN pg_class child ON child.oid = con.conrelid
            JOIN pg_class parent ON parent.oid = con.confrelid
            JOIN pg_namespace cn ON cn.oid = child.relnamespace
            JOIN pg_namespace pn ON pn.oid = parent.relnamespace
            WHERE con.contype = 'f'
              AND pn.nspname = %s
              AND parent.relname = ANY(%s)
              AND NOT (cn.nspname = %s AND child.relname = ANY(%s))
            ORDER BY 1, 2
            """,
            (schema, list(tables), schema, list(tables)),
        )
        return [(r[0], r[1]) for r in cur.fetchall()]


def get_owned_sequences(
    conn: psycopg.Connection, schema: str, table: str, columns: list[str]
) -> list[tuple[str, str]]:
    """컬럼에 연결된 (serial/identity) 시퀀스를 (시퀀스 이름, 컬럼) 쌍으로 반환."""
    result: list[tuple[str, str]] = []
    with conn.cursor() as cur:
        for col in columns:
            cur.execute(
                "SELECT pg_get_serial_sequence(%s, %s)",
                (qualified_text(schema, table), col),
            )
            row = cur.fetchone()
            if row and row[0]:
                result.append((row[0], col))
    return result


def estimate_rows(conn: psycopg.Connection, schema: str, table: str) -> int | None:
    """통계 기반 추정 행 수. 분석된 적 없으면 None."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT reltuples::bigint FROM pg_class WHERE oid = %s::regclass",
            (qualified_text(schema, table),),
        )
        row = cur.fetchone()
        if row is None or row[0] < 0:
            return None
        return int(row[0])


def count_rows(conn: psycopg.Connection, schema: str, table: str, where: str | None) -> int:
    """실제 행 수 (조건 포함)."""
    query = sql.SQL("SELECT count(*) FROM {}").format(qualified(schema, table))
    if where:
        query = query + sql.SQL(" WHERE ") + sql.SQL(where)
    with conn.cursor() as cur:
        cur.execute(query)
        return int(cur.fetchone()[0])  # type: ignore[index]
