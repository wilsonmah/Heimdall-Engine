"""Execution trace: every pipeline stage appends timestamped events."""
import sys
import time


class Trace:
    def __init__(self, echo=False):
        self.t0 = time.perf_counter()
        self.events = []
        self.echo = echo

    def log(self, stage, level, msg):
        ms = (time.perf_counter() - self.t0) * 1000
        self.events.append({"tMs": round(ms, 1), "stage": stage, "level": level, "msg": msg})
        if self.echo:
            print(f"[{ms:7.1f}ms] {level:<5} {stage:<10} {msg}", file=sys.stderr)

    def errors(self):
        return [e["msg"] for e in self.events if e["level"] == "ERROR"]
