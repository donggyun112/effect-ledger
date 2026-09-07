# durable_tool: 원격 전송과 MCP 경로

> **이 문서는 보조 경로다.** 대부분의 경우 [ExecutionBoundary 가이드](langchain-boundary.md)를
> 읽어야 한다. 이미 가진 툴에 미들웨어 하나를 얹으면 끝이고, 그게 추천 경로다.
>
> `durable_tool`은 그것으로 부족할 때만 쓴다. 효과를 **다른 프로세스가 실행**하고 이쪽은
> `execute(operation_id, effect, request)` 전송만 담당하는 경우 — MCP 서버, 원격 HTTP
> 제공자처럼 원장이 이 프로세스 밖에 있는 배치다. 같은 툴에 둘을 겹쳐 쓰지 않는다.

`create_agent`의 명시적인 효과 툴 안에 복구 중단을 둔다. 서버의 원장이 실행권을 소유하고,
LangGraph 체크포인터가 원래 모델 요청과 업무 진행 상태를 보존한다.

## 직접 실행하기

```bash
uv sync --all-extras
uv run --all-extras python examples/recovery_agent.py --state-dir /tmp/recovery-agent-demo start --lose-response
uv run --all-extras python examples/recovery_agent.py --state-dir /tmp/recovery-agent-demo status
uv run --all-extras python examples/recovery_agent.py --state-dir /tmp/recovery-agent-demo resume
```

외부 API 키 없이 실행된다. 결정적인 예제 모델이 MCP 서버의 로컬 메일함에 `hello`를
한 번 기록한 뒤 응답 유실을 흉내 낸다. 마지막 resume도 판정이 없으므로 중단 상태를 유지한다.
`interrupts`의 `operation_id`, `version`으로 미해결 작업을 식별한다.

이전 예제 프로세스들이 종료됐고 메일함의 해당 행이 실제 작업임을 확인한 후, 출력의 값을 넣는다.

```bash
uv run --all-extras python examples/recovery_agent.py --state-dir /tmp/recovery-agent-demo confirm \
  --operation-id <출력된-operation_id> --version <출력된-version> \
  --decision-id confirmed-message-1 --message-id 1 --workers-stopped
uv run --all-extras python examples/recovery_agent.py --state-dir /tmp/recovery-agent-demo resume
```

`completed: true`와 최종 모델 응답을 반환한다. 메일함 DB의 메시지는 여전히 한 건이다.
confirm은 모델 툴이 아닌 로컬 운영자 명령이며 지정된 행과 요청 내용을 확인한다.
실제 서비스에서는 원본 작업과 제공자 결과의 상관관계를 더 엄격히 확인해야 한다.
동일 내용을 보낸 다른 작업은 같은 작업의 증거가 아니다.

새 시나리오는 다른 state-dir 또는 새 thread로 실행한다. 파일 삭제 후 기존 thread 재사용은
내구성 계약을 깨뜨린다. 예제는 checkpoints.sqlite(그래프), effects.sqlite(원장),
mailbox.sqlite(가짜 외부 시스템)의 독립된 세 DB를 유지한다.

## 애플리케이션 연결

durable_tool은 `execute(operation_id, effect, request)` 동기 콜백을 받는 StructuredTool이다.
콜백은 서버의 Operation.response() 형태를 반환한다. executor를 직접 내장하거나
StdioEffectClient.execute로 MCP 서버에 연결할 수 있다.

```python
from langchain.agents import create_agent
from effect_ledger.langgraph import LedgerRunner, durable_tool

# model, saver, transport는 앱의 모델·내구 체크포인터·효과 서버 연결이다.
send = durable_tool(
    name="send_message", description="Send one message",
    workflow_id="mail-agent:v1", effect="message.send:v1",
    execute=transport.execute,
)
runner = LedgerRunner(create_agent(model, [send], checkpointer=saver))
config = {"configurable": {"thread_id": "business-workflow-123"}}
result = runner.start({"messages": [("user", "send a message")]}, config)
# 운영자 판정이 서버에 저장된 뒤:
result = runner.resume(config)
```

동기는 SqliteSaver, 비동기는 AsyncSqliteSaver와 `await runner.astart(...)` /
`await runner.aresume(...)`를 사용한다. 인메모리 체크포인터와 체크포인터 없는 그래프는
거절한다. 다른 체크포인터의 실제 내구성은 앱이 보장해야 한다.

## 보장하는 흐름

