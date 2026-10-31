"""Thin LLM wrapper with retries. Backends: anthropic, openai, mock (offline testing)."""
from __future__ import annotations

import os
import re
import time
from collections import deque


class LLM:
    def __init__(self, cfg: dict):
        llm_cfg = cfg.get("llm", {})
        self.backend = os.getenv("LLM_BACKEND", llm_cfg.get("backend", "groq")).lower()
        self.model = llm_cfg.get("model", "llama-3.1-8b-instant")
        if self.backend == "groq" and os.getenv("GROQ_MODEL"):
            self.model = os.environ["GROQ_MODEL"].strip()      # .env wins over config.yaml
        self.max_output_tokens = int(llm_cfg.get("max_output_tokens", 0) or 0)  # 0 = no cap
        self.reasoning_effort = llm_cfg.get("reasoning_effort", "low")  # gpt-oss only: low|medium|high
        self.temperature = llm_cfg.get("temperature", 0.9)
        self.retries = int(llm_cfg.get("retries", 8))
        self.tpm_limit = int(llm_cfg.get("tpm_limit", 0) or 0)   # 0 = no client-side throttle
        self._window = deque()                                   # (timestamp, est_tokens)
        self._client = None
        self.calls = 0

    # ------------------------------------------------------------------ public
    def complete(self, system: str, user: str, max_tokens: int = 4096,
                 temperature: float | None = None) -> str:
        temp = self.temperature if temperature is None else temperature
        if self.max_output_tokens:
            max_tokens = min(max_tokens, self.max_output_tokens)
        last_err = None
        for attempt in range(self.retries):
            try:
                self._throttle(len(system) + len(user), max_tokens)
                self.calls += 1
                if self.backend == "mock":
                    return _mock(system, user)
                if self.backend == "anthropic":
                    return self._anthropic(system, user, max_tokens, temp)
                if self.backend in ("openai", "groq"):
                    return self._openai(system, user, max_tokens, temp)
                raise ValueError(f"Unknown backend: {self.backend}")
            except (ValueError, ImportError):
                raise
            except Exception as e:  # network / rate limit / overload
                last_err = e
                wait = _retry_after(str(e)) or min(60, 2 ** attempt * 5)
                kind = "rate limit" if "rate limit" in str(e).lower() else type(e).__name__
                print(f"  [llm] {kind}; waiting {wait:.0f}s then retrying ({attempt + 1}/{self.retries})")
                time.sleep(wait)
        raise RuntimeError(f"LLM call failed after {self.retries} attempts: {last_err}")

    def _throttle(self, chars: int, max_tokens: int):
        """Stay under the provider's tokens-per-minute limit (estimate: chars/3 + max_tokens)."""
        if not self.tpm_limit or self.backend == "mock":
            return
        est = chars // 3 + max_tokens
        budget = self.tpm_limit * 0.92
        while True:
            now = time.time()
            while self._window and now - self._window[0][0] > 60:
                self._window.popleft()
            used = sum(t for _, t in self._window)
            if used + est <= budget or not self._window:
                break
            wait = 60 - (now - self._window[0][0]) + 1
            print(f"  [throttle] waiting {wait:.0f}s for the per-minute token budget ...")
            time.sleep(max(wait, 1))
        self._window.append((time.time(), est))

    # --------------------------------------------------------------- backends
    def _anthropic(self, system, user, max_tokens, temp):
        if self._client is None:
            import anthropic  # lazy import
            self._client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
        kwargs = dict(model=self.model, max_tokens=max_tokens, system=system,
                      messages=[{"role": "user", "content": user}])
        if temp is not None:
            kwargs["temperature"] = temp
        # streaming avoids timeouts on long generations
        with self._client.messages.stream(**kwargs) as stream:
            msg = stream.get_final_message()
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")

    def _openai(self, system, user, max_tokens, temp):
        if self._client is None:
            if self.backend == "groq" and not os.getenv("GROQ_API_KEY"):
                raise ValueError("GROQ_API_KEY not set. Put it in the .env file (see .env.example).")
            from openai import OpenAI  # lazy import (Groq is OpenAI-compatible); pip install openai
            if self.backend == "groq":
                key = os.environ["GROQ_API_KEY"]
                self._client = OpenAI(api_key=key.strip(), base_url="https://api.groq.com/openai/v1")
            else:
                self._client = OpenAI()  # reads OPENAI_API_KEY
        kwargs = dict(model=self.model,
                      messages=[{"role": "system", "content": system},
                                {"role": "user", "content": user}],
                      max_completion_tokens=max_tokens)
        if temp is not None:
            kwargs["temperature"] = temp
        if "gpt-oss" in self.model and self.reasoning_effort:
            # reasoning tokens count against max_completion_tokens, so keep effort low for prose
            kwargs["extra_body"] = {"reasoning_effort": self.reasoning_effort}
        resp = self._client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content or ""


