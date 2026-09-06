> Historical middleware experiment, not the new server execution contract.
> Run with the `[langchain]` extra. GraphInterrupt does not prove effect safety.

# langgraph-effect-ledger

외부 효과의 복구 권한을 LangChain 에이전트 미들웨어로 얹는다.

LangGraph의 재개는 태스크를 처음부터 재생한다. 완료된 태스크는 체크포인트에서 복원되지만,
**시작만 하고 결과를 남기지 못한 호출**은 다시 실행된다. 공식 문서가 인정하는 동작이다:

> A task that started but did not finish may run again upon resume, so it is crucial to design
> side effects to be idempotent.

즉 재시도 안전은 사용자 몫으로 남아 있다. 이 패키지는 그 자리를 메운다. 효과보다 먼저 시작을
기록하고, 결과 없이 돌아온 호출은 `Indeterminate`로 남긴다. 호출자가 판정을 내리기 전까지
다시 실행하지 않는다.

내구 실행(durable execution)을 대체하지 않는다. 내구 실행은 단계를 재생하고, 이 원장은
**어떤 효과가 이미 밖으로 나갔는지 모른다는 사실**을 기록한다.

## 설치

```bash
pip install langgraph-effect-ledger
```

## 쓰기

```python
from dataclasses import dataclass, field

from langchain.agents import create_agent
from langgraph.checkpoint.memory import InMemorySaver
from langgraph_effect_ledger import EffectLedger, MixedEffectDetector


@dataclass
class Context:
    effect_verdicts: dict[str, str] = field(default_factory=dict)


agent = create_agent(
    model,
    tools,
    middleware=[EffectLedger(store), MixedEffectDetector()],
    checkpointer=InMemorySaver(),
    context_schema=Context,
)
```

프로세스가 죽고 재개하면, 결과를 남기지 못한 호출은 실행되지 않고 `INDETERMINATE`를 돌려준다.
판정은 run-scoped context로 전달한다.

```python
agent.invoke(
    None,
    config,
    context=Context(effect_verdicts={"call-1": "retry_safe"}),
)
```

| 판정 | 뜻 |
|---|---|
| `retry_safe` | 호출자가 재시도 안전을 선언한다. 한 번만 유효하다. |
| `already_done` | 효과가 나간 것이 확인됐다. 재실행하지 않고 완료로 넘긴다. |
| 없음 / 그 밖 | 거부. 호출은 미해결로 남는다. |

**판정을 `interrupt()`로 묻지 말 것.** 미들웨어는 툴보다 바깥이라 항상 먼저 걸리고, 사람이
툴에게 준 답을 가로챈다(`probes/probe_c_verdict.py`).

## 저장소

`EffectStore`는 `get`/`put` 두 개짜리 프로토콜이다. 기본값 `InMemoryEffectStore`는 프로세스와
함께 죽는다 — 이 원장이 막으려는 바로 그 상황에서 기록이 남지 않는다. 실제 사용에는 내구성
있는 저장소를 넣어야 한다.

체크포인터는 쓰지 않는다. 자기 저장소에 직접 쓰므로 체크포인트 쓰기 순서에 의존하지 않는다
([langgraph#8039](https://github.com/langchain-ai/langgraph/issues/8039)).

## 덮지 못하는 것

**한 툴 안에 `[효과 → interrupt()]`가 같이 있으면 막지 못한다.** `wrap_tool_call`은 툴 바깥이라
툴 본문의 재생을 중간에서 자를 수 없다. 재시도를 승인하면 그 툴은 안에서 또 멈추고, 멈추면 또
재생되고, 효과가 또 나간다(`probes/probe_e_stale_approval.py`).

막지 못하므로 지적한다. `MixedEffectDetector`가 두 겹으로 찾는다.

- **런타임** — `handler`에서 `GraphInterrupt`가 올라오면 그 툴은 내부에서 중단한다. 예외라서
  헬퍼 몇 단계를 거치든, 동적 디스패치를 하든 잡힌다.
- **정적** — 그 툴 안에서 효과가 중단보다 앞서는지 소스로 본다. 반복 블록, `try`, 중첩 분기,
  첨자·속성 대입, 1단계 헬퍼까지 본다.

```text
[risky]   effect_then_gate: 효과가 중단보다 먼저 실행된다
[unknown] two_level: 중단 지점을 소스에서 찾지 못했다 (간접 호출이나 동적 디스패치)
```

`unknown`을 `safe`로 접지 않는다. 가설 검증에서 탐지기 두 세대가 정확히 그것 때문에 죽었다
(`probes/probe_j_falsify.py`, `probes/probe_k_falsify2.py`).

이 배치의 올바른 해법은 효과와 `interrupt()`를 다른 툴로 분리하는 것이다. 라이브러리는 그걸
강제하지 못한다.

## 근거

`probes/`에 설계 근거가 순서대로 남아 있다. 각 파일은 그대로 실행된다.

| | |
|---|---|
| `probe_a_replay.py` | `create_agent`는 완료된 툴을 재실행하지 않는다 |
| `probe_b_ledger.py` | 원장이 중복 효과를 막는다 |
| `probe_c_verdict.py` | 미들웨어의 `interrupt()`가 툴의 resume 값을 가로챈다 |
| `probe_d_context.py` | context로 받으면 충돌하지 않는다 |
| `probe_e~g` | 툴 내부 중단은 어떻게 해도 못 막는다 |
| `probe_h_distinguish.py` | `GraphInterrupt`로 중단과 죽음을 구분한다 |
| `probe_i~l` | 탐지기 가설 검증 4라운드 |

## 검증

```bash
uv run python tests/test_ledger.py
uv run python tests/test_detector.py
```

async 경로(`awrap_tool_call`)는 구현돼 있으나 테스트가 없다.

MIT license.
