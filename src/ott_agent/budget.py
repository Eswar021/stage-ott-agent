"""Persistent, conservative reservations for this application's paid inference.

Reservations survive process failure. Unknown/failed request billing is kept as
reserved rather than treating failure as free. This ledger cannot see unrelated
spending on the same OpenAI account.
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class BudgetExceeded(RuntimeError):
    """Another request would exceed a local spending ceiling."""


class UnknownPricing(ValueError):
    """No reviewed pricing exists for the requested model."""


@dataclass(frozen=True)
class Pricing:
    input_per_million: float
    output_per_million: float
    cached_input_per_million: float
    cache_write_multiplier: float = 1.25

    def upper_cost(self, input_tokens: int, output_tokens: int) -> float:
        # Treat all input as a potential cache write; never rely on a cache hit
        # or discount to admit a request. Reasoning tokens are in output_tokens.
        return (max(0, input_tokens) * self.input_per_million * self.cache_write_multiplier
                + max(0, output_tokens) * self.output_per_million) / 1_000_000


# Reviewed 2026-10-31: https://developers.openai.com/api/docs/pricing
# Unknown models fail closed; changing a model must include reviewing its price.
PRICES = {"gpt-6-luna": Pricing(0.10, 0.50, 0.01)}


def pricing_for(model: str) -> Pricing:
    try:
        return PRICES[model]
    except KeyError as exc:
        raise UnknownPricing(f"No reviewed pricing for model {model!r}") from exc


def conservative_input_bound(payload: Any) -> int:
    """Byte-level upper allowance including API framing/schema overhead.

    The provider also applies a separate tokenizer-based context limit. This
    deliberately over-reserves cost; it is not an accurate token estimate.
    """
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    return len(raw.encode("utf-8")) + 4096


class BudgetLedger:
    def __init__(self, path: str | Path, *, run_id: str,
                 run_limit_usd: float = 0.50, total_limit_usd: float = 1.00):
        if not all(math.isfinite(v) and v >= 0 for v in (run_limit_usd, total_limit_usd)):
            raise ValueError("Budget limits must be finite and nonnegative")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.run_limit_usd = run_limit_usd
        self.total_limit_usd = total_limit_usd
        self.db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=15000")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS inference_budget (
              reservation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
              model TEXT NOT NULL, status TEXT NOT NULL,
              reserved_usd REAL NOT NULL, charged_usd REAL,
              input_bound INTEGER NOT NULL, output_bound INTEGER NOT NULL,
              input_tokens INTEGER, output_tokens INTEGER,
              created_at REAL NOT NULL, note TEXT
            )""")
        self.db.execute("CREATE INDEX IF NOT EXISTS budget_run ON inference_budget(run_id)")

    def _used(self, run_id: str | None = None) -> float:
        query = "SELECT COALESCE(SUM(COALESCE(charged_usd,reserved_usd)),0) FROM inference_budget"
        args: tuple = ()
        if run_id is not None:
            query += " WHERE run_id=?"
            args = (run_id,)
        return float(self.db.execute(query, args).fetchone()[0])

    def reserve(self, model: str, *, input_bound: int, output_bound: int) -> str:
        if type(input_bound) is not int or type(output_bound) is not int or input_bound < 0 or output_bound <= 0:
            raise ValueError("Invalid request token bounds")
        amount = pricing_for(model).upper_cost(input_bound, output_bound)
        reservation = uuid.uuid4().hex
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if self.db.execute("SELECT COUNT(*) FROM inference_budget WHERE note LIKE 'usage_exceeded_reservation%'").fetchone()[0]:
                raise BudgetExceeded("A prior request exceeded its reservation; review pricing/token bounds before more inference")
            if self._used(self.run_id) + amount > self.run_limit_usd + 1e-12:
                raise BudgetExceeded("Per-run live inference budget exhausted")
            if self._used() + amount > self.total_limit_usd + 1e-12:
                raise BudgetExceeded("Cumulative live inference budget exhausted")
            self.db.execute("""INSERT INTO inference_budget
                (reservation_id,run_id,model,status,reserved_usd,input_bound,output_bound,created_at)
                VALUES(?,?,?,'reserved',?,?,?,?)""",
                (reservation, self.run_id, model, amount, input_bound, output_bound, time.time()))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return reservation

    def settle(self, reservation: str, usage: dict[str, Any] | None) -> float:
        """Reconcile reported usage; missing usage retains the full reserve."""
        row = self.db.execute("SELECT * FROM inference_budget WHERE reservation_id=?", (reservation,)).fetchone()
        if row is None:
            raise KeyError("Unknown inference reservation")
        if not usage or "input_tokens" not in usage or "output_tokens" not in usage:
            self.uncertain(reservation, "response_usage_missing")
            return float(row["reserved_usd"])
        inputs, outputs = int(usage["input_tokens"]), int(usage["output_tokens"])
        if inputs < 0 or outputs < 0:
            self.uncertain(reservation, "invalid_response_usage")
            return float(row["reserved_usd"])
        cost = pricing_for(row["model"]).upper_cost(inputs, outputs)
        # A higher-than-reserved usage signal is never silently clamped. The
        # provider treats the breach as a configuration error for future calls.
        note = "conservative_usage_cost_including_possible_cache_write"
        if cost > row["reserved_usd"] + 1e-12:
            note = "usage_exceeded_reservation;future_calls_must_stop"
        self.db.execute("""UPDATE inference_budget SET status='settled',charged_usd=?,
            input_tokens=?,output_tokens=?,note=? WHERE reservation_id=?""",
            (cost, inputs, outputs, note, reservation))
        return cost

    def uncertain(self, reservation: str, note: str = "request_failed_billing_unknown") -> None:
        # Notes must be our short error labels, not raw provider exception text.
        self.db.execute("UPDATE inference_budget SET status='uncertain',note=? WHERE reservation_id=?",
                        (note[:160], reservation))

    def summary(self) -> dict[str, Any]:
        rows = self.db.execute("SELECT status,COUNT(*) n FROM inference_budget GROUP BY status").fetchall()
        breach = self.db.execute("SELECT COUNT(*) FROM inference_budget WHERE note LIKE 'usage_exceeded_reservation%'").fetchone()[0]
        return {"run_id": self.run_id, "run_limit_usd": self.run_limit_usd,
                "total_limit_usd": self.total_limit_usd,
                "run_accounted_usd": round(self._used(self.run_id), 9),
                "total_accounted_usd": round(self._used(), 9),
                "request_states": {r["status"]: r["n"] for r in rows},
                "reservation_breaches": int(breach),
                "cost_basis": "conservative standard pricing; includes possible cache-write premium",
                "scope": "this local application ledger only; not account balance"}

    def close(self) -> None:
        self.db.close()
