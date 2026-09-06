"""MCP server fixture executing a single request against the fake HTTP provider."""

import json
import os
import sys
from pathlib import Path
from urllib.request import Request, urlopen

from langgraph_effect_ledger.mcp import create_server
from langgraph_effect_ledger.operations import EffectExecutor

(Path(sys.argv[1]).parent / f"mcp-server-{os.getppid()}.pid").write_text(str(os.getpid()))


def send(call):
    with urlopen(Request(sys.argv[2], json.dumps(call.request).encode(), method="POST"), timeout=15) as response:
        return json.load(response)


create_server(EffectExecutor(sys.argv[1], scope="test-account"), {"message.send:v1": send}).run()
