"""FK 의존성 기반 테이블 순서 결정 (위상 정렬 + 수동 순서 우선)."""

from __future__ import annotations

from collections import defaultdict


class OrderingError(Exception):
    """순환 참조 등으로 순서를 결정할 수 없을 때 발생."""


def resolve_order(
    tables: set[str],
    dependencies: list[tuple[str, str]],
    manual_order: tuple[str, ...] = (),
) -> list[str]:
    """복사 순서를 결정한다.

    - tables: 복사 대상 테이블 집합
    - dependencies: (자식, 부모) 쌍. 부모가 자식보다 먼저 와야 한다.
    - manual_order: 사용자가 지정한 순서. 여기 포함된 테이블은 지정된 순서대로 맨 앞에 배치되고,
      나머지는 FK 기준 위상 정렬로 뒤에 붙는다. 수동 지정된 테이블끼리의 FK 순서는 검사하지 않는다
      (사용자 책임).

    순환 참조가 있으면 OrderingError 를 발생시킨다. 순환에 속한 테이블을 manual_order 로
    지정하면 해결된다.
    """
    manual = [t for t in manual_order if t in tables]
    manual_set = set(manual)
    remaining = tables - manual_set

    # 자동 정렬 대상끼리의 의존성만 고려. 수동 지정된 부모는 이미 앞에 있으므로 제외.
    parents_of: dict[str, set[str]] = defaultdict(set)
    children_of: dict[str, set[str]] = defaultdict(set)
    for child, parent in dependencies:
        if child in remaining and parent in remaining:
            parents_of[child].add(parent)
            children_of[parent].add(child)

    indegree = {t: len(parents_of[t]) for t in remaining}
    ready = sorted(t for t, d in indegree.items() if d == 0)
    ordered: list[str] = []

    # Kahn 알고리즘. 같은 단계에서는 이름순으로 뽑아 결과를 결정적으로 만든다.
    while ready:
        current = ready.pop(0)
        ordered.append(current)
        newly_ready: list[str] = []
        for child in children_of[current]:
            indegree[child] -= 1
            if indegree[child] == 0:
                newly_ready.append(child)
        if newly_ready:
            ready = sorted(ready + newly_ready)

    if len(ordered) != len(remaining):
        cyclic = sorted(t for t, d in indegree.items() if d > 0)
        raise OrderingError(
            "테이블 간 순환 참조가 있어 순서를 자동으로 결정할 수 없습니다: "
            + ", ".join(cyclic)
            + " (설정의 order 에 순서를 직접 지정하세요)"
        )

    return manual + ordered
