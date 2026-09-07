# db-migration

PostgreSQL 스키마 간 테이블 데이터를 복사하는 CLI 도구입니다.
소스 DB 의 `COPY ... TO STDOUT` 출력을 대상 DB 의 `COPY ... FROM STDIN` 으로 그대로 스트리밍하므로
Python 에서 행을 파싱하지 않아 빠릅니다. 직접 연결이 자주 끊기는 환경에서는 로컬 파일을 거쳐
내려받기 / 올리기를 따로 재시도하는 `file` 전송 방식을 쓸 수 있습니다.

## 기능

- 전체 테이블 복사 (제외 테이블 지정 가능)
- 지정한 테이블만 복사
- 지정한 테이블을 WHERE 조건으로 걸러서 복사
- FK 의존성을 읽어 복사 순서를 자동 결정, 필요하면 수동 순서 지정
- 복사 모드: `truncate` / `append` / `upsert` (전역 설정 + 테이블별 override)
- 복사 후 serial / identity 시퀀스를 MAX 값에 맞춰 재설정
- 테이블 단위 트랜잭션. 실패한 테이블과 그 하위(FK 자식) 테이블은 건너뛰고 나머지는 계속 진행
- 연결이 끊기거나 멈추면 재접속해서 재시도 (`retries`)
- 전송 방식 선택: 직접 스트리밍(`stream`) 또는 로컬 파일 경유(`file`)
- dry-run 으로 실행 계획과 예상 행 수만 확인

## 전제 조건

- 대상 DB 에 테이블(스키마)이 이미 만들어져 있어야 합니다. 이 도구는 DDL 을 생성하지 않습니다.
- 소스 / 대상 모두 지정한 스키마 하나만 대상으로 합니다.
- WHERE 조건으로 걸러 복사할 때 참조 무결성(부모 행 존재 여부)은 사용자가 책임집니다.

## 설치 및 실행

```bash
uv sync
cp config.example.yaml config.yaml   # 테이블, 모드 등 설정 편집
cp .env.example .env                 # 접속 정보(비밀번호 등) 입력

uv run db-migration --dry-run                  # 계획만 확인 (현재 폴더의 config.yaml 사용)
uv run db-migration                            # 실행
uv run db-migration -c other.yaml              # 다른 설정 파일 지정
uv run db-migration --env-file prod.env        # 다른 .env 사용
uv run db-migration --transfer file            # 로컬 파일을 거쳐 복사 (설정 파일의 transfer 를 덮어씀)
```

종료 코드: 0 성공, 1 실패한 테이블 있음, 2 설정 오류 또는 실행 전 중단.

`config.yaml` 과 `.env` 는 `.gitignore` 에 포함되어 있어 커밋되지 않습니다.

## 설정 파일

전체 예제와 설명은 `config.example.yaml` 을 참고하세요.

```yaml
source:
  host: ${SOURCE_HOST:-localhost}   # ${VAR} 또는 ${VAR:-기본값}
  port: 5432
  user: app
  password: ${SOURCE_PASSWORD}
  database: source_db
  schema: public
destination:
  host: dst-host
  port: 5432
  user: app
  password: ${DEST_PASSWORD}
  database: target_db
  schema: public

mode: append               # truncate | append | upsert
copy_all: false            # true 면 exclude 를 제외한 전체 테이블
exclude: [audit_log]

tables:
  - users
  - name: orders
    where: "created_at >= '2024-01-01'"
    mode: upsert
  - name: order_items
    where: "order_id IN (SELECT id FROM orders WHERE created_at >= '2024-01-01')"

order: []                  # 수동 순서 (선택)
truncate_cascade: false
disable_triggers: false
reset_sequences: true
count_rows_on_dry_run: true
```

