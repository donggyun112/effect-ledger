# effect-ledger

[![CI](https://github.com/donggyun112/langgraph-effect-ledger/actions/workflows/ci.yml/badge.svg)](https://github.com/donggyun112/langgraph-effect-ledger/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

*[English README](README.md)*

에이전트가 카드를 긁었다. 제공자의 응답이 돌아오기 전에 프로세스가 죽었다. 그래프는
마지막 체크포인트에서 재개하며 툴을 다시 호출한다. 카드를 한 번 더 긁는다.

버그가 아니라 문서화된 동작이다. 시작했지만 끝내지 못한 태스크는 재개할 때 다시 실행된다.
효과를 안전하게 만드는 일은 사용자 몫으로 남는다.

**effect-ledger는 효과가 나가기 전에 시도 기록을 커밋한다. 그래서 두 번째 호출이 그 기록을
발견한다.** 결과를 남기지 못한 시도는 `unresolved`로 멈춰 사람을 기다린다. 추측으로 재시도하는
경로는 없다.

제공자 멱등성을 만들어내지 않고 exactly-once도 보장하지 않는다. 이미 나갔을지 모르는 것을
기록하고 나머지는 추측하지 않는다.

[LangChain 실행 경계](docs/langchain-boundary.md)에서 시작한다. 이미 가진 툴 위에 미들웨어
하나를 얹는 것이다. 나머지는 따로 고른다 — 저장소(SQLite/Postgres), 업무 ID, 복구 정책. 코어는
프레임워크에 의존하지 않는다. [조합 API와 확장 계약](docs/composition.md)이 다중 호스트 저장소까지
설명한다.

```python
from langchain.agents import create_agent
from effect_ledger import EffectExecutor
from effect_ledger.langchain import ExecutionBoundary
from effect_ledger.langgraph import LedgerRunner

boundary = ExecutionBoundary(
    EffectExecutor("effects.sqlite", scope="account-1"),
    workflow_id="mail-agent:v1",
)
# model, send_message, saver는 앱의 기존 모델·단일 효과 툴·내구 체크포인터다.
agent = create_agent(model, [send_message], middleware=[boundary], checkpointer=saver)
runner = LedgerRunner(agent)
```

등록된 모든 툴은 기본적으로 보호된다. 기존 툴의 이름·인자 스키마를 유지한다. 읽기·제어
예외와 안정적인 효과 이름은 [LangChain 실행 경계 가이드](docs/langchain-boundary.md)의
`tools` 설정으로 지정한다. 여러 툴 미들웨어를 쓴다면 경계를 마지막에 배치한다.

## 설치

```bash
pip install "effect-ledger[langchain]"
pip install "effect-ledger[mcp]"       # MCP로 효과를 노출할 때
pip install "effect-ledger[postgres]"  # 다중 호스트 저장소를 쓸 때
```

이 저장소를 클론해서 개발할 때는 `uv sync --extra langchain`(또는 `--all-extras`)을 쓴다.

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
`ready`는 `execute`, `completed`는 `use_result`를 반환한다.
경쟁에서 진 호출자는 먼저 `get_effect`로 완료를 기다리며 즉시 운영자 판정을 요구하지 않는다.
저장소 오류로 응답을 못 받았을 때도 동일 작업 ID를 유지한다. 핸들러는 동기 함수이며
내부 SDK 재시도와 복수 효과의 부분 성공은 핸들러/제공자 어댑터의 책임이다.

### 리스가 없다. 그게 설계다

`in_flight`는 작업자가 살아 있다는 뜻이 아니다. 리스도 heartbeat도 없으므로 그 상태에 있는
작업은 지금도 실행 중일 수도 있고 일주일 전에 죽었을 수도 있다. 원장은 둘을 구분하지 못하고
바깥에서도 구분할 수 없다.

**그래서 여기서는 아무것도 만료되지 않는다.** 시간이 아무리 지나도 `in_flight`에서 저절로
빠져나오지 않는다. 타이머로 만료되는 실행권은 제공자를 본 적 없는 시계가 발급하는 재시도
허가이기 때문이다. 작업자를 멈추고 제공자를 확인한 사람만 `resolve`로 판정할 수 있다.

나중에 리스를 열더라도 그것은 조사 신호이지 실행권이 아니다. 만료는 어디를 봐야 하는지
알려줄 뿐이고 다음 시도를 허용하는 경로는 여전히 `resolve` 하나다.

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
from effect_ledger import EffectExecutor

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
발생하지만 실행권 버전이 바뀌면 **외부 효과가 성공한 뒤 결과 저장 시점에도** 발생할 수 있다.
동일 작업을 조회하고 판정해야 하며 예외만 보고 새 ID로 재시도하지 않는다.

## 저장소와 배포 범위

SQLite `BEGIN IMMEDIATE`로 실행권을 원자적으로 획득하고 `synchronous=FULL`로 효과보다
먼저 커밋한다. 네트워크 호출 중에는 DB 잠금을 유지하지 않는다. 동일 호스트의 프로세스들이
같은 로컬 디스크 DB를 공유하는 범위다. 네트워크 파일시스템·다중 호스트용 구현은 아니다.
DB 손실·오래된 백업 복원·원장 삭제는 보장을 깨뜨린다. 자동 만료/삭제는 구현하지 않았다.

다중 호스트는 `[postgres]`의 `PostgresOperationStore(dsn)`를 주입한다. 같은 DB와 scope를
사용하는 호스트들이 실행권을 공유한다. scope별 짧은 트랜잭션을 직렬화하며 외부 호출 동안
잠금을 잡지 않는다. 연결 풀·스키마 마이그레이션·DB 장애 조치는 포함하지 않는다.
원장의 분산 실행권과 LangGraph thread 스케줄링은 별개다. 동일 thread 직렬화는 호스트 책임이다.

scope는 인증을 대신하지 않는다. 예제는 신뢰할 수 있는 단일 호스트의 stdio용이다.
HTTP 배포는 인증·권한·계정별 라우팅을 별도로 구성해야 한다.

## 검증

```bash
uv run --all-extras python -m unittest discover -s tests -p 'test_operation*.py' -v
uv run --all-extras python -m unittest discover -s tests -p test_mcp_server.py -v
uv run --all-extras python -m unittest discover -s tests -p 'test_langgraph*.py' -v
uv run --all-extras python -m unittest discover -s tests -p test_recovery_agent_example.py -v
# PostgreSQL 전용 테스트 DB를 준비한 뒤 전체 계약/강제 종료 테스트 실행:
EFFECT_LEDGER_TEST_DSN=postgresql://postgres@localhost/effect_ledger_test \
  uv run --all-extras python -m unittest discover -s tests -v
```

- 별도 HTTP 제공자가 자체 DB에 효과를 커밋한 직후 작업 프로세스를 강제 종료한다.
- 새 프로세스의 재호출과 운영자 완료 확인 뒤에도 제공자 효과는 한 건으로 유지된다.
- 독립 프로세스 4개의 동시 호출은 실행권을 하나만 획득한다.
- MCP stdio 연결을 실제 재시작하며 결과 재생·충돌·미해결·복구를 검증한다.

CI는 Python 3.10–3.13에서 실제 PostgreSQL 서비스를 띄워 이 스위트를 돌리며 테스트가
하나라도 skip으로 보고되면 빌드를 실패시킨다.

설계 근거는 `probes/`에 순서대로 남아 있다. 각 파일은 패키지를 import하지 않고 그대로
실행된다. 설계와 구현 계획은 `docs/superpowers/`에 있다.

초기의 `EffectLedger` 미들웨어와 `MixedEffectDetector`는 제거했다. 미들웨어는 툴 바깥이라
툴 본문의 재생을 끊지 못했고 탐지기는 그 한계를 정적 분석으로 경고하는 우회책이었다
(`probes/probe_i~l`). `ExecutionBoundary`는 효과 전에 실행권을 커밋하므로 툴이 내부에서
중단해도 실행권이 풀리지 않는다 — 경고할 위험 자체가 사라져 탐지기도 함께 사라졌다.
필요하면 git 히스토리에서 꺼낼 수 있다.
