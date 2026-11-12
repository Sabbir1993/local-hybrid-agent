"""The reply's real speeds, from llama-server's own `timings` (one object per model call in the reply).

The badge used to divide generated tokens by the whole request time, which also counts reading the (large) system prompt,
knowledge-base and web lookups and tool calls, so a model generating at 25 t/s could show 10 t/s. These are the numbers
llama-server measured: generation (predicted_*), prompt reading (prompt_*) and, with an MTP / draft model, how many
drafted tokens were accepted. Cloud models send no timings; callers then keep their end-to-end figure."""


class SpeedTally:
    def __init__(self):
        self.gen_n = self.gen_ms = 0.0
        self.prompt_n = self.prompt_ms = 0.0
        self.draft_n = self.draft_ok = 0
        # the first model call of the reply: how much of the prompt the server already had cached from the previous
        # turn (cache_n) against how much it had to read again (prompt_n). This is the stable-prefix measure.
        self.first_cached = self.first_read = None

    def add(self, res) -> None:
        t = (res or {}).get("timings") if isinstance(res, dict) else None
        if not isinstance(t, dict):
            return
        try:
            if self.first_cached is None and ("cache_n" in t or "prompt_n" in t):
                self.first_cached, self.first_read = int(t.get("cache_n") or 0), int(t.get("prompt_n") or 0)
            pn, pms = float(t.get("predicted_n") or 0), float(t.get("predicted_ms") or 0)
            if pn > 0 and pms > 0:
                self.gen_n += pn
                self.gen_ms += pms
            qn, qms = float(t.get("prompt_n") or 0), float(t.get("prompt_ms") or 0)
            if qn > 0 and qms > 0:
                self.prompt_n += qn
                self.prompt_ms += qms
            dn = int(t.get("draft_n") or 0)
            if dn > 0:
                self.draft_n += dn
                self.draft_ok += int(t.get("draft_n_accepted") or t.get("n_accepted") or 0)
        except (TypeError, ValueError):
            pass

    def gen_tps(self):
        return round(1000.0 * self.gen_n / self.gen_ms, 1) if self.gen_ms > 0 else None

    def prompt_tps(self):
        return round(1000.0 * self.prompt_n / self.prompt_ms, 1) if self.prompt_ms > 0 else None

    def draft_accept(self):
        return round(self.draft_ok / self.draft_n, 2) if self.draft_n > 0 else None

    def as_event(self) -> dict:
        """Fields for the `done` event; empty when llama-server reported nothing (cloud model)."""
        out = {"gen_tps": self.gen_tps(), "prompt_tps": self.prompt_tps(), "draft_accept": self.draft_accept(),
               "cache_tokens": self.first_cached, "read_tokens": self.first_read}
        return {k: v for k, v in out.items() if v is not None}
