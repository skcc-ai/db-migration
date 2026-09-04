"""CLI 진입점."""

from __future__ import annotations

import argparse
import sys

from .config import ConfigError, load_config
from .models import Event, MigrationPlan, MigrationResult
from .ordering import OrderingError
from .runner import MigrationError, run_migration


def _fmt_rows(n: int | None) -> str:
    return "-" if n is None else f"{n:,}"


def _print_plan(plan: MigrationPlan) -> None:
    print("\n[실행 계획]")
    header = f"{'#':>3}  {'table':<30} {'mode':<9} {'est.rows':>12}  notes"
    print(header)
    print("-" * len(header))
    for i, t in enumerate(plan.tables, 1):
        notes: list[str] = []
        if t.precheck_status:
            notes.append(f"[{t.precheck_status}] {t.precheck_message}")
        if t.where:
            notes.append(f"where: {t.where}")
        if t.parents:
            notes.append(f"after: {', '.join(t.parents)}")
        print(f"{i:>3}  {t.name:<30} {t.mode:<9} {_fmt_rows(t.estimated_rows):>12}  {' | '.join(notes)}")
    if plan.truncate_targets:
        print(f"\nTRUNCATE 대상: {', '.join(plan.truncate_targets)}")
    print()


def _print_summary(result: MigrationResult) -> None:
    print("\n[실행 결과]")
    header = f"{'table':<30} {'status':<8} {'rows':>12} {'sec':>8}  message"
    print(header)
    print("-" * len(header))
    for r in result.results:
        print(f"{r.name:<30} {r.status:<8} {_fmt_rows(r.rows):>12} {r.elapsed:>8.1f}  {r.message}")
    counts = {s: sum(1 for r in result.results if r.status == s) for s in ("success", "failed", "skipped")}
    print(f"\n성공 {counts['success']} / 실패 {counts['failed']} / 건너뜀 {counts['skipped']}")


def _make_listener(verbose: bool):
    def listener(event: Event) -> None:
        if event.kind == "warning":
            print(f"경고: {event.message}", file=sys.stderr)
        elif event.kind == "truncate":
            print(event.message)
        elif event.kind == "table_start":
            print(f"  {event.table}: {event.message}", flush=True)
        elif event.kind == "table_done" and event.result is not None:
            r = event.result
            if r.status == "success":
                print(f"  {r.name}: 완료 ({_fmt_rows(r.rows)} 행, {r.elapsed:.1f}s)")
            else:
                print(f"  {r.name}: {r.status} - {r.message}")
        elif verbose:
            print(event.message)

    return listener


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="db-migration",
        description="PostgreSQL 스키마 간 테이블 데이터 복사 도구",
    )
    parser.add_argument("-c", "--config", required=True, help="YAML 설정 파일 경로")
    parser.add_argument("--dry-run", action="store_true", help="실행 계획만 출력하고 아무것도 쓰지 않음")
    parser.add_argument("-v", "--verbose", action="store_true", help="상세 로그 출력")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        plan, result = run_migration(config, dry_run=args.dry_run, on_event=_make_listener(args.verbose))
    except (ConfigError, OrderingError, MigrationError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"예상치 못한 오류: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if args.dry_run or result is None:
        _print_plan(plan)
        return 0

    _print_summary(result)
    return 0 if result.succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
