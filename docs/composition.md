# 조합 가능한 실행 원장

라이브러리의 단위는 명시적인 외부 효과 하나다. 호출부를 `executor.bind(effect, handler)`로
연결하고, 저장소·업무 ID·복구 정책·프레임워크 연결을 각각 선택한다. 커뮤니티 확장은 이
계약을 구현하는 저장소와 제공자 어댑터로 추가할 수 있다. 효과 경계와 업무 ID의 책임까지
자동 추론하지는 않는다.

기존 LangChain 툴에는 [ExecutionBoundary](langchain-boundary.md)를 진입점으로 사용한다.
아래 bind/저장소/복구 계약은 그 경계와 독립적인 코어 API다.

```text
호스트 업무 ID ─┐
LangGraph ─────┼─ EffectExecutor ─ OperationStore ─ SQLite / Postgres / 사용자 구현
MCP 서버 ──────┘       │
                      ├─ bind(effect, handler) ─ 외부 제공자
                      └─ recover() ─ RecoveryPolicy ─ 버전 확인 후 결정 저장
```

## 저장소 교체

```python
from langgraph_effect_ledger import EffectExecutor, SQLiteOperationStore

executor = EffectExecutor(store=SQLiteOperationStore("effects.sqlite"), scope="account-1")
# 이전 호출 방식도 같은 SQLite 구현을 사용한다.
executor = EffectExecutor("effects.sqlite", scope="account-1")
```

Postgres는 선택 의존성이다. 코어 설치는 psycopg·LangChain·MCP를 로드하지 않는다.

```python
import os
from langgraph_effect_ledger import EffectExecutor
from langgraph_effect_ledger.postgres import PostgresOperationStore

executor = EffectExecutor(
    store=PostgresOperationStore(os.environ["EFFECT_LEDGER_DSN"]),
    scope="account-1",
)
```

Postgres용 DB 또는 전용 search_path를 준비한다. 생성자는 `operations`, `decisions` 테이블이
없으면 만들며 기존 스키마를 마이그레이션하지 않는다. DB 계정에 필요한 권한을 부여하고,
모든 워커가 같은 DB·스키마·scope를 사용하게 한다. scope는 인증 경계가 아니다.