| 항목 | 설명 |
|---|---|
| `source` / `destination` | `host`, `port`, `user`, `password`, `database`, `schema`. `database` 만 필수이고 나머지는 libpq 기본값(`PGHOST` 등 환경변수 포함)을 따름. 대신 `dsn` 한 줄로 적어도 됨 |
| 환경변수 치환 | 문자열 값 어디서나 `${VAR}` (없으면 에러) 또는 `${VAR:-기본값}` (없거나 비어 있으면 기본값) 사용 가능 |
| `.env` | 설정 파일과 같은 폴더, 그 다음 현재 폴더의 `.env` 를 자동으로 읽어 환경변수로 사용. `--env-file` 로 직접 지정 가능. 셸에 이미 있는 환경변수가 `.env` 보다 우선 |
| `mode` | 기본 복사 모드. 테이블별 `mode` 로 override 가능 |
| `copy_all` | `true` 면 소스 스키마 전체 테이블 (파티션 자식 제외). `false` 면 `tables` 에 적힌 것만 |
| `exclude` | `copy_all: true` 일 때 제외할 테이블 |
| `tables` | 복사 대상 목록 또는 테이블별 override. 문자열 하나만 적으면 이름만 지정 |
| `tables[].where` | 소스 테이블에 적용할 WHERE 절 (SQL 그대로). 서브쿼리에서 스키마 없이 테이블명을 써도 소스 스키마로 해석됨 |
| `order` | 수동 순서. 여기 적힌 테이블이 먼저 이 순서대로, 나머지는 FK 기준 자동 정렬 |
| `truncate_cascade` | truncate 대상을 복사 대상이 아닌 테이블이 FK 로 참조할 때 그 테이블까지 비울지 여부 |
| `disable_triggers` | 복사 중 대상 테이블의 사용자 트리거 비활성화 (테이블 소유자 권한 필요) |
| `reset_sequences` | 복사 후 컬럼에 연결된 시퀀스를 MAX 값으로 재설정 |
| `count_rows_on_dry_run` | dry-run 에서 where 조건이 있는 테이블의 실제 count 수행 여부 |
| `stall_timeout` | 이 시간(초) 동안 데이터가 전혀 흐르지 않으면 양쪽 연결을 끊어 해당 테이블을 실패 처리하고, 다시 연결해 다음 테이블을 진행. 기본 300, 0 이면 무제한 대기. 풀러나 네트워크 장비가 백엔드 세션만 끊고 클라이언트 연결은 살려두는 경우에 무한 대기를 막음 |
| `progress_interval` | 복사 중 진행 상황(전송 행 수, 용량) 출력 간격(초). 기본 5, 0 이면 출력 안 함. 데이터가 흐르지 않으면 "N초째 데이터 없음, 소스 수신/대상 전송 대기 중" 으로 표시되어 느린 것과 멈춘 것을 구분할 수 있음 |
| `retries` | 연결 오류(끊김, `stall_timeout` 정지)로 실패했을 때 재접속 후 다시 시도하는 횟수. 기본 3, 0 이면 재시도 없음. SQL 오류(제약 위반 등)는 재시도하지 않음 |
| `transfer` | 전송 방식. `stream`(기본) 은 소스에서 대상으로 직접 스트리밍, `file` 은 로컬 파일을 거침. 아래 "전송 방식" 참고 |
| `spool_dir` | `file` 전송에서 내려받은 파일을 둘 폴더. 생략하면 시스템 임시 폴더 아래 `db-migration` |
| `keep_spool_files` | `file` 전송에서 올리기가 끝난 파일을 남길지 여부. 기본 false |

## 복사 모드별 동작

| 모드 | 동작 |
|---|---|
| `truncate` | 실행 시작 시 truncate 모드인 테이블 전체를 `TRUNCATE a, b, c` 한 문장으로 비운 뒤 복사. 복사 대상이 아닌 테이블이 FK 로 참조 중이면 에러로 중단 (`truncate_cascade: true` 면 함께 비움) |
| `append` | 기존 데이터를 두고 그대로 추가 |
| `upsert` | 임시 테이블에 COPY 한 뒤 `INSERT ... ON CONFLICT (PK) DO UPDATE`. PK 가 없으면 첫 unique 제약을 사용하고, 둘 다 없으면 해당 테이블만 실패 처리 |

## 전송 방식

| 방식 | 동작 |
|---|---|
| `stream` | 소스 `COPY TO STDOUT` 을 대상 `COPY FROM STDIN` 으로 바로 흘려보냅니다. 가장 빠르지만 소스와 대상 연결이 동시에 살아 있어야 하고, 중간에 끊기면 그 테이블을 처음부터 다시 합니다 |
| `file` | 테이블마다 소스를 로컬 파일(COPY 텍스트 포맷)로 내려받은 뒤 그 파일을 대상으로 올립니다. 내려받기와 올리기를 따로 재시도하므로, 올리기 중 끊겨도 소스를 다시 읽지 않고 파일에서 다시 올립니다. 느리더라도 안정적으로 끝내야 할 때 사용하세요 |

`file` 전송의 세부 동작:

