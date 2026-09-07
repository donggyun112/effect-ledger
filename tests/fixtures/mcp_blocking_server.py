"""Real stdio cancellation fixture: record one effect, await a release file."""

import sys
import time
from pathlib import Path

from effect_ledger.mcp import create_server
from effect_ledger.operations import EffectExecutor

root = Path(sys.argv[1])


def send(call):
    with (root / "effects").open("a") as log:
        log.write("effect\n")
    (root / "entered").touch()
    deadline = time.monotonic() + 10
    while not (root / "release").exists():
        if time.monotonic() > deadline:
            raise TimeoutError("Test did not release handler")
        time.sleep(0.01)
    return "done"


create_server(EffectExecutor(root / "ledger.sqlite", scope="test"), {"send:v1": send}).run()
