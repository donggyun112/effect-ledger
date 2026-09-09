# effect-ledger

[![CI](https://github.com/donggyun112/effect-ledger/actions/workflows/ci.yml/badge.svg)](https://github.com/donggyun112/effect-ledger/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://github.com/donggyun112/effect-ledger/blob/main/LICENSE)

*[English README](https://github.com/donggyun112/effect-ledger/blob/main/README.md)*

## 먼저 실행하기

앱 구성에 맞는 어댑터를 설치한다.

```bash
pip install "effect-ledger[langchain]"           # LangChain 또는 LangGraph
pip install "effect-ledger[langchain,postgres]"  # 공유 PostgreSQL 원장
pip install "effect-ledger[mcp]"                 # MCP로 원격 효과 실행
```

저장소를 클론해서 개발할 때는 `uv sync --extra langchain` 또는 `uv sync --all-extras`를
실행한다. 코어 패키지는 Python 3.10 이상에서 동작하며 런타임 의존성이 없다.

| 앱 구성 | 추가할 실행 경계 |
|---|---|
| `create_agent(...)` | `ExecutionBoundary` 미들웨어 |
| `messages` 상태를 쓰는 루트 `StateGraph` | `ToolNode` 대신 `LedgerToolNode` |
| 다른 프로세스가 처리하는 효과 | `durable_tool(...)` |

기존 LangChain 에이전트에는 미들웨어 하나를 추가한다.

```python
from langchain.agents import create_agent
from effect_ledger import EffectExecutor
from effect_ledger.langchain import ExecutionBoundary
from effect_ledger.langgraph import LedgerRunner

boundary = ExecutionBoundary(
    EffectExecutor("effects.sqlite", scope="account-1"),
    workflow_id="mail-agent",
)
agent = create_agent(
    model, [send_message], middleware=[boundary], checkpointer=saver,
)
runner = LedgerRunner(agent)
```

등록된 도구는 모두 원장을 거친다. 읽기 전용 도구, 제어 도구와 유지할 효과 이름은 `tools`
설정에 적는다. 도구 미들웨어가 여러 개라면 `ExecutionBoundary`를 마지막에 배치해 도구와
가장 가까운 경계로 만든다.

직접 만든 `StateGraph`에는 [LedgerToolNode](#직접-만든-langgraph에-연결)를 사용한다.

## 필요한 이유

에이전트가 카드를 결제한 직후, 제공자의 응답이 오기 전에 프로세스가 죽을 수 있다.
LangGraph가 체크포인트에서 재개하면 같은 도구를 다시 호출할 수 있다.

effect-ledger는 도구 호출 전에 작업 기록을 쓴다. 제공자 결과를 받지 못하면 작업은
`unresolved`로 남는다. 다음 실행은 기존 기록을 발견하고 추가 결제 전에 멈춘다. 호스트는
제공자를 확인한 뒤 `complete` 또는 `retry` 판정을 기록한다.

제공자 멱등성과 exactly-once 전달은 제공자가 책임진다. 원장은 이미 실행됐을 수 있는 작업을
기록하고, 확인되지 않은 재시도를 차단한다.

![model, ledger, unresolved 세 노드의 그래프. ledger는 실행권 커밋, send_confirmation,
결과 기록 세 단계를 담은 상자로 그려진다. 아직 아무것도 보내지 않은 상태에서 실행권이 커밋되고,
확인 메시지가 나간 뒤 응답을 잃어 indeterminate로 unresolved에서 멈춘다. 재개하면 발송 단계가
흐려진 채 실행되지 않고 attempt도 그대로다. 호스트가 확인한 결과를 기록하자 원장이 재생한다. 보낸
메시지와 제공자 시도 두 카운터가 계속
1이다](https://raw.githubusercontent.com/donggyun112/effect-ledger/main/docs/recovery-walk.gif)

`EffectExecutor.execute()`는 실행권을 커밋하고 도구를 호출한 뒤 결과를 기록한다. 따라서
아무것도 보내지 않은 시점에 실행권이 먼저 존재한다. 응답을 잃으면 두 번째 확인 메시지를
보내기 전에 `unresolved`에서 멈춘다.

판정 없이 재개하면 기존 미해결 작업을 발견하고 발송 전에 멈춘다. attempt는 그대로다.
호스트가 제공자 결과를 확인하면 원장이 그 결과를 저장하고 재생한다. 두 카운터는 끝까지 1이다.

화면의 모든 값은
[examples/execution_boundary_agent.py](https://github.com/donggyun112/effect-ledger/blob/main/examples/execution_boundary_agent.py)의
합성 그래프를 실제로 실행해 캡처한 것이다. `in_flight` 행은 시도가 진행되는 동안 저장소를
샘플링해서 얻었다. 소스:
[docs/demo/recovery-walk.html](https://github.com/donggyun112/effect-ledger/blob/main/docs/demo/recovery-walk.html).

데모 그래프는 `EffectExecutor`를 직접 조합하므로 실행 경계가 노드로 보인다.
`ExecutionBoundary`는 에이전트의 기존 `tools` 노드 안에서 실행된다. `langgraph.json`은
`langgraph dev`에서 두 구성을 모두 보여준다.

[LangChain 실행 경계 가이드](https://github.com/donggyun112/effect-ledger/blob/main/docs/langchain-boundary.ko.md)는
도구 정책과 미들웨어 순서를 설명한다. [조합 가이드](https://github.com/donggyun112/effect-ledger/blob/main/docs/composition.ko.md)는
저장소, 업무 ID, 복구 정책과 다중 호스트 배포를 다룬다.

## 직접 만든 LangGraph에 연결

기존 `ToolNode`를 `LedgerToolNode`로 교체한다. `@tool` 함수와 모델 루프를 유지하며,
한 모델 응답에서 같은 도구를 여러 번 호출해도 개별 실행마다 기록한다.

```python
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import tools_condition
from effect_ledger import EffectExecutor, RecoveryDecision
from effect_ledger.langchain import READ_ONLY
from effect_ledger.langgraph import LedgerRunner, LedgerToolNode

# model, 도구 함수들, saver, provider_for는 앱에서 제공한다.
tools = [send_mail, send_slack, create_ticket, search]
model_with_tools = model.bind_tools(tools)

def reconcile(operation):
    provider = provider_for(operation.effect)
    receipt = provider.find_confirmed_receipt(
        operation.provider_key, operation.request,
    )
    if receipt is None:
        return None
    return RecoveryDecision(
        action="complete",
        decision_id=f"{operation.effect}:{receipt.id}",
        reason="Provider confirmed this operation",
        result=LedgerToolNode.result(
            "Completed", artifact={"receipt_id": receipt.id},
        ),
    )

executor = EffectExecutor(
    "effects.sqlite", scope="account-1", recovery=reconcile,
)

builder = StateGraph(MessagesState)
builder.add_node("model", lambda state: {
    "messages": [model_with_tools.invoke(state["messages"])]
})
builder.add_node("tools", LedgerToolNode(
    tools,
    executor=executor,
    workflow_id="order-notifications",
    policies={
        "send_mail": "mail.send:v1",
        "send_slack": "slack.send:v1",
        "create_ticket": "ticket.create:v1",
        "search": READ_ONLY,
    },
))
builder.add_edge(START, "model")
builder.add_conditional_edges("model", tools_condition)
builder.add_edge("tools", "model")

runner = LedgerRunner(builder.compile(checkpointer=saver))
config = {"configurable": {"thread_id": "order-123"}}
outcome = runner.start(
    {"messages": [("user", "확인 메일을 보내고 슬랙에도 알려줘.")]},
    config,
)
```

노드는 미들웨어와 같은 실행 경계를 사용한다. 설정에 없는 도구도
`langchain.tool:<도구 이름>` 효과로 보호한다. `policies`에서
`{"send_mail": "mail.send:v1"}`처럼 유지할 효과 이름을 정할 수 있다.
`READ_ONLY` 도구는 원장을 생략하므로 재개 시 다시 실행될 수 있다.
같은 도구에 미들웨어나 `durable_tool`을 이중 적용하지 않는다.

메일은 완료되고 슬랙 응답만 유실되면 그래프가 보류된다. 재개 시 메일의 저장된
`ToolMessage`를 재생하고 원장에서 슬랙 작업이 여전히 미해결임을 확인한다. 두 효과의 attempt는
그대로 유지된다. 기본 ToolNode는 여러 호출을 동시에 실행할 수 있으므로 보류됐다고 모든
워커와 외부 요청이 종료된 것은 아니다. 여러 미해결 호출은 재개 과정에서 차례로 드러날 수 있다.
`executor.unresolved()`로 해당 scope의 미해결 기록을 조회한다.

### 복구 판정의 책임

원장은 상태 변경, 버전 확인, 중복 판정 방지와 저장된 결과 재생을 담당한다. 메일 제공자가
요청을 받았는지, 결제가 확정됐는지, 이전 워커가 계속 실행될 수 있는지는 알 수 없다.
제공자의 영수증을 조회하고 성공 여부를 해석하는 일은 애플리케이션의 비즈니스 로직이다.

자동 복구 워커, webhook, 운영 서비스 또는 관리자 화면에서 이 비즈니스 대조 작업을 수행할
수 있다. 판단할 수 없으면 미해결 상태를 유지한다. 충분한 증거가 있을 때만 신뢰된
`complete` 또는 `retry` 판정을 제출한다. API는 사람 운영자를 요구하지 않는다.

호스트가 이전 워커를 중단하면 복구 워커가 설정된 정책으로 작업을 대조한다. `recover()`는
현재 레코드와 버전을 읽어 반환된 판정을 적용한다. 정책이 판단을 보류하면 기존 상태를 유지한다.

```python
pending = outcome["__interrupt__"][0].value
record = executor.recover(
    pending["operation_id"],
    workers_stopped=True,
)
if record.state == "completed":
    outcome = runner.resume(config)
```

복구 정책은 확인된 결과를 `LedgerToolNode.result(...)`로 감싸 ToolMessage 포맷을 저장한다.
`resume()`은 thread를 깨우는 역할만 한다. 정책이 이전 시도가 앞으로 적용될 수 없음을 확인한
뒤 `action="retry"`를 반환하면 다음 시도 한 번을 허용한다.

### 작업 ID 구성

호스트가 작업 ID를 직접 제공하지 않을 때 `workflow_id`는 서로 다른 그래프의 도구 호출을
구분하는 namespace가 된다. 기본 작업 ID는 다음 값을 조합해 만든다.

```text
workflow_id + thread_id + 체크포인트의 부모 AIMessage ID + tool-call ID
```

`workflow_id`는 재시작과 일반 배포에서도 유지해야 한다. 값을 바꾸면 새로운 작업 ID 공간이
생기므로 완료된 외부 효과도 다시 실행될 수 있다. 도구의 외부 의미가 바뀌었다면 `policies`의
효과 이름을 버전 관리한다. 일반 배포 때 workflow ID를 바꾸는 방식으로 마이그레이션하지 않는다.

애플리케이션이 개별 업무의 내구 ID를 이미 관리한다면 `operation_id` 콜백을 제공하고
`workflow_id`를 생략할 수 있다.

```python
tools_node = LedgerToolNode(
    tools,
    executor=executor,
    operation_id=lambda runtime: runtime.state["operation_ids"][runtime.tool_call_id],
)
```

콜백은 서로 다른 업무에는 다른 ID를, 같은 업무의 재생에는 같은 ID를 반환해야 한다. 모든
도구 호출에 하나의 ID를 공유하면 안 된다.

### 지원하는 그래프 형태

지원 범위는 `MessagesState`(또는 `add_messages` reducer를 쓰는
`messages` 필드), 내구 체크포인터, `LedgerRunner`를 통해 같은 thread 호출을 직렬화하는
루트 그래프다. 보호된 도구 하나는 외부 효과 하나를 수행하고 JSON으로 표현 가능한 결과·
artifact를 반환해야 한다. 서브그래프·handoff·time travel·보호된 도구 내부의 승인 interrupt는
이 계약 밖이다. 비동기 도구는 비동기 체크포인터와 `astart`/`aresume`으로 실행한다.

### 여러 도구의 복구 예제 실행

[examples/ledger_tool_node.py](examples/ledger_tool_node.py)는 모델 API 키 없이 실행된다.
메일·슬랙 발송을 로컬 SQLite 테이블로 시뮬레이션하고 슬랙 응답만 유실시킨다.
새 상태 디렉터리로 다음 명령을 하나씩 실행한다.

```bash
uv sync --extra langchain
uv run python examples/ledger_tool_node.py --state-dir /tmp/node-demo start --lose-response
uv run python examples/ledger_tool_node.py --state-dir /tmp/node-demo resume
uv run python examples/ledger_tool_node.py --state-dir /tmp/node-demo confirm --workers-stopped
uv run python examples/ledger_tool_node.py --state-dir /tmp/node-demo resume
```

처음 두 명령은 `paused`, 마지막 명령은 `completed`를 반환한다. 두 발송 횟수는 끝까지
각각 1회다. `confirm`은 로컬 영수증의 provider key·채널·원본 본문을 작업 기록과 대조한다.
`--workers-stopped`는 이전 프로세스와 요청이 계속 실행될 수 없다는 확인이며 실제 종료 명령이
아니다. `status`로 체크포인트와 미해결 기록을 조회할 수 있다.

### PostgreSQL에 실행 기록 저장

`effect-ledger[langchain,postgres]`를 설치하고 executor 생성 부분만 교체한다.

```python
import os
from effect_ledger.postgres import PostgresOperationStore

# DATABASE_URL=postgresql://user:password@localhost:5432/my_app
executor = EffectExecutor(
    store=PostgresOperationStore(os.environ["DATABASE_URL"]),
    scope="account-1",
)
```

`my_app` DB는 미리 생성하며 이름은 직접 정한다. 저장소는 연결의 `search_path`에
`operations`, `decisions`, `schema_version` 테이블을 생성·마이그레이션한다. 모든 워커는 같은
DB·스키마·scope를 사용한다. 기존 SQLite 기록은 자동 이전되지 않는다. LangGraph의 내구
체크포인터는 별도로 설정한다. 같은 PostgreSQL DB를 사용할 수 있지만 체크포인트와 원장 변경이
하나의 트랜잭션으로 묶이지는 않는다. 저장소를 바꿔도 도구 노드 코드는 그대로다.

## 실행 계약

호스트는 논리 작업 ID를 호출 전에 내구적으로 저장하고 재시도 시 재사용하며, MCP 요청 ID나
모델이 매번 생성하는 툴 호출 ID로 대체하지 않는다. 서버는 계정/테넌트 scope와 효과 이름·버전을
고정한다.

```text
호스트: 작업 ID 저장
  → 서버: scope + 작업 ID에 효과·원본 JSON·제공자 키 결합
  → 저장소: 실행권 획득과 시작 기록 커밋
  → 핸들러: 단일 외부 효과 실행
  → 저장소: 결과 기록
  → 호스트: 결과 수신 또는 미해결 작업 보류
```

같은 scope와 ID에 다른 효과나 요청을 보내면 충돌이다.

- JSON 객체의 키 순서는 무관하지만 값을 바꾸면 새 요청이다.
- 정수와 실수 표현도 구분한다.
- 요청에는 JSON 값만 허용한다.

핸들러는 실행 전에 저장된 `operation.provider_key`를 제공자가 지원할 때 사용하는데, 키를
보존한다고 제공자의 멱등성 보존 기간까지 연장되지는 않는다.

| 상태 | 의미 | 같은 ID로 execute |
|---|---|---|
| `in_flight` | 실행 중이거나 작업자가 죽었을 수 있음 | 현 상태 반환 |
| `indeterminate` | 핸들러 예외 또는 결과 직렬화 실패 | 현 상태 반환 |
| `ready` | 신뢰된 복구 결정이 다음 시도 한 번을 허용함 | 원자적으로 권한 소비 후 실행 |
| `completed` | 실행 또는 외부 확인으로 결과 확정 | 저장 결과 반환 |

앞의 두 상태는 `unresolved=true`이며 시간이 지나거나 취소·재시작이 일어나도 자동으로 해제하지
않는다. 응답의 `next_action`은 `in_flight`이면 `wait`, `indeterminate`이면 `reconcile`,
`ready`이면 `execute`, `completed`이면 `use_result`를 반환한다.

경쟁에서 진 호출자는 먼저 `get_effect`로 완료를 기다린 뒤 복구 경로로 넘긴다.
저장소 오류로 응답을 못 받았을 때도 동일 작업 ID를 유지한다.

핸들러는 동기 함수이며 내부 SDK 재시도와 복수 효과의 부분 성공은 핸들러/제공자 어댑터의
책임이다.

### 리스와 자동 만료 없음

`in_flight`는 작업자가 살아 있다는 뜻이 아니다. 리스도 heartbeat도 없으므로 그 상태에 있는
작업은 지금도 실행 중일 수도 있고 일주일 전에 죽었을 수도 있다. 원장은 둘을 구분하지 못하고
바깥에서도 구분할 수 없다.

작업은 자동으로 만료되지 않는다. 타이머는 제공자 결과를 볼 수 없으므로 시간이 지나도
`in_flight`에서 저절로 빠져나오지 않는다. 호스트가 이전 작업자를 중지하고 제공자 요청을
대조한 뒤 `resolve`로 판정한다.

나중에 리스를 열더라도 그것은 조사 신호이지 실행권이 아니다. 다음 시도를 허용하는 경로는
여전히 `resolve` 하나다.

## MCP 서버 예제

[examples/mcp_server.py](https://github.com/donggyun112/effect-ledger/blob/main/examples/mcp_server.py)는 별도 SQLite 파일에 메시지를 추가하는
로컬 비멱등 메일함이며 실제 메일이나 외부 계정에 접근하지 않는다.

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

등록 효과는 `create_server(executor, effects)`의 서버 측 registry로 제한하고 scope와
provider key는 툴 인자로 받지 않는다. 제공자별 입력 검증은 핸들러가 담당한다.

응답은 `structuredContent`와 JSON text에 동일한 상태를 담는다. 미해결도 유효한 상태 응답이라
`isError=false`일 수 있다.

호스트는 `unresolved`를 검사하고 후속 업무를 보류해야 한다. 모델에게 오류 문장만 보여주는
것으로는 fail-closed가 완성되지 않는다. 서버는 의미적 중복, 곧 같은 업무를 새 ID로 다시
요청하는 경우를 알아낼 수 없다.

`--lose-response`를 추가하면 메일함 저장 후 응답 유실을 흉내 낸다. 재호출해도 메시지는
추가되지 않고 `indeterminate`가 반환된다. 실제 강제 종료 검증은 테스트에 있다.

## 미해결 작업 복구

복구 API는 MCP 툴로 노출하지 않는다. 기존 작업자를 중지하고 이미 전송된 제공자 요청의
상태까지 확인한 뒤, 신뢰할 수 있는 호스트 경로에서 호출한다. `workers_stopped=True`는
호출자의 확인을 기록하며 원격 효과를 차단하지 못한다.

제공자 결과를 확인하고 해석하는 일은 호스트의 비즈니스 로직이다. 자동 복구 워커, webhook,
운영 서비스 또는 관리자 화면에서 처리할 수 있다. `effect-ledger` 콘솔은 판정을 읽고 기록하며
제공자에게 요청을 보내지 않는다.

```console
$ effect-ledger --db effects.sqlite --scope account-1 list
STATE          VER ATT  EFFECT                   OPERATION ID
indeterminate    2   1  payment.charge:v1        charge-1

$ effect-ledger --db effects.sqlite --scope account-1 show charge-1
{ "request": { "amount": 4200, "card": "tok_x" }, "state": "indeterminate", "version": 2, ... }

# 이 요청에 대한 제공자 기록을 확인한 뒤:
$ effect-ledger --db effects.sqlite --scope account-1 resolve charge-1 \
    --complete --result-json '{"charge_id": "ch_77"}' \
    --expected-version 2 --decision-id operator-charge-1 \
    --reason "Stripe shows ch_77; workers drained" --workers-stopped
```

복구 경로가 조사한 버전을 `--expected-version`으로 전달한다. 대조 작업 중 상태가 바뀌면
오래된 버전의 판정을 거부한다. `--db`는 `postgresql://` DSN도 받는다.

```python
from effect_ledger import EffectExecutor

executor = EffectExecutor("/tmp/effects.sqlite", scope="local-mailbox")
record = executor.get("message-1")
if record is None:
    raise LookupError("Unknown operation")

# 메일함의 message_id=1을 확인하고 이전 워커를 중지한 뒤 실행.
executor.resolve(
    "message-1", expected_version=record.version,
    decision_id="operator-confirmed-message-1",
    action="complete", result={"message_id": 1},
    reason="Mailbox confirms message 1; previous workers stopped",
    workers_stopped=True,
)
```

`action="retry"`는 result 없이 다음 실행 한 번을 허용하지만, 실제로 실행되는 것은 호스트가
동일 ID와 효과, 원본 요청으로 execute를 호출했을 때다. `complete`에는 확인된 결과가 필수이며 명시적
`None`도 허용한다. 판정 ID와 인자도 호출 전에 보존해야 한다.

복구는 버전을 확인하고 판정과 전이를 한 트랜잭션에 저장한다. 판정 내용과 사유, 시각은
`decisions` 테이블에 남는다.

동일 판정을 다시 전달하면 현재 상태만 반환한다. 같은 판정 ID에 다른 내용이 오거나 오래된
버전에 새 판정이 오면 거절한다. 늦은 결과는 변경된 버전을 덮어쓰지 못하지만 이미 전송된 외부
요청을 취소하지는 못한다.

`execute()`의 `OperationConflict`만으로 효과 실패를 판단할 수 없다. 요청 바인딩이 다르면
실행 전에 발생한다. 실행권 버전이 바뀐 경우에는 외부 효과가 성공한 뒤 결과를 저장할 때도
발생할 수 있다. 동일 작업을 조회하고 판정하며, 예외만 보고 새 ID로 재시도하지 않는다.

## 저장소와 배포 범위

SQLite `BEGIN IMMEDIATE`로 실행권을 원자적으로 획득하고 `synchronous=FULL`로 효과보다
먼저 커밋한다. 네트워크 호출 중에는 DB 잠금을 유지하지 않는다.

이 보장은 동일 호스트의 프로세스들이 같은 로컬 디스크 DB를 공유하는 범위에서 성립한다.
네트워크 파일시스템이나 다중 호스트용 구현은 아니다. DB 손실과 오래된 백업 복원, 원장 삭제는
보장을 깨뜨리며 자동 만료/삭제는 구현하지 않았다.

원장은 스키마 버전을 갖고 열 때 제자리에서 올라간다. 현재 빌드보다 새 릴리스가 쓴 원장은
시작 시점에 거부하므로 다운그레이드 오류가 한곳에서 드러난다.

다중 호스트에서는 `[postgres]`의 `PostgresOperationStore(dsn)`를 주입한다. 같은 DB와 scope를
사용하는 호스트들이 실행권을 공유한다. scope별 짧은 트랜잭션을 직렬화하며 외부 호출 동안
잠금을 잡지 않는다.

연결 풀과 DB 장애 조치는 포함하지 않는다. 원장의 분산 실행권과 LangGraph
thread 스케줄링은 별개이므로 동일 thread 직렬화는 호스트 책임이다.

scope는 인증을 대신하지 않는다. 예제는 신뢰할 수 있는 단일 호스트의 stdio용이므로, HTTP로
배포하려면 인증과 권한, 계정별 라우팅을 별도로 구성해야 한다.

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

CI는 Python 3.10부터 3.13까지 실제 PostgreSQL 서비스를 띄워 이 스위트를 돌리며, 테스트가
하나라도 skip으로 보고되면 빌드를 실패시킨다.

설계 근거는 `probes/`에 순서대로 남아 있고, 각 파일은 패키지를 import하지 않고 그대로
실행된다. 설계와 구현 계획은 `docs/superpowers/`에 있다.
