"""Online learned model for entry quality.

A logistic regression trained by SGD on scored council decisions: features at
decision time -> probability that the next ``DECISION_SCORE_MINUTES`` were
seller-friendly (|move| < 1σ of the horizon-scaled expected move). Weights
persist in agent memory. It is deliberately small and transparent: the
dashboard shows the weights, and the LearnedModel agent votes with a score
proportional to (p - 0.5) and confidence that grows with the sample size.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List

FEATURES = ["rsi_c", "iv_minus_rv", "vix_rank_c", "pcr_c", "dte_log", "ret5_abs", "trend_flat", "regime_range"]


def vectorize(f: Dict[str, Any]) -> List[float] | None:
    try:
        rsi = f.get("rsi")
        iv, rv = f.get("iv_atm"), f.get("realized_vol")
        vr = f.get("vix_rank")
        pcr = f.get("pcr")
        dte = f.get("dte")
        ret5 = f.get("ret_5m_pct")
        if iv is None or pcr is None or dte is None:
            return None
        return [
            ((rsi if rsi is not None else 50.0) - 50.0) / 25.0,
            ((iv - rv) / 5.0) if rv is not None else 0.0,
            ((vr if vr is not None else 50.0) - 50.0) / 50.0,
            (pcr - 1.0) / 0.3,
            math.log(max(float(dte), 0.05)),
            abs(ret5 or 0.0) / 0.3,
            1.0 if f.get("trend") == "FLAT" else 0.0,
            1.0 if f.get("regime") == "RANGE" else 0.0,
        ]
    except Exception:
        return None


class OnlineLogit:
    def __init__(self, lr: float = 0.05, l2: float = 0.001) -> None:
        self.w = [0.0] * len(FEATURES)
        self.b = 0.0
        self.lr, self.l2 = lr, l2
        self.n = 0
        self.loss_ema: float | None = None

    def predict(self, x: List[float]) -> float:
        z = self.b + sum(wi * xi for wi, xi in zip(self.w, x))
        z = max(-30.0, min(30.0, z))
        return 1.0 / (1.0 + math.exp(-z))

    def update(self, x: List[float], y: float) -> float:
        p = self.predict(x)
        g = p - y
        self.b -= self.lr * g
        self.w = [wi - self.lr * (g * xi + self.l2 * wi) for wi, xi in zip(self.w, x)]
        self.n += 1
        loss = -(y * math.log(max(p, 1e-9)) + (1 - y) * math.log(max(1 - p, 1e-9)))
        self.loss_ema = loss if self.loss_ema is None else 0.95 * self.loss_ema + 0.05 * loss
        return p

    def to_dict(self) -> Dict[str, Any]:
        return {"w": self.w, "b": self.b, "n": self.n, "loss_ema": self.loss_ema, "features": FEATURES}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> OnlineLogit:
        m = cls()
        if d and len(d.get("w", [])) == len(FEATURES):
            m.w, m.b, m.n, m.loss_ema = list(d["w"]), float(d.get("b", 0.0)), int(d.get("n", 0)), d.get("loss_ema")
        return m


class EntryQualityModel:
    """Owns the logit, trains it from newly scored decisions, persists to agent memory."""

    KEY = "entry_quality_model"

    def __init__(self, db) -> None:
        self.db = db
        self.model = OnlineLogit.from_dict(db.memory_get(self.KEY, {}) or {})
        self._trained_ids = set(db.memory_get(self.KEY + ":ids", []) or [])

    def train_pending(self) -> int:
        n = 0
        for row in self.db.scored_decisions(500):
            if row["id"] in self._trained_ids:
                continue
            sf = row["outcome"].get("seller_friendly")
            x = vectorize(row["features"])
            if sf is None or x is None:
                self._trained_ids.add(row["id"])
                continue
            self.model.update(x, 1.0 if sf else 0.0)
            self._trained_ids.add(row["id"])
            n += 1
        if n:
            self.db.memory_set(self.KEY, self.model.to_dict())
            self.db.memory_set(self.KEY + ":ids", sorted(self._trained_ids)[-5000:])
        return n

    def predict(self, features: Dict[str, Any]) -> float | None:
        x = vectorize(features)
        return self.model.predict(x) if x is not None else None

    def describe(self) -> Dict[str, Any]:
        d = self.model.to_dict()
        return {"samples": d["n"], "loss_ema": round(d["loss_ema"], 4) if d["loss_ema"] is not None else None, "weights": {f: round(w, 3) for f, w in zip(FEATURES, d["w"])}, "bias": round(d["b"], 3)}