짧은 DB 트랜잭션은 scope별 advisory lock으로 직렬화한다. 초기 테이블 생성도 공통 잠금으로
직렬화한다. 연결은 트랜잭션마다 만들고 닫으며 외부 효과나 복구 조회 중에는 유지하지 않는다.
READ COMMITTED와 synchronous_commit=on을 명시한다. 같은 scope의 DB 처리량은 이 잠금의
영향을 받는다. 연결 풀, 자동 장애 조치, 분산 그래프 스케줄러는 제공하지 않는다.
[PostgreSQL 잠금](https://www.postgresql.org/docs/current/explicit-locking.html)과
[psycopg 트랜잭션](https://www.psycopg.org/psycopg3/docs/basic/transactions.html) 계약을 사용한다.

## 단일 효과 연결

```python
# provider.send는 앱에서 선택한 제공자 SDK 호출이다.
def send_to_provider(operation):
    return provider.send(
        **operation.request,
        idempotency_key=operation.provider_key,
    )

send = executor.bind("message.send:v1", send_to_provider)
record = send("order-123:confirmation", {"text": "Order confirmed"})
```

제공자가 실제로 키를 지원하는 경우에만 해당 인자를 전달한다. 키 지원 여부와 보존 기간,
SDK 내부 재시도는 제공자 어댑터가 알아야 한다. `bind`는 얇은 진입점이며 한 번에 여러 외부
효과를 실행하는 함수를 안전한 단일 효과로 바꾸지는 않는다.

`record.state == "completed"`일 때 `record.result`로 후속 업무를 진행한다. 미해결이면
`record.response()`의 상태를 노출하고 보류한다. 저장소 오류나 OperationConflict는 효과
이후에도 발생할 수 있으므로 같은 업무 ID를 유지한다.

## 호스트 업무 ID와 LangGraph

core와 MCP는 operation_id를 직접 받는다. LangGraph의 `durable_tool`에도 콜백으로 제공할 수 있다.

```python
from langgraph_effect_ledger.langgraph import durable_tool

send_tool = durable_tool(
    name="send_confirmation", description="Send the order confirmation",
    effect="message.send:v1", execute=transport.execute,
    operation_id=lambda runtime: runtime.config["configurable"]["confirmation_id"],
)
config = {"configurable": {
    "thread_id": "workflow-123",
    "confirmation_id": "order-123:confirmation",
}}
```

예시는 워크플로에 확인 메시지 하나라는 업무 제약이 있을 때 사용한다. 서로 다른 의도에는
서로 다른 ID가 필요하다. 툴 여러 개나 여러 번의 같은 툴 호출을 한 업무 ID에 뭉치면 의도한
효과가 억제되거나 인자 충돌이 난다. 호스트의 내구 업무 레코드에서 ID를 읽고 모든 재개에
같은 값을 전달한다. 콜백에서 매번 UUID를 만들거나 모델 출력에 ID 소유권을 넘기지 않는다.

콜백은 모델 스키마에 노출되지 않는다. 콜백 실패나 빈 ID는 `identity_error` interrupt로
보류하고, 임의의 파생 ID로 대체하지 않는다. 올바른 호스트 매핑을 복원한 뒤 resume한다.

콜백을 생략하면 기존 workflow_id·thread_id·부모 메시지 ID·tool_call_id 파생 방식을 쓴다.
호스트 ID를 사용하면 업무 식별을 체크포인터의 메시지 ID와 분리할 수 있다. 원본 인자와
워크플로 진행을 복원하기 위한 내구 체크포인터는 여전히 필요하다.

## 복구 정책

`RecoveryPolicy`는 `Operation -> RecoveryDecision | None`인 신뢰된 동기 콜백이다.
실행 중 자동 호출하지 않는다. 감독자가 이전 워커를 중단하고 이미 전송된 요청도 정리한 뒤
`executor.recover(id, workers_stopped=True)`를 호출한다. 스케줄러가 이 조건을 확인할 수 있다면
사람 대신 호출할 수 있다. 라이브러리가 워커 종료 여부를 증명해 주지는 않는다.

```python
from langgraph_effect_ledger import EffectExecutor, RecoveryDecision

def reconcile(operation):
    # 앱의 제공자 어댑터: 원본 요청과 정확히 대응하는 완료 기록만 반환한다.
    receipt = provider.find_confirmed_receipt(operation.provider_key, operation.request)
    if receipt is None:
        return None  # 검색 결과가 없다는 사실만으로 재시도를 허용하지 않는다.
    return RecoveryDecision(
        action="complete", decision_id=receipt.durable_decision_id,
        reason="Provider confirmed this exact operation", result=receipt.result,
    )

executor = EffectExecutor(store=store, scope="account-1", recovery=reconcile)
record = executor.recover("order-123:confirmation", workers_stopped=True)
```

기본값과 `None`은 보류다. 정책 예외도 권한을 변경하지 않고 전파한다. `complete`에는 명시적인
결과가 필요하며 `None`도 유효한 결과다. `retry`는 결과 없이 다음 시도 하나만 허용한다.
이전 요청이 앞으로 적용될 가능성까지 배제하는 제공자별 증거가 필요하다. 24시간 경과,
타임아웃, 단순한 조회 실패는 증거가 아니다. 이 배포에는 제공자별 자동 복구 구현은 없다.

결정은 조회한 버전에 묶인다. 조회 중 다른 감독자가 판정했다면 stale conflict가 나며
덮어쓰지 않는다. 같은 결정을 재전달할 때는 ID와 원래 expected_version 등 전체 인자를
보존해 `resolve`를 호출한다. `recover`는 재조회이므로 이후 다른 시도의 새 결정을 만드는
경우에는 별도 증거와 별도 결정 ID가 필요하다. 과거 ID 재사용은 재시도 권한을 늘리지 않는다.

## 사용자 저장소의 계약

`OperationStore`는 상속이 필수인 기본 클래스가 아닌 Protocol이다. 기존 `EffectStore`
get/put 프로토콜은 레거시 실험용이며 이 실행 계약을 충족하지 않는다.

| 메서드 | 필요한 원자성/내구성 |
|---|---|
| `get(scope, id)` | 커밋된 Operation 또는 None 반환 |
| `claim(scope, id, effect, request)` | 키·원본 인자를 효과 전에 저장; Claim.acquired는 경쟁자 중 하나만 True; 완료/미해결 재호출은 False |
| `finish(owned, state, result, error)` | in_flight와 owned.version 일치 때만 완료/결과 불명 기록; 실패 시 OperationConflict |
| `resolve(scope, id, ...)` | 버전 확인·결정 ID 중복 검사·상태 전이를 한 트랜잭션에 기록 |

finish의 result는 코어가 검증·직렬화한 JSON 문자열 또는 None이다. 공개 Operation.result는
디코딩된 JSON 값이다. 같은 ID의 바인딩 비교는 JSON 키 순서만 정규화하며 값과 효과 버전을
바꿀 수 없다. 공유 저장소는 scope를 격리한다. 완료/미해결 레코드를 자동 만료하지 않는다.

`tests/test_operations.py`의 OperationsContract와 `tests/test_operation_crash.py`의 CrashContract를
SQLite와 실제 PostgreSQL에 모두 적용한다. 독립 프로세스의 실행권 경합, 효과 반영 뒤 SIGKILL,
완료 확인 재생, 결정 ID 경합, 늦은 완료와 버전 충돌을 검사한다. 로컬 Docker PostgreSQL에서
검증했으며 실제 다중 머신 네트워크 분할·DB failover까지 검증한 것은 아니다.
