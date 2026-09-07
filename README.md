# langgraph-effect-ledger

LangChain 에이전트의 쓰기 툴에 붙이는 **조합 가능한 실행 원장 라이브러리**다.
저장소(SQLite/Postgres), 업무 ID, 복구 정책, LangGraph/MCP 어댑터를 따로 선택한다.
코어는 프레임워크에 의존하지 않으며 **요청·제공자 키·실행 시도·결과·복구 판정**을 보존한다.

외부 시스템이 요청을 적용한 직후 프로세스가 죽으면 성공 여부를 로컬에서 알 수 없다.
`EffectExecutor`는 같은 작업을 자동 재전송하지 않고 미해결 상태로 남긴다.
제공자 멱등성을 만들어내거나 exactly-once를 보장하지 않는다.

LangGraph 에이전트의 **장애 → 미해결 보류 → 운영자 판정 → 재개 → 최종 응답**도 연결했다.
[전체 실행 예제와 복구 절차](docs/langgraph-recovery.md)에서 시작할 수 있다.
[조합 API와 확장 계약](docs/composition.md)은 내장 실행부터 다중 호스트 저장소까지 설명한다.

```python
from langchain.agents import create_agent
from langgraph_effect_ledger import EffectExecutor
from langgraph_effect_ledger.langchain import ExecutionBoundary
from langgraph_effect_ledger.langgraph import DurableAgentRunner

boundary = ExecutionBoundary(
    EffectExecutor("effects.sqlite", scope="account-1"),
    workflow_id="mail-agent:v1",
)
# model, send_message, saver는 앱의 기존 모델·단일 효과 툴·내구 체크포인터다.
agent = create_agent(model, [send_message], middleware=[boundary], checkpointer=saver)
runner = DurableAgentRunner(agent)
```

등록된 모든 툴은 기본적으로 보호된다. 기존 툴의 이름·인자 스키마를 유지한다. 읽기·제어
예외와 안정적인 효과 이름은 [LangChain 실행 경계 가이드](docs/langchain-boundary.md)의
`tools` 설정으로 지정한다. 여러 툴 미들웨어를 쓴다면 경계를 마지막에 배치한다.
[LangChain 실행 경계 가이드](docs/langchain-boundary.md)에 적용 코드·HITL·복구·조합 제약을 정리했다.

## 설치

```bash
uv sync --extra langchain
# MCP 연결이 필요할 때:
uv sync --extra mcp
# 다중 호스트 저장소를 사용할 때:
uv sync --extra postgres
```

코어는 Python 3.10 이상과 표준 라이브러리만 사용한다. MCP extra는 SDK v1
(`mcp>=1.28,<2`)용이다. LangChain 실행 경계와 LangGraph 어댑터는 `[langchain]` extra를 사용한다.

## 실행 계약

호스트는 **논리 작업 ID를 호출 전에 내구적으로 저장**하고 재시도 시 재사용한다.
MCP 요청 ID나 모델이 매번 생성하는 툴 호출 ID로 대체하지 않는다.
서버는 계정/테넌트 scope와 효과 이름·버전을 고정한다.

```text
호스트: 작업 ID 저장
  → 서버: scope + 작업 ID에 효과·원본 JSON·제공자 키 결합
  → 저장소: 실행권 획득과 시작 기록 커밋
  → 핸들러: 단일 외부 효과 실행
  → 저장소: 결과 기록
  → 호스트: 결과 수신 또는 미해결 작업 보류
```

같은 scope와 ID에 다른 효과/요청을 보내면 충돌이다. JSON 객체 키 순서는 무관하지만
값을 바꾸면 새 요청이다. 정수와 실수 표현도 구분한다. JSON 값만 허용한다.
어댑터는 실행 전에 저장된 `call.provider_key`를 제공자가 지원할 때 사용한다.
키를 보존해도 제공자의 멱등성 보존 기간이 연장되지는 않는다.

