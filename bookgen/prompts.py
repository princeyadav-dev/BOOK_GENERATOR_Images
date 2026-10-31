"""All prompt templates live here. Tune these, not the pipeline code."""
from __future__ import annotations

import json
import re

DEFAULT_BANNED = [
    "tapestry", "delve", "testament to", "in conclusion", "little did they know",
    "a symphony of", "shivers down", "heart pounded in her chest", "the weight of",
    "unspoken", "in the realm of", "it is important to note", "navigate the complexities",
    "ever-evolving", "game-changer", "unlock the power",
]

FICTION_RULES = """\
- Show emotion through action, body sensation, dialogue and subtext. Never name the
  emotion outright ("she felt sad" is banned).
- Use concrete, specific details: a named street, a smell, an object with a history.
- Vary sentence length. Mix short blunt lines with longer flowing ones. Allow fragments,
  interruptions, and imperfect speech in dialogue.
- Characters want something, hide something, and sometimes contradict themselves.
  Not everyone is articulate or wise.
- Do not end every scene with a lesson or neat moral. End on an image, a line of
  dialogue, or an open question.
- Start the chapter in the middle of something (action, dialogue, sensory detail), not
  with background explanation."""

NONFICTION_RULES = """\
- Write like a person talking to one reader. Use "you" and "I" where it fits the voice.
- Every section needs a concrete story, example, number, or scene, not abstractions.
- Make one clear argument per chapter, then complicate it honestly (a counter-example,
  an exception, a mistake the author made).
- Vary rhythm: short punchy lines between longer explanatory ones.
- Never invent statistics, studies, quotes, or named experts. If you need a fact you
  were not given, write [VERIFY: describe the needed fact] so the author can fill it in.
- No bullet lists or sub-headings inside the prose. Plain flowing paragraphs only.
- Start with a hook (scene, question, surprising claim), not a roadmap of the chapter."""


def system_prompt(cfg: dict) -> str:
    b = cfg["book"]
    banned = DEFAULT_BANNED + list(b.get("banned_phrases", []))
    rules = FICTION_RULES if b.get("mode", "fiction") == "fiction" else NONFICTION_RULES
    return f"""You are an author with a distinct, consistent voice, writing a book for a human reader.
You write the book itself, not a summary of it.

LANGUAGE: {b.get('language', 'English')}. Keep the natural register; if Hinglish, mix Hindi
and English the way real speakers do, in Roman script.
POINT OF VIEW / TENSE: {b.get('pov', 'third person, past tense')}
TONE: {b.get('tone', 'warm, honest')}

CRAFT RULES
{rules}
- No lists, no markdown, no headings inside the prose, no "In conclusion", no recap of
  what you just wrote.
- Scene breaks: a line containing only ***
- Avoid these phrases completely: {', '.join(banned)}

OUTPUT FORMAT (strict). Return only these tags, nothing before or after:
<chapter_title>...</chapter_title>
<chapter_text>
paragraphs separated by a blank line
</chapter_text>
<chapter_summary>max 120 words, plain facts of what happened or was argued</chapter_summary>
<new_facts>bullet list ("- ...") of names, dates, claims, objects established in this chapter</new_facts>"""


JSON_SYSTEM = ("You are a careful book planner. Reply with valid JSON only: no markdown fences, "
               "no commentary.")


def bible_prompt(cfg: dict) -> str:
    b = cfg["book"]
    if b.get("mode", "fiction") == "fiction":
        schema = ('{"premise": str, "themes": [str], "style_guide": str, "setting": str, '
                  '"central_arc": str, "characters": [{"name": str, "role": str, "traits": str, "voice": str}]}')
    else:
        schema = ('{"thesis": str, "reader_promise": str, "style_guide": str, "key_concepts": [str], '
                  '"recurring_stories": [str], "things_to_avoid": [str]}')
    return f"""### TASK: BIBLE
Create the "book bible" that every chapter will be written against.

TOPIC: {b['topic']}
SUMMARY: {b['summary']}
MOTIVE (what the reader should feel / learn / do): {b['motive']}
AUDIENCE: {b.get('audience', 'general readers')}
TONE: {b.get('tone', '')}
POV: {b.get('pov', '')}
AUTHOR'S REAL MATERIAL (experiences, opinions, anecdotes to use):
{b.get('author_notes', '(none given)')}

Return JSON with exactly this shape:
{schema}"""


