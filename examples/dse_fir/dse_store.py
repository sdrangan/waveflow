"""Durable, transactional replay execution. One SQLite store per experiment.

The transaction spans each bounded local evaluation: duplicate concurrent calls cannot
spend twice. A killed process rolls back; no external toolchain side effect is executed
here. Live/remote backends will require a leased job/outbox, not this short-call runner.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .contracts import Experiment, canonical, identity


class Store:
    def __init__(self, root, config, semantics):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "experiments.sqlite3"
        with self.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS observations (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                candidate TEXT, operation TEXT NOT NULL, charge INTEGER NOT NULL,
                row_json TEXT NOT NULL)""")
            previous = db.execute("SELECT value FROM metadata WHERE key='experiment'").fetchone()
            if previous:
                saved = json.loads(previous[0])
                if config is not None and canonical(config) != canonical(saved["config"]):
                    raise ValueError("experiment configuration differs; use a new root")
                if canonical(semantics) != canonical(saved["semantics"]):
                    raise ValueError("execution semantics changed; use a new root to preserve evidence")
                self.manifest = saved
            else:
                config = config if config is not None else Experiment().model_dump()
                self.manifest = {"config": config, "semantics": semantics,
                                 "experiment_id": identity({"config": config, "semantics": semantics})}
                db.execute("INSERT INTO metadata VALUES ('experiment', ?)", (canonical(self.manifest),))

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=120)
        try:
            db.execute("PRAGMA busy_timeout=120000")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def run(self, operation, params, evaluation_key, limit, evaluate, invalid=None):
        started = time.monotonic()
        candidate_id = None if invalid else identity(params)
        key = identity({"experiment": self.manifest["experiment_id"], "operation": operation,
                        "request": evaluation_key, "invalid": invalid is not None})
        with self.transaction() as db:
            known = db.execute("SELECT row_json FROM observations WHERE id=?", (key,)).fetchone()
            if known:
                return {**json.loads(known[0]), "cached": True,
                        "cost": {"units": 0, "wall_seconds": time.monotonic() - started}}
            spent = db.execute("SELECT COALESCE(SUM(charge),0) FROM observations WHERE operation=?",
                               (operation,)).fetchone()[0]
            charge = 0
            if invalid:
                payload = {"status": "invalid", "error": invalid, "metrics": {}, "evidence_kind": "none"}
            elif spent >= limit:
                payload = {"status": "budget_exhausted", "metrics": {}, "evidence_kind": "none"}
            else:
                charge = 1
                try:
                    payload = evaluate()
                    canonical(payload)  # Reject NaN/Inf before committing; never emit invalid JSON.
                    if (not isinstance(payload, dict)
                            or not isinstance(payload.get("status"), str)
                            or not isinstance(payload.get("evidence_kind"), str)
                            or not isinstance(payload.get("metrics"), dict)):
                        raise TypeError("backend must return status, evidence_kind and a metrics object")
                except Exception as exc:  # noqa: BLE001 -- domain failures are durable observations
                    payload = {"status": "failed", "metrics": {}, "evidence_kind": "none",
                               "error": f"{type(exc).__name__}: {exc}"}
            row = {"evaluation_id": key, "candidate_id": candidate_id, "operation": operation,
                   "params": params, "status": payload["status"], "cached": False,
                   "cost": {"units": charge, "wall_seconds": time.monotonic() - started},
                   "result": payload, "experiment_id": self.manifest["experiment_id"]}
            db.execute("INSERT INTO observations(id,candidate,operation,charge,row_json) VALUES (?,?,?,?,?)",
                       (key, candidate_id, operation, charge, canonical(row)))
        return row

    def rows(self):
        with sqlite3.connect(self.path) as db:
            return [json.loads(r[0]) for r in db.execute("SELECT row_json FROM observations ORDER BY seq")]

    def spent(self):
        with sqlite3.connect(self.path) as db:
            return dict(db.execute("SELECT operation,SUM(charge) FROM observations GROUP BY operation"))
