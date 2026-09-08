"""Account-wide journal survives plan changes; SQLite commits precede side effects."""
import json
import sqlite3


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS journal (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        row = self.db.execute("SELECT body FROM journal WHERE id=1").fetchone()
        self.state = json.loads(row[0]) if row else {
            "version": 1, "trades": {}, "seen": [], "outbox": [], "days": {}, "notices": []}
        if self.state["version"] != 1:
            raise ValueError("Unsupported execution journal")
        self.save()

    def save(self):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO journal VALUES (1, ?)",
                            (json.dumps(self.state, allow_nan=False),))

    def notify(self, key, kind, reason):
        identity = f"{key}:{kind}:{reason}"
        if identity not in self.state["notices"]:
            self.state["notices"].append(identity)
            self.state["outbox"].append(f"[BTC 자동매매] {kind}\n{reason}\nID: {key}")
        self.save()

    def flush(self, sender):
        while self.state["outbox"]:
            sender(self.state["outbox"][0])
            self.state["outbox"].pop(0)
            self.save()

    def close(self):
        self.db.close()