def outline_prompt(cfg: dict, bible: dict) -> str:
    b = cfg["book"]
    arc = b.get("emotional_arc", "")
    return f"""### TASK: OUTLINE
NUM_CHAPTERS={b['chapters']}
Plan the whole book as exactly {b['chapters']} chapters.

BOOK BIBLE:
{json.dumps(bible, ensure_ascii=False, indent=1)}

MOTIVE: {b['motive']}
EMOTIONAL ARC TO FOLLOW (across the book): {arc or 'build naturally'}
Each chapter ends in a way that pulls the reader into the next one.

Return JSON with exactly this shape:
{{"title": str, "subtitle": str, "chapters": [{{"n": int, "title": str, "purpose": str,
"beats": [str, str, str], "emotion": str}}]}}"""


def chapter_prompt(cfg: dict, bible: dict, outline_ch: dict, total: int,
                   rolling_summary: str, facts: str, prev_tail: str, words: int) -> str:
    b = cfg["book"]
    return f"""### TASK: CHAPTER
CHAPTER_NUMBER={outline_ch['n']}
CHAPTER_TITLE={outline_ch['title']}
TARGET_WORDS={words}

BOOK BIBLE:
{json.dumps(bible, ensure_ascii=False, indent=1)}

AUTHOR'S REAL MATERIAL (weave in naturally where it fits; never quote it verbatim):
{b.get('author_notes', '(none)') or '(none)'}

STORY / ARGUMENT SO FAR:
{rolling_summary or '(this is the first chapter)'}

FACTS ALREADY ESTABLISHED (stay consistent with these):
{facts or '(none yet)'}

LAST ~400 WORDS OF THE PREVIOUS CHAPTER (continue smoothly, do not repeat):
{prev_tail or '(none)'}

NOW WRITE chapter {outline_ch['n']} of {total}: "{outline_ch['title']}".
Purpose: {outline_ch.get('purpose', '')}
Beats to cover, in order:
{chr(10).join('- ' + x for x in outline_ch.get('beats', []))}
Emotional target for this chapter: {outline_ch.get('emotion', '')}
Length: about {words} words. Do not stop early and do not pad."""


def revise_prompt(text: str, instructions: str) -> str:
    return f"""### TASK: REVISE
Rewrite the chapter below according to these instructions. Keep the plot, facts, names and
length (+/- 10%) the same. Keep the same voice. Change only what the instructions require.

INSTRUCTIONS:
{instructions}

Return only:
<chapter_text>
...full revised chapter...
</chapter_text>

<chapter_text>
{text}
</chapter_text>"""


CRITIQUE_INSTRUCTIONS = """\
Find the ~8 most generic, cliched, or over-explained lines (stock phrases, emotions named
outright, tidy moral summaries, repeated sentence openings, lists of three). Rewrite them
with specific, concrete, surprising details. Cut any paragraph that only restates the
previous one. Roughen up dialogue so it sounds like real speech."""


def compress_prompt(text: str, max_chars: int, what: str) -> str:
    return f"""### TASK: COMPRESS
Compress the following {what} to at most {max_chars} characters. Keep every name, number,
date, and plot-critical or argument-critical fact. Plain text, no preamble.
###
{text}"""


def summary_prompt(text: str) -> str:
    return f"""### TASK: SUMMARY
Summarize this chapter in at most 120 words of plain facts (what happened or was argued).
Then list established names/dates/claims as "- ..." bullets after a line "FACTS:".
###
{text}"""


def image_prompt(title: str, summary: str, setting: str) -> str:
    return f"""### TASK: IMAGE_PROMPT
Write ONE image-generation prompt (max 45 words) for the illustration that opens this chapter.
Depict a single concrete moment, place, or object from the chapter (not an abstract idea).
Describe subject, setting, time of day/light, and mood.
Prefer landscapes, rooms, objects, hands, or silhouettes over detailed faces.
No text or lettering, no real people or celebrities, no brand names, logos or copyrighted characters.
Return only the prompt, nothing else.

BOOK SETTING / THEME: {setting}
CHAPTER: {title}
SUMMARY: {summary}"""


# ---------------------------------------------------------------- parsing
def tag(out: str, name: str) -> str | None:
    m = re.search(rf"<{name}>(.*?)</{name}>", out, re.S)
    if m:
        return m.group(1).strip()
    m = re.search(rf"<{name}>(.*)$", out, re.S)  # unterminated tag (truncated output)
    return m.group(1).strip() if m else None


def extract_json(out: str) -> dict:
    out = out.strip()
    out = re.sub(r"^```(?:json)?|```$", "", out, flags=re.M).strip()
    start, end = out.find("{"), out.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object found")
    return json.loads(out[start:end + 1])