def _retry_after(msg: str) -> float | None:
    """Parse Groq's 'Please try again in 20.25s' / '1m2.5s' / '350ms' hint."""
    m = re.search(r"try again in\s+(?:(\d+)m(?!s))?\s*(?:([\d.]+)s)?\s*(?:([\d.]+)ms)?", msg)
    if not m or not any(m.groups()):
        return None
    mins, secs, ms = (float(x) if x else 0.0 for x in m.groups())
    return mins * 60 + secs + ms / 1000 + 2.0   # small safety margin


# ---------------------------------------------------------------------- mock
_LOREM = [
    "The kettle had gone cold an hour ago, but Meera kept her hands around it anyway, as if warmth were something you could borrow back.",
    "Outside, the rain drummed on the tin roof of the neighbour's shed, and somewhere a scooter coughed twice and gave up.",
    "\"You never said you were leaving,\" her brother said, not looking at her. \"You just started packing.\"",
    "She folded the same shirt three times. Her fingers knew the work; her mind was elsewhere, in the corridor of the old house, counting the steps to the kitchen.",
    "Nobody spoke for a while. The clock in the hallway, which had been wrong for years, ticked on with great confidence.",
    "It was not forgiveness, exactly. It was smaller than that, and stranger: a door left unlatched.",
]


def _mock(system: str, user: str) -> str:
    task = re.search(r"### TASK: (\w+)", user)
    task = task.group(1) if task else "CHAPTER"
    if task == "BIBLE":
        return ('{"premise": "Mock premise", "themes": ["memory", "return"], '
                '"style_guide": "Plain, warm, concrete.", "characters": '
                '[{"name": "Meera", "role": "protagonist", "traits": "guarded, observant", "voice": "short sentences"}], '
                '"setting": "A small town in monsoon season", "central_arc": "From avoidance to acceptance"}')
    if task == "OUTLINE":
        n = int(re.search(r"NUM_CHAPTERS=(\d+)", user).group(1))
        chs = ",".join(
            '{"n": %d, "title": "Mock Chapter %d", "purpose": "advance the story", '
            '"beats": ["a quiet scene", "a small conflict", "an image to end on"], '
            '"emotion": "uneasy curiosity"}' % (i, i) for i in range(1, n + 1))
        return '{"title": "The Mock Book", "subtitle": "A Test Edition", "chapters": [%s]}' % chs
    if task == "IMAGE_PROMPT":
        return "A quiet monsoon street at dusk, wet tin roofs, one lit window, warm lamplight, calm mood"
    if task in ("COMPRESS", "SUMMARY"):
        body = user.split("###", 2)[-1]
        return body.strip()[:600]
    if task == "REVISE":
        found = re.findall(r"<chapter_text>(.*?)</chapter_text>", user, re.S)
        return "<chapter_text>\n%s\n</chapter_text>" % (found[-1].strip() if found else "")
    # CHAPTER
    n = int(re.search(r"CHAPTER_NUMBER=(\d+)", user).group(1))
    title = re.search(r"CHAPTER_TITLE=(.*)", user).group(1).strip()
    words = int(re.search(r"TARGET_WORDS=(\d+)", user).group(1))
    paras, count, i = [], 0, n
    while count < words:
        block = " ".join(_LOREM[(i + k) % len(_LOREM)] for k in range(3))
        paras.append(block)
        count += len(block.split())
        i += 1
        if len(paras) == 4:
            paras.append("***")
    text = "\n\n".join(paras)
    return (f"<chapter_title>{title}</chapter_title>\n<chapter_text>\n{text}\n</chapter_text>\n"
            f"<chapter_summary>In chapter {n}, Meera avoids a conversation and a door is left unlatched.</chapter_summary>\n"
            f"<new_facts>\n- Meera has a brother\n- The hallway clock is wrong (ch{n})\n</new_facts>")
