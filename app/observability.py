"""Per-stage timing and counters. Safe to log: no message content or raw identifiers."""

from __future__ import annotations

import logging
import statistics
import time
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager


def setup_logging(level: str) -> None:
    logging.basicConfig(level=level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx logs full URLs at INFO (BlueBubbles puts the password in the query): keep it quiet.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


class Metrics:
    def __init__(self) -> None:
        self.timings: dict[str, list[float]] = defaultdict(list)
        self.counters: dict[str, int] = defaultdict(int)

    def observe(self, stage: str, seconds: float) -> None:
        self.timings[stage].append(seconds)

    def incr(self, name: str, n: int = 1) -> None:
        self.counters[name] += n

    @contextmanager
    def timed(self, stage: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            self.observe(stage, time.perf_counter() - start)

    def summary(self) -> dict:
        stages = {}
        for stage, vals in sorted(self.timings.items()):
            s = sorted(vals)
            stages[stage] = {
                "n": len(s),
                "p50_ms": round(statistics.median(s) * 1000, 2),
                "p95_ms": round(s[min(len(s) - 1, int(0.95 * (len(s) - 1) + 0.5))] * 1000, 2),
            }
        return {"stages": stages, "counters": dict(sorted(self.counters.items()))}