- 파일은 `spool_dir/<소스스키마>.<테이블>.copy` 에 저장되고, 옆에 `.meta` (컬럼, where, 행 수, 내려받은 시각) 가 생깁니다. 쓰는 중에는 `.part` 이름으로 쓰다가 끝나면 이름을 바꾸므로, `.copy` 파일이 있으면 완전한 파일입니다.
- 올리기까지 성공하면 파일을 지웁니다 (`keep_spool_files: true` 면 남김). 올리기가 실패하면 파일을 남겨 둡니다.
- 실행을 시작할 때 이미 완전한 파일이 있고 컬럼과 where 가 지금 설정과 같으면 다시 내려받지 않고 그 파일을 재사용하며 경고로 알려 줍니다. 프로세스가 중간에 죽었거나 올리기만 실패한 경우 다음 실행에서 이어서 진행할 수 있습니다. 파일은 내려받은 시점의 데이터이므로 최신 데이터가 필요하면 그 파일을 지우고 실행하세요.
- 재시도 중에도 내려받기 실패는 대상을 건드리지 않고, 올리기 실패는 소스 스냅샷을 유지합니다.
- 디스크에 테이블 크기만큼 여유가 필요합니다. 큰 테이블이면 `spool_dir` 을 넉넉한 곳으로 지정하세요.

## 순서 결정과 실패 처리

- 소스와 대상 양쪽의 FK 를 합쳐 위상 정렬합니다. 같은 단계에서는 이름순입니다.
- 순환 참조가 있으면 실행 전에 에러가 나며, `order` 에 순환에 속한 테이블 순서를 직접 적으면 해결됩니다.
- 소스는 REPEATABLE READ 스냅샷 하나에서 읽어 테이블 간 일관성을 유지합니다.
- 대상은 테이블마다 별도 트랜잭션입니다. 실패한 테이블은 롤백되고, 그 테이블을 FK 로 참조하는 하위 테이블은 자동으로 건너뜁니다.
- 연결이 끊기거나 `stall_timeout` 에 걸리면 재접속한 뒤 `retries` 만큼 같은 테이블(또는 `file` 전송의 해당 단계)을 다시 시도합니다. 소스를 재접속하면 이후 테이블은 새 스냅샷에서 읽습니다.
- 실행 전 검사에서 걸리는 항목: 대상에 테이블 없음(`copy_all` 이면 경고 후 건너뜀, 명시 지정이면 실패), 소스 컬럼이 대상에 없음, upsert 인데 PK/unique 없음, where 절 문법 오류.
- 대상에 추가 컬럼이 있으면 default 값으로 채워집니다. generated 컬럼은 양쪽 모두 복사에서 제외됩니다.

## 연결 설정

- 모든 연결에 TCP keepalive 를 켭니다 (30초 유휴 후 10초 간격 3회, 약 1분 안에 끊긴 연결 감지). VPN 이나 방화벽이 유휴 연결을 조용히 끊어도 무한 대기하지 않고 해당 테이블이 실패 처리됩니다.
- 접속 대기는 15초, `application_name` 은 `db-migration` 으로 설정되어 `pg_stat_activity` 에서 찾기 쉽습니다.
- 위 값들은 `dsn` 에 같은 키를 직접 적으면 덮어쓸 수 있습니다. 예: `dsn: "keepalives_idle=60 ..."`.

## 프로그램에서 사용하기

CLI 외에 core 함수를 직접 호출할 수 있습니다. 진행 상황은 `on_event` 콜백으로 전달되므로 UI 를 붙이기 쉽습니다.

```python
from db_migration import load_config, run_migration

config = load_config("config.yaml")
plan, result = run_migration(config, dry_run=False, on_event=print)
print(result.succeeded)
```

## 테스트

단위 테스트는 DB 없이 동작하고, 통합 테스트는 PostgreSQL 이 있을 때만 실행됩니다.

```bash
docker run -d --name db-migration-test \
  -e POSTGRES_USER=test -e POSTGRES_PASSWORD=test -e POSTGRES_DB=test \
  -p 15432:5432 postgres:16

uv run pytest
# 다른 DB 를 쓰려면: DB_MIGRATION_TEST_DSN=postgresql://... uv run pytest
```

## 구조

```
db_migration/
  config.py    YAML 설정 로드 및 검증
  db.py        메타데이터 조회 (테이블, 컬럼, PK, FK, 시퀀스)
  ordering.py  FK 위상 정렬
  copier.py    테이블 하나 복사: 스트리밍(copy_table), 파일 내려받기(dump_table) / 올리기(load_table)
  runner.py    계획 수립, TRUNCATE, 테이블별 트랜잭션 실행, 재시도와 파일 경유 흐름
  models.py    계획 / 결과 / 이벤트 데이터 구조
  cli.py       CLI 진입점
```
