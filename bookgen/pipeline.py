"""Pipeline: bible -> outline -> chunked chapter writing (with resume + human review)."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from . import prompts
from .llm import LLM


def slugify(s: str) -> str:
    s = re.sub(r"[^\w\s-]", "", s.lower(), flags=re.U).strip()
    return re.sub(r"[-\s]+", "-", s)[:50] or "book"


def count_words(text: str) -> int:
    return len(text.split())


class Project:
    def __init__(self, cfg: dict, llm: LLM):
        self.cfg, self.llm = cfg, llm
        out = cfg.get("output_dir") or f"output/{slugify(cfg['book']['topic'])}"
        self.dir = Path(out)
        self.ch_dir = self.dir / "chapters"
        self.ch_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.dir / "state.json"
        self.state = json.loads(self.state_path.read_text("utf-8")) if self.state_path.exists() else {"cache": {}}

    # ------------------------------------------------------------- helpers
    def _save_state(self):
        self.state_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), "utf-8")

    def _json_call(self, user: str, max_tokens=4000, tries=3) -> dict:
        for i in range(tries):
            raw = self.llm.complete(prompts.JSON_SYSTEM, user, max_tokens=max_tokens, temperature=0.7)
            try:
                return prompts.extract_json(raw)
            except Exception as e:
                print(f"  [warn] bad JSON ({e}); retry {i + 1}/{tries}")
                user += "\n\nIMPORTANT: your last reply was not valid JSON. Return valid JSON only."
        raise RuntimeError("Could not get valid JSON from the model")

    def _max_tokens(self, words: int) -> int:
        g = self.cfg.get("generation", {})
        if g.get("max_tokens"):
            return int(g["max_tokens"])
        lang = self.cfg["book"].get("language", "English").lower()
        per_word = 6.0 if ("hindi" in lang and "hinglish" not in lang) else 2.4
        return min(int(words * per_word) + 600, 16000)

    # --------------------------------------------------------- bible/outline
    @property
    def bible_path(self): return self.dir / "bible.json"
    @property
    def outline_path(self): return self.dir / "outline.json"

    def make_bible(self, force=False) -> dict:
        if self.bible_path.exists() and not force:
            print("bible.json exists (edit it freely); skipping generation")
            return json.loads(self.bible_path.read_text("utf-8"))
        print("Generating book bible ...")
        bible = self._json_call(prompts.bible_prompt(self.cfg))
        self.bible_path.write_text(json.dumps(bible, ensure_ascii=False, indent=2), "utf-8")
        print(f"  saved {self.bible_path}")
        return bible

    def make_outline(self, force=False) -> dict:
        if self.outline_path.exists() and not force:
            print("outline.json exists (edit it freely); skipping generation")
            return json.loads(self.outline_path.read_text("utf-8"))
        bible = self.make_bible()
        print("Generating outline ...")
        outline = self._json_call(prompts.outline_prompt(self.cfg, bible), max_tokens=6000)
        chs = outline.get("chapters", [])
        for i, c in enumerate(chs, 1):
            c["n"] = i
        outline["chapters"] = chs
        self.outline_path.write_text(json.dumps(outline, ensure_ascii=False, indent=2), "utf-8")
        print(f"  saved {self.outline_path}  ({len(chs)} chapters)")
        return outline

    # ------------------------------------------------------------ chapter io
    def ch_md(self, n): return self.ch_dir / f"ch{n:02d}.md"
    def ch_meta(self, n): return self.ch_dir / f"ch{n:02d}.meta.json"

    def read_chapter(self, n: int) -> tuple[str, str]:
        raw = self.ch_md(n).read_text("utf-8")
        lines = raw.split("\n", 1)
        title = lines[0].lstrip("# ").strip()
        text = lines[1].strip() if len(lines) > 1 else ""
        return title, text

    def _write_chapter_files(self, n, title, text, summary, facts):
        self.ch_md(n).write_text(f"# {title}\n\n{text}\n", "utf-8")
        self.ch_meta(n).write_text(json.dumps({"summary": summary, "facts": facts},
                                              ensure_ascii=False, indent=1), "utf-8")

    def done_chapters(self) -> list[int]:
        return sorted(int(p.stem[2:]) for p in self.ch_dir.glob("ch??.md"))

    # ----------------------------------------------------- rolling context
    def _cached_compress(self, text: str, max_chars: int, what: str) -> str:
        if len(text) <= max_chars:
            return text
        key = hashlib.sha1((what + text).encode("utf-8")).hexdigest()
        cache = self.state.setdefault("cache", {})
        if key not in cache:
            print(f"  compressing {what} ({len(text)} chars -> <= {max_chars}) ...")
            cache[key] = self.llm.complete("You compress text faithfully.",
                                           prompts.compress_prompt(text, max_chars, what),
                                           max_tokens=3000, temperature=0.2).strip()
            self._save_state()
        return cache[key]

    def rolling_summary(self, upto: int) -> str:
        c = self.cfg.get("context", {})
        items = []
        for i in range(1, upto):
            if self.ch_meta(i).exists():
                m = json.loads(self.ch_meta(i).read_text("utf-8"))
                title, _ = self.read_chapter(i)
                items.append(f"Ch{i} ({title}): {m['summary']}")
        recent = items[-3:]
        older = "\n".join(items[:-3])
        older = self._cached_compress(older, int(c.get("max_summary_chars", 5000)), "story summary") if older else ""
        return (older + "\n" + "\n".join(recent)).strip()

    def facts_ledger(self, upto: int) -> str:
        c = self.cfg.get("context", {})
        seen, lines = set(), []
        for i in range(1, upto):
            if self.ch_meta(i).exists():
                for ln in json.loads(self.ch_meta(i).read_text("utf-8")).get("facts", "").splitlines():
                    ln = ln.strip()
                    if ln and ln.lower() not in seen:
                        seen.add(ln.lower()); lines.append(ln)
        return self._cached_compress("\n".join(lines), int(c.get("max_facts_chars", 3500)), "facts list")

    def prev_tail(self, n: int) -> str:
        if n > 1 and self.ch_md(n - 1).exists():
            _, text = self.read_chapter(n - 1)
            return " ".join(text.split()[-int(self.cfg.get("context", {}).get("tail_words", 400)):])
        return ""

    # ------------------------------------------------------- chapter writing
    def _banned_hits(self, text: str) -> list[str]:
        banned = prompts.DEFAULT_BANNED + list(self.cfg["book"].get("banned_phrases", []))
        low = text.lower()
        return sorted({p for p in banned if p.lower() in low})

    def _revise(self, text: str, instructions: str, words: int) -> str:
        raw = self.llm.complete(prompts.system_prompt(self.cfg),
                                prompts.revise_prompt(text, instructions),
                                max_tokens=self._max_tokens(words), temperature=0.8)
        new = prompts.tag(raw, "chapter_text")
        # guard against truncated / collapsed rewrites
        if new and count_words(new) >= 0.7 * count_words(text):
            return new
        print("  [warn] revision looked truncated; keeping previous version")
        return text

    def write_chapter(self, ch: dict, total: int) -> dict:
        n = ch["n"]
        g = self.cfg.get("generation", {})
        words = int(ch.get("words") or self.cfg["book"].get("words_per_chapter", 2000))
        user = prompts.chapter_prompt(self.cfg, self.read_bible(), ch, total,
                                      self.rolling_summary(n), self.facts_ledger(n),
                                      self.prev_tail(n), words)
        print(f"Writing chapter {n}/{total}: {ch['title']} (~{words} words) ...")
        raw = self.llm.complete(prompts.system_prompt(self.cfg), user,
                                max_tokens=self._max_tokens(words),
                                temperature=g.get("temperature"))
        text = prompts.tag(raw, "chapter_text") or raw.strip()
        title = prompts.tag(raw, "chapter_title") or ch["title"]
        summary = prompts.tag(raw, "chapter_summary")
        facts = prompts.tag(raw, "new_facts") or ""
        if count_words(text) < 0.6 * words:
            print(f"  [warn] short chapter ({count_words(text)} words vs ~{words}); consider re-running")

        hits = self._banned_hits(text)
        if hits:
            print(f"  fixing banned phrases: {hits}")
            text = self._revise(text, "Rewrite every sentence containing these phrases so they "
                                      "no longer appear: " + ", ".join(hits), words)
        if g.get("revise_pass", False):
            print("  running critique/rewrite pass ...")
            text = self._revise(text, prompts.CRITIQUE_INSTRUCTIONS, words)

        if not summary:
            s = self.llm.complete("You summarize faithfully.", prompts.summary_prompt(text),
                                  max_tokens=800, temperature=0.2)
            summary, _, f = s.partition("FACTS:")
            summary, facts = summary.strip(), f.strip()
        self._write_chapter_files(n, title, text, summary, facts)
        return {"n": n, "title": title, "words": count_words(text)}

    def read_bible(self) -> dict:
        return json.loads(self.bible_path.read_text("utf-8"))

    def write_all(self, only: list[int] | None = None, review=False, redo=False):
        outline = self.make_outline()
        chs = outline["chapters"]
        total = len(chs)
        for ch in chs:
            n = ch["n"]
            if only and n not in only:
                continue
            if self.ch_md(n).exists() and not redo:
                print(f"chapter {n} exists; skipping (use --redo to regenerate)")
                continue
            info = self.write_chapter(ch, total)
            print(f"  done: {info['words']} words -> {self.ch_md(n)}")
            if review:
                input(f"  Review/edit {self.ch_md(n)} now (edits are used for the next chapters), "
                      f"then press Enter to continue... ")
        return total

    def image_map(self) -> dict:
        d = self.dir / "images"
        return {n: d / f"ch{n:02d}.jpg" for n in self.done_chapters() if (d / f"ch{n:02d}.jpg").exists()}

    def load_for_export(self):
        outline = json.loads(self.outline_path.read_text("utf-8"))
        chapters = []
        for n in self.done_chapters():
            title, text = self.read_chapter(n)
            chapters.append((n, title, text))
        return outline, chapters
