"""Two native tools in a root StateGraph, with no model or external API required.

uv run --extra langchain python examples/ledger_tool_node.py --state-dir /tmp/node-demo start --lose-response
uv run --extra langchain python examples/ledger_tool_node.py --state-dir /tmp/node-demo resume
uv run --extra langchain python examples/ledger_tool_node.py --state-dir /tmp/node-demo confirm --workers-stopped
uv run --extra langchain python examples/ledger_tool_node.py --state-dir /tmp/node-demo resume

Each command is a separate process. Mail and Slack are simulated by committed
rows in deliveries.sqlite. Only the Slack reply is lost; both effects happened.
Confirm checks those rows before settling the ledger. Use a fresh state directory
for a new demo. Never run confirm while start/resume or provider work is active.
"""
import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, MessagesState, StateGraph

from effect_ledger import EffectExecutor
from effect_ledger.langchain import current_operation
from effect_ledger.langgraph import LedgerRunner, LedgerToolNode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('start').add_argument('--lose-response', action='store_true')
    commands.add_parser('resume')
    commands.add_parser('status')
    commands.add_parser('confirm').add_argument('--workers-stopped', action='store_true', required=True)
    args = parser.parse_args()
    args.state_dir.mkdir(parents=True, exist_ok=True)
    provider_db = args.state_dir / 'deliveries.sqlite'
    executor = EffectExecutor(args.state_dir / 'effects.sqlite', scope='local-notifications')
    with closing(sqlite3.connect(provider_db)) as db:
        db.execute('CREATE TABLE IF NOT EXISTS deliveries ('
                   'id INTEGER PRIMARY KEY, channel TEXT NOT NULL, '
                   'provider_key TEXT NOT NULL, text TEXT NOT NULL)')
        db.commit()

    def deliver(channel, text):
        operation = current_operation()
        with closing(sqlite3.connect(provider_db)) as db:
            receipt = db.execute(
                'INSERT INTO deliveries(channel, provider_key, text) VALUES (?, ?, ?)',
                (channel, operation.provider_key, text),
            ).lastrowid
            db.commit()
        if channel == 'slack' and getattr(args, 'lose_response', False):
            raise TimeoutError('Slack accepted the message; its reply was lost')
        return f'Delivered via {channel}', {'receipt': receipt}

    @tool(response_format='content_and_artifact')
    def send_mail(text: str):
        """Send one simulated email."""
        return deliver('mail', text)

    @tool(response_format='content_and_artifact')
    def send_slack(text: str):
        """Send one simulated Slack message."""
        return deliver('slack', text)

    builder = StateGraph(MessagesState)
    builder.add_node('tools', LedgerToolNode(
        [send_mail, send_slack], executor=executor, workflow_id='notifications:v1',
        policies={'send_mail': 'mail.send:v1', 'send_slack': 'slack.send:v1'},
    ))
    builder.add_edge(START, 'tools')
    builder.add_edge('tools', END)
    config = {'configurable': {'thread_id': 'order-123'}}

    with SqliteSaver.from_conn_string(str(args.state_dir / 'graph.sqlite')) as saver:
        runner = LedgerRunner(builder.compile(checkpointer=saver))
        confirmed = 0
        if args.command == 'start':
            # An application's model node normally produces this AIMessage.
            outcome = runner.start({'messages': [AIMessage(
                content='', id='order-123-notifications', tool_calls=[
                    {'name': 'send_mail', 'args': {'text': 'Order 123 confirmed'}, 'id': 'mail-123'},
                    {'name': 'send_slack', 'args': {'text': 'Order 123 confirmed'}, 'id': 'slack-123'},
                ],
            )]}, config)
        elif args.command == 'resume':
            outcome = runner.resume(config)
        else:
            if args.command == 'confirm':
                for operation in executor.unresolved():
                    channel = {'mail.send:v1': 'mail', 'slack.send:v1': 'slack'}[operation.effect]
                    with closing(sqlite3.connect(provider_db)) as db:
                        receipts = db.execute(
                            'SELECT id FROM deliveries WHERE provider_key=? AND channel=? AND text=?',
                            (operation.provider_key, channel, operation.request['text']),
                        ).fetchall()
                    if len(receipts) != 1:
                        raise RuntimeError('Expected exactly one matching delivery; leave unresolved')
                    receipt = receipts[0][0]
                    executor.resolve(
                        operation.operation_id, expected_version=operation.version,
                        decision_id=f'verified-delivery-{receipt}', action='complete',
                        result=LedgerToolNode.result(f'Delivered via {channel}', artifact={'receipt': receipt}),
                        reason=f'Local provider receipt {receipt} matches this operation; workers stopped',
                        workers_stopped=args.workers_stopped,
                    )
                    confirmed += 1
            outcome = runner.graph.get_state(config).values

        snapshot = runner.graph.get_state(config)
        with closing(sqlite3.connect(provider_db)) as db:
            deliveries = dict(db.execute('SELECT channel, count(*) FROM deliveries GROUP BY channel'))
        print(json.dumps({
            'status': ('paused' if snapshot.next or snapshot.interrupts
                       else ('completed' if snapshot.values else 'not_started')),
            'deliveries': deliveries,
            'confirmed': confirmed,
            'interrupts': [item.value for item in snapshot.interrupts],
            'unresolved': [operation.response() for operation in executor.unresolved()],
            'results': [{'tool': message.name, 'content': message.content, 'artifact': message.artifact}
                        for message in outcome.get('messages', []) if isinstance(message, ToolMessage)],
        }, indent=2))


if __name__ == '__main__':
    main()
