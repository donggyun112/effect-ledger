# 기존 LangChain 툴에 실행 경계 붙이기

Semora의 네이티브 실행 경계와 결과를 먼저 저장하는 설계를 참고했다. LangChain의 Agent,
BaseTool/StructuredTool, 모델 루프, LangGraph 체크포인터를 그대로 사용한다. 미들웨어를
연결하면 등록된 모든 툴이 기본적으로 보호된다. 툴 인자를 `request={...}`로 다시 작성할
필요가 없다.

```python
from langchain.agents import create_agent
from langchain.tools import tool
from langgraph_effect_ledger import EffectExecutor
from langgraph_effect_ledger.langchain import CONTROL, READ_ONLY, ExecutionBoundary, current_operation
from langgraph_effect_ledger.langgraph import DurableAgentRunner

@tool
def send_confirmation(order_id: str, text: str) -> dict:
    """Send one order confirmation."""
    # provider는 앱의 제공자 SDK. 실제 멱등성 키 지원이 있을 때만 전달한다.
    return provider.send(order_id=order_id, text=text,
                         idempotency_key=current_operation().provider_key)

executor = EffectExecutor("effects.sqlite", scope="account-1")
boundary = ExecutionBoundary(
    executor,
    workflow_id="orders:v1",
)
agent = create_agent(
    model, [send_confirmation],
    middleware=[boundary],
    checkpointer=saver,
)
runner = DurableAgentRunner(agent)
config = {"configurable": {"thread_id": "order-workflow-123"}}
outcome = runner.start({"messages": [("user", "Send the confirmation")]}, config)
```

`model`, `saver`, `provider`는 앱의 모델·내구 체크포인터·제공자 연결이다. `current_operation()`은
선택 기능이다. 기존 툴은 그대로 둘 수 있고, 제공자 키가 필요한 툴만 이 접근자를 사용한다.
접근자는 보호된 실행 안에서만 유효하며 모델 스키마에 인자를 추가하지 않는다.

## 툴 정책

`tools`는 보호 대상을 고르는 allowlist가 아니다. 설정에 없는 툴도 `langchain.tool:<이름>`
효과로 원장을 통과한다. 확실한 읽기 툴, 외부 효과가 없는 제어 툴, 배포 사이에 유지할 효과
이름만 지정한다.

```python
boundary = ExecutionBoundary(
    executor,
    workflow_id="orders:v1",
    tools={
        "search_orders": READ_ONLY,
        "transfer_to_human": CONTROL,
        "send_confirmation": "confirmation.send:v1",
    },
)
```

`READ_ONLY`와 `CONTROL`은 원장 쓰기 없이 기존 handler를 호출한다. `READ_ONLY`는 성능 옵션이
아니라 외부 변경이 없다는 정확성 선언이다. 쓰기 가능성이 있는 툴을 SQLite 경합 때문에
`READ_ONLY`로 지정하면 체크포인트 재실행에서 효과가 중복될 수 있다.

`Command`를 반환하는 handoff·상태 제어 툴은 `CONTROL`로 지정한다. 이를 빠뜨리거나 durable
툴이 JSON이 아닌 artifact를 반환하면 실제 호출 뒤 `indeterminate`로 멈춘다. 반환 타입만으로
안전하게 판별할 수 없으므로 **배포 전에 등록된 모든 툴의 실제 반환 경로를 한 번씩 호출하는
통합 테스트가 필요하다.** 외부 효과가 있는 툴의 artifact는 JSON으로 바꿔야 하며, 오류를
피하려고 그 툴을 `CONTROL`로 지정하면 안 된다.

기본 효과 이름은 툴 이름을 포함한다. 보호된 툴을 rename하면 기존 실행을 재개할 때 효과
바인딩이 충돌한다. **복구 방법은 이전 문자열을 `tools`에 명시하는 것 하나뿐이다.** 그래야
기록된 결과를 그대로 재생하고 효과를 다시 호출하지 않는다.

`workflow_id`를 올리는 것은 복구가 아니다. 실행 ID 공간이 통째로 바뀌어 새 오퍼레이션이
되므로, 이미 `completed`인 효과라도 **한 번 더 실행된다.** 결제·발송 툴이면 중복 청구·중복
발송이다. 새 ID 공간은 의도적으로 새 행동을 일으킬 때만 쓴다.

자동 이름에는 자동으로 올라가지 않는 `:v1` 접미사를 붙이지 않는다.

## 경계와 조합 순서

```text
모델·네이티브 HITL
  → 바깥 툴 미들웨어: 승인 / 재시도 / 결과 후처리
    → ExecutionBoundary: ID·원본 인자 결합 → 실행권 커밋
      → 기존 툴: 단일 효과
    ← ToolMessage 결과 커밋 / 미해결 보류
  ← 결과 후처리
```