1. `durability="sync"`로 AIMessage와 툴 인자를 효과 실행 전에 저장한다.
2. 호스트의 operation_id 콜백 또는 workflow_id·thread_id·부모 AIMessage ID·tool_call_id로 작업 ID를 만든다.
   재시작에서는 같고 새 모델 턴의 의도적인 작업은 다르다. 효과 버전은 ID에 포함하지 않아,
   기존 작업의 효과 버전을 바꾸면 서버가 충돌을 감지한다.
3. 서버가 시작 기록을 커밋한 뒤 효과를 실행한다.
4. 미해결/전송 오류/잘못된 응답이면 툴이 interrupt한다. 오류 ToolMessage를 모델에 넘겨
   다음 판단으로 진행하지 않는다. 미해결 thread에 새 입력을 넣는 start도 거절한다.
5. 운영자는 서버의 resolve로 특정 버전에 완료 확인 또는 한 번의 재시도를 기록한다.
6. resume는 저장된 인자로 재진입한다. **재개 값은 재시도 승인이 아니다.** 서버를 확인해
   미해결이면 다시 멈추고 완료면 JSON 결과를 ToolMessage로 복원한다.
7. 그래프가 모델의 최종 응답까지 진행한다. 완료한 그래프의 resume는 새 업무를 시작하지 않는다.

판정 저장과 그래프 재개는 단일 트랜잭션이 아니다. 그 사이에 죽어도 같은 판정 재전달과
같은 그래프 재개가 안전하도록 양쪽에 내구 기록을 남긴다. 일반 사용자 승인 interrupt에는
`responses={interrupt_id: answer}`를 명시해야 한다. runner의 자동 신호는 복구 재확인에 한정한다.

## 검증한 장애 구간

실제 HTTP 제공자·MCP 서버·LangGraph 작업자를 실행한다. 제공자는 자체 DB에 효과를 저장하고
테스트가 작업자와 MCP 자식을 SIGKILL한다. 로컬 executor와 MCP 경로 양쪽을 검증한다.

- 외부 커밋 직후, 응답/원장 완료 전: 새 에이전트는 미해결로 멈춘다.
- 원장 완료 후, 그래프가 결과를 받기 전: 새 에이전트는 결과를 재생한다.
- 운영자 완료 판정 후, 재개 전: 새 프로세스가 결과를 받아 최종 응답까지 진행한다.
- 허용된 재시도의 외부 커밋 직후 다시 종료: 이전 판정 재전달은 추가 시도를 허용하지 않는다.

효과 횟수와 모델 계획 호출 횟수, checkpoint에 보존된 인자를 검사한다. 별도로 병렬 툴의
완료된 형제 호출, async, 일반 HITL, 반복 재개, 새 모델 턴의 ID 재사용을 검증한다.

```bash
uv run --all-extras python -m unittest discover -s tests -p 'test_langgraph*.py' -v
uv run --all-extras python -m unittest discover -s tests -p test_recovery_agent_example.py -v
```

## 적용 범위

- root create_agent의 최신 체크포인트만 지원한다. subgraph namespace·time travel·직접
  update_state로 원래 요청을 수정하는 복구는 지원하지 않는다.
- 같은 thread의 호출은 호스트가 직렬화한다. runner는 분산 스케줄러/분산 잠금이 아니다.
- graph.invoke 직접 호출은 runner의 입력 차단과 durability 설정을 우회한다.
- workflow namespace, 부모 메시지, 체크포인트, 서버 scope와 원장을 함께 보존한다.
- 새 모델 요청으로 같은 업무를 다시 계획하는 의미적 중복은 구별하지 못한다.
- 모델 호출 비용, 임의 노드의 효과, 제공자 SDK 내부 재시도까지 보호하지 않는다.
- 제공자 조회는 호스트의 RecoveryPolicy로 연결할 수 있다. 기본값은 수동 판정이며,
  시간만으로 재시도 안전을 추론하지 않는다. [조합 API](composition.md)를 참조한다.
- MCP stdio는 호출마다 서버 세션을 연다. 각 재개에서 서버 확인은 한 번이다.
  반복 재개 시 interrupt 이력은 누적된다. 폴링에는 status/get_effect를 사용하고,
  판정이나 외부 상태가 바뀌었을 때 resume한다.

원장 응답의 `next_action=wait`는 다른 실행이 완료될 수 있으므로 먼저 상태를 기다리라는
뜻이다. 생존 증명이 아니다. 리스가 없는 현재 구현에서 중단된 작업자인지는 호스트가
확인해야 한다. `next_action=reconcile`은 효과 확인을 운영자에게 넘긴다. 양쪽 모두
미해결 동안 그래프의 후속 업무는 보류한다.
