"""Run token budget: what a run really spends, and how it should behave as the budget runs down.

Every request resends the whole conversation, but the provider's prompt cache (and llama.cpp's KV prefix cache)
means only the tokens that are NEW since the previous call cost anything. Summing whole prompts (what this budget
used to do) made a run that had read a few files look 10x as expensive as it was, and it stopped productive work.

`RunBudget.add` counts what is actually new:
  * the growth of the prompt since the previous call, minus the previous reply (already counted when it was made);
  * plus the reply itself;
  * or, when the provider reports cached tokens, the prompt minus the cached part;
  * or the full prompt when the start of the conversation was rewritten (old results cleared, history compacted, a
    different lane/model answered): the cache is cold then, so that really costs the whole prompt.

`stage()` turns the used fraction into guidance for the loop: steer, squeeze, finish, then a short grace so the
step in progress is not cut off, then stop.
"""

from typing import Optional

STAGE_OK, STAGE_HALF, STAGE_SQUEEZE, STAGE_FINISH = 0, 1, 2, 3
HALF_AT, SQUEEZE_AT, FINISH_AT = 0.5, 0.7, 0.9
GRACE_FRACTION = 0.10       # the in-progress item may use this much more than the limit...
GRACE_STEPS = 3             # ...for at most this many steps


class RunBudget:
    def __init__(self, limit: int = 0):
        self.limit = max(0, int(limit or 0))
        self.used = 0
        self._prev_prompt: Optional[int] = None
        self._prev_completion = 0
        self.grace_steps = 0

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    @property
    def fraction(self) -> float:
        return (self.used / self.limit) if self.limit else 0.0

    def add(self, prompt_tokens, completion_tokens, cached_tokens: int = 0, prefix_rewritten: bool = False) -> int:
        """Record one model call; returns the tokens it added to the budget."""
        p = max(0, int(prompt_tokens or 0))
        c = max(0, int(completion_tokens or 0))
        cached = max(0, int(cached_tokens or 0))
        if cached and not prefix_rewritten:
            new_prompt = max(0, p - cached)
        elif prefix_rewritten or self._prev_prompt is None:
            new_prompt = p
        else:
            new_prompt = max(0, p - self._prev_prompt - self._prev_completion)
        self._prev_prompt, self._prev_completion = p, c
        added = new_prompt + c
        self.used += added
        return added

    def stage(self) -> int:
        f = self.fraction
        if not self.limit:
            return STAGE_OK
        if f >= FINISH_AT:
            return STAGE_FINISH
        if f >= SQUEEZE_AT:
            return STAGE_SQUEEZE
        if f >= HALF_AT:
            return STAGE_HALF
        return STAGE_OK

    def exhausted(self) -> bool:
        """True when the run must stop now. Past the limit it may finish the step in progress: up to GRACE_STEPS more
        steps and GRACE_FRACTION more tokens (call `note_step` once per step taken past the limit)."""
        if not self.limit or self.used < self.limit:
            return False
        return self.used >= self.limit * (1 + GRACE_FRACTION) or self.grace_steps >= GRACE_STEPS

    def note_step(self) -> None:
        if self.limit and self.used >= self.limit:
            self.grace_steps += 1


MESSAGES = {
    STAGE_HALF: ("[budget] Half of this run's token budget is used. Prioritise the remaining plan items by value, do not "
                 "explore broadly, and do not re-read files you have already seen."),
    STAGE_SQUEEZE: ("[budget] Most of this run's token budget is used. Finish what is left in as few steps as possible and "
                    "give the final answer."),
    STAGE_FINISH: ("[budget] The token budget is nearly gone. No more exploring: only write, edit, run or answer now. "
                   "Finish the item you are on, then give the final answer listing what is done and the exact next step."),
}