`ExecutionBoundary`는 **툴을 감싸는 미들웨어 중 마지막**에 둔다. LangChain은 첫 미들웨어를
가장 바깥에 배치하므로 마지막 경계가 툴에 가장 가깝다. 바깥 재시도가 handler를 반복 호출해도
매번 원장을 통과한다. 바깥 후처리가 결과를 받은 뒤 실패해도 이미 저장된 툴 결과를 재생한다.
[LangChain 미들웨어 계약](https://docs.langchain.com/oss/python/langchain/middleware/custom)

Semora는 자체 정책·원장 협력자를 하나의 바깥 capability가 조정한다. 여기서는 LangChain의
다른 툴 미들웨어가 임의로 재시도할 수 있으므로 **배치까지 똑같이 옮기지 않았다**.
경계 안쪽에 재시도 미들웨어를 놓으면 한 번의 실행권 안에서 툴이 여러 번 실행될 수 있다.
경계 바깥 미들웨어 자체의 외부 효과도 보호 대상이 아니다.

네이티브 `HumanInTheLoopMiddleware`와 함께 쓸 수 있다. 일반 승인은 명시적인 사용자 응답을
요구하고, 승인 전에는 툴의 실행권을 획득하지 않는다. `resume()`의 복구 신호는 사람의 승인이나
제공자 재시도 허가를 대신하지 않는다.

## 결과와 복구

성공한 ToolMessage의 content·artifact·추가 메타데이터를 JSON으로 보존한다. 재생할 때 메시지
ID는 새 그래프 메시지로 부여되고 tool_call_id는 현재 호출에 연결된다. 같은 업무 ID를 다른
그래프 호출에서 조회해도 과거 tool_call_id를 반환하지 않는다.

일반 예외와 status=error인 ToolMessage는 `indeterminate`가 된다. Semora의 일반 오류 결과
완료 처리와 의도적으로 다르다. TimeoutError만으로 외부 효과의 실패를 확정하지 않는다.
취소·GraphInterrupt는 실행권을 해제하지 않는다. 보호된 툴 내부 interrupt를 일반 승인 경로로
사용하지 말고, 승인 게이트를 효과 경계 앞에 둔다.

운영자가 실제 결과를 확인해 complete를 기록할 때는 메시지 결과 형식을 지정한다.

```python
# pending은 outcome['__interrupt__'][0].value에서 얻은 원장 상태다.
# 이전 워커와 전송된 요청을 정리하고 제공자의 실제 결과를 확인한 경우:
executor.resolve(
    pending["operation_id"], expected_version=pending["version"],
    decision_id="verified-confirmation-123", action="complete",
    reason="Provider confirmed this exact operation", workers_stopped=True,
    result=ExecutionBoundary.result("Confirmation sent", artifact={"message_id": "m-123"}),
)
outcome = runner.resume(config)
```

`RecoveryPolicy`도 같은 envelope를 result로 반환한다. `ExecutionBoundary.result()`는 메시지
포맷 생성기이며 제공자 성공을 확인하거나 복구를 승인하는 함수가 아니다. 잘못된 완료 결과
포맷은 `result_error`로 중단된다. 저장된 완료 결과는 불변이므로 올바른 포맷으로 판정해야 한다.

## 지원 범위

- 기존 동기·비동기 툴을 지원한다. 비동기 그래프에는 내구 AsyncSqliteSaver 등의 체크포인터와
  `runner.astart/aresume`을 쓴다. 저장소 I/O는 워커 스레드로 분리한다.
- 등록된 모든 툴을 기본적으로 보호한다. `READ_ONLY`와 `CONTROL`로 선언한 툴만 원장을
  우회한다.
- durable 툴은 단일 외부 효과, 고정된 의미, JSON으로 표현 가능한 결과라는 계약이 필요하다.
  복수 효과·내부 승인 interrupt는 지원하지 않는다. 실행 뒤 지원하지 않는 결과가 나오면
  미해결로 보류한다.
- 제공자 계정은 executor.scope에 고정한다. 효과에 영향을 주는 값은 원장에 결합되는 툴 인자에
  넣고, 구현 의미가 바뀌면 effect 버전을 바꾼다. runtime.context나 외부 mutable 상태에서
  수신자·금액 등을 가져오면 저장된 인자만으로 재실행 의미를 고정할 수 없다.
- `operation_id=lambda runtime: ...`로 호스트 업무 ID를 제공할 수 있다. 생략하면 workflow와
  체크포인트의 부모 메시지·툴 호출 ID로 파생한다. 서로 다른 업무는 다른 ID가 필요하다.
- SDK 내부 재시도는 이 경계보다 안쪽이다. 제공자 멱등성 계약에 맞게 설정해야 한다.
- 같은 thread의 호출 직렬화와 내구 체크포인터는 호스트 책임이다. root create_agent의 최신
  체크포인트 범위이며 임의 StateGraph·서브그래프 지원을 주장하지 않는다.
- 같은 툴에 `durable_tool`과 ExecutionBoundary를 이중 적용하지 않는다. 원격 MCP 실행에는
  기존 durable_tool/transport 경로를 사용할 수 있다.

## 검증

```bash
uv run --all-extras python -m unittest discover -s tests -p test_execution_boundary.py -v
uv run --all-extras python -m unittest discover -s tests -p test_langgraph_crash.py -k boundary -v
```

실제 create_agent에서 원래 스키마, 오류 ToolMessage 보류, 완료 결과·artifact 재생, 호스트 ID,
인자 충돌, 바깥 재시도, 후처리 실패, 네이티브 HITL과 async를 검사한다. 별도 프로세스·HTTP
제공자로 외부 커밋 뒤 SIGKILL, 원장 커밋 뒤 SIGKILL, 허용된 재시도 중 두 번째 SIGKILL도 검사한다.