| 상태 | 의미 | 같은 ID로 execute |
|---|---|---|
| `in_flight` | 실행 중이거나 작업자가 죽었을 수 있음 | 현 상태 반환 |
| `indeterminate` | 핸들러 예외 또는 결과 직렬화 실패 | 현 상태 반환 |
| `ready` | 신뢰된 복구 결정이 다음 시도 한 번을 허용함 | 원자적으로 권한 소비 후 실행 |
| `completed` | 실행 또는 외부 확인으로 결과 확정 | 저장 결과 반환 |

앞의 두 상태는 `unresolved=true`다. 시간 경과·취소·재시작으로 자동 해제하지 않는다.
응답의 `next_action`은 `in_flight`이면 `wait`, `indeterminate`이면 `reconcile`이다.
경쟁에서 진 호출자는 먼저 `get_effect`로 완료를 기다리며 즉시 운영자 판정을 요구하지 않는다.
리스/heartbeat가 없어 `in_flight`의 생존 여부는 여전히 알 수 없다. 작업자가 중단됐는지는
호스트가 조사해야 하며, 기다린 시간이 길다는 이유로 새 실행권을 주지 않는다.
`ready`는 `execute`, `completed`는 `use_result`를 반환한다.
저장소 오류로 응답을 못 받았을 때도 동일 작업 ID를 유지한다. 핸들러는 동기 함수이며,
내부 SDK 재시도와 복수 효과의 부분 성공은 핸들러/제공자 어댑터의 책임이다.

## MCP 서버 예제

[examples/mcp_server.py](examples/mcp_server.py)는 별도 SQLite 파일에 메시지를 추가하는
**로컬 비멱등 메일함**이다. 실제 메일이나 외부 계정에 접근하지 않는다.

```bash
uv run --extra mcp python examples/mcp_server.py --ledger /tmp/effects.sqlite --mailbox /tmp/mailbox.sqlite
```

stdio MCP 클라이언트에서 다음 두 툴을 호출한다.

```json
{"name":"execute_effect","arguments":{"operation_id":"message-1","effect":"message.send:v1","request":{"text":"hello"}}}
```

```json
{"name":"get_effect","arguments":{"operation_id":"message-1"}}
```

등록 효과는 `create_server(executor, effects)`의 서버 측 registry로 제한한다.
scope와 provider key는 툴 인자로 받지 않는다. 제공자별 입력 검증은 핸들러가 담당한다.

응답은 `structuredContent`와 JSON text에 동일한 상태를 담는다. 미해결도 유효한 상태 응답이며
`isError=false`일 수 있다. **호스트는 `unresolved`를 검사하고 후속 업무를 보류해야 한다.**
모델에게 오류 문장만 보여주는 것으로 fail-closed가 완성되지는 않는다. 새 ID로 같은 업무를
다시 요청하는 의미적 중복은 서버가 알아낼 수 없다.

`--lose-response`를 추가하면 메일함 저장 후 응답 유실을 흉내 낸다. 재호출해도 메시지는
추가되지 않고 `indeterminate`가 반환된다. 실제 강제 종료 검증은 테스트에 있다.

## 운영자 복구

복구 API는 MCP 툴로 노출하지 않는다. 신뢰할 수 있는 운영 경로에서 호출한다.
**기존 작업자를 중지하고 이미 전송된 제공자 요청의 상태까지 확인한 뒤** 판정한다.
`workers_stopped=True`는 호출자의 확인이며 원격 효과를 차단하는 장치가 아니다.

```python
from langgraph_effect_ledger import EffectExecutor

executor = EffectExecutor("/tmp/effects.sqlite", scope="local-mailbox")
record = executor.get("message-1")
if record is None:
    raise LookupError("Unknown operation")

# 메일함의 message_id=1을 실제 확인했고 이전 서버가 종료된 경우에만 실행.
executor.resolve(
    "message-1", expected_version=record.version,
    decision_id="operator-confirmed-message-1",
    action="complete", result={"message_id": 1},
    reason="Mailbox confirms message 1; previous server stopped",
    workers_stopped=True,
)
```

