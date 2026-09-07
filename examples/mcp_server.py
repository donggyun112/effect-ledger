"""Local non-idempotent mailbox demo. No real messages or external accounts."""

import argparse
import sqlite3
from contextlib import closing

from effect_ledger.mcp import create_server
from effect_ledger.operations import EffectExecutor, Operation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--mailbox", required=True)
    parser.add_argument("--lose-response", action="store_true",
                        help="Simulate response loss after the independent mailbox commits")
    args = parser.parse_args()
    with closing(sqlite3.connect(args.mailbox)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS messages (body TEXT NOT NULL)")

    def send(call: Operation):
        text = call.request.get("text")
        if not isinstance(text, str) or not text:
            raise ValueError("text must be a nonempty string")
        with closing(sqlite3.connect(args.mailbox)) as db, db:
            cursor = db.execute("INSERT INTO messages(body) VALUES (?)", (text,))
            message_id = cursor.lastrowid
        if args.lose_response:
            raise TimeoutError("Mailbox committed, but response was lost")
        return {"message_id": message_id}

    create_server(
        EffectExecutor(args.ledger, scope="local-mailbox"), {"message.send:v1": send},
    ).run(transport="stdio")


if __name__ == "__main__":
    main()