`action="retry"`는 result 없이 다음 실행 한 번을 허용한다. 호스트가 동일 ID·효과·원본
요청으로 execute를 호출해야 실행된다. `complete`에는 확인된 결과가 필수이며 명시적
`None`도 허용한다. 판정 ID와 인자도 호출 전에 보존해야 한다.

복구는 버전을 확인하고 판정과 전이를 한 트랜잭션에 저장한다. 동일 판정 재전달은 현재
상태만 반환한다. 같은 판정 ID에 다른 내용, 오래된 버전에 새 판정은 거절한다.
판정 내용·사유·시각은 `decisions` 테이블에 남는다. 늦은 결과는 변경된 버전을 덮어쓰지
못하지만 이미 전송된 외부 요청을 취소하지는 못한다.

`execute()`의 `OperationConflict`도 효과 실패를 뜻하지 않는다. 요청 바인딩이 다르면 실행 전에
발생하지만, 실행권 버전이 바뀌면 **외부 효과가 성공한 뒤 결과 저장 시점에도** 발생할 수 있다.
동일 작업을 조회하고 판정해야 하며, 예외만 보고 새 ID로 재시도하지 않는다.

## 저장소와 배포 범위

SQLite `BEGIN IMMEDIATE`로 실행권을 원자적으로 획득하고 `synchronous=FULL`로 효과보다
먼저 커밋한다. 네트워크 호출 중에는 DB 잠금을 유지하지 않는다. 동일 호스트의 프로세스들이
같은 로컬 디스크 DB를 공유하는 범위다. 네트워크 파일시스템·다중 호스트용 구현은 아니다.
DB 손실·오래된 백업 복원·원장 삭제는 보장을 깨뜨린다. 자동 만료/삭제는 구현하지 않았다.

다중 호스트는 `[postgres]`의 `PostgresOperationStore(dsn)`를 주입한다. 같은 DB와 scope를
사용하는 호스트들이 실행권을 공유한다. scope별 짧은 트랜잭션을 직렬화하며, 외부 호출 동안
잠금을 잡지 않는다. 연결 풀·스키마 마이그레이션·DB 장애 조치는 포함하지 않는다.
원장의 분산 실행권과 LangGraph thread 스케줄링은 별개다. 동일 thread 직렬화는 호스트 책임이다.

scope는 인증을 대신하지 않는다. 예제는 신뢰할 수 있는 단일 호스트의 stdio용이다.
HTTP 배포는 인증·권한·계정별 라우팅을 별도로 구성해야 한다.

## 검증

```bash
uv run --all-extras python -m unittest discover -s tests -p 'test_operation*.py' -v
uv run --all-extras python -m unittest discover -s tests -p test_mcp_server.py -v
uv run --all-extras python -m unittest discover -s tests -p 'test_langgraph*.py' -v
uv run --all-extras python -m unittest discover -s tests -p test_durable_agent_example.py -v
uv run --extra langchain python tests/test_ledger.py
uv run --extra langchain python tests/test_detector.py
# PostgreSQL 전용 테스트 DB를 준비한 뒤 전체 계약/강제 종료 테스트 실행:
EFFECT_LEDGER_TEST_DSN=postgresql://postgres@localhost/effect_ledger_test \
  uv run --all-extras python -m unittest discover -s tests -v
```

- 별도 HTTP 제공자가 자체 DB에 효과를 커밋한 직후 작업 프로세스를 강제 종료한다.
- 새 프로세스의 재호출과 운영자 완료 확인 뒤에도 제공자 효과는 한 건으로 유지된다.
- 독립 프로세스 4개의 동시 호출은 실행권을 하나만 획득한다.
- MCP stdio 연결을 실제 재시작하며 결과 재생·충돌·미해결·복구를 검증한다.

기존 미들웨어·탐지기는 [이전 실험 문서](docs/legacy-middleware.md)와 `probes/`에 보존했다.
새로운 서버 실행 계약의 근거로 사용하지 않는다. 기존 사용자는 이제 `[langchain]` extra가
필요하다. 설계와 구현 계획은 `docs/superpowers/`에 있다.
