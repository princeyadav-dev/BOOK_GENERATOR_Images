# bookgen: topic se poori book (Word + PDF, chapter images ke saath)

Aap sirf `config.yaml` mein topic, summary, motive, tone, chapters aur apni real material (`author_notes`) dete ho.
Tool: **bible -> outline -> chapter-by-chapter API calls (rolling context) -> DOCX + PDF**.

## Setup
```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...        # ya OPENAI_API_KEY (Windows: setx / $env:...)
```
PDF ke liye **LibreOffice** install hona chahiye (`soffice`). Nahi ho to DOCX milega; usse Word mein khol ke Save as PDF karo.

## Chalane ka tarika
```bash
python main.py --backend mock all   # pehle offline dry run (koi API key nahi)
python main.py all                  # asli run
python main.py all --review         # outline par aur har chapter ke baad ruk ke aapko edit karne deta hai
```
Step-by-step:
```bash
python main.py bible                # output/<slug>/bible.json   (edit kar sakte ho)
python main.py outline              # output/<slug>/outline.json (edit kar sakte ho)
python main.py write                # chapters/ch01.md ... (resume ho jata hai)
python main.py images               # har chapter ke liye illustration (images/ch01.jpg ...)
python main.py write --only 3 --redo
python main.py export               # DOCX + PDF dobara banao (edited chapters ke saath)
```

## Kaise kaam karta hai
1. **Bible**: premise/thesis, style guide, characters/concepts. Har chapter call mein jaata hai taaki consistency rahe.
2. **Outline**: har chapter ke beats, purpose, aur emotional target (`emotional_arc` se).
3. **Chapter call** (ek chapter = ek call, ~2000 words). Context mein: bible + author_notes + rolling summary + facts ledger + pichle chapter ke last 400 words.
   Summary/ledger bade ho jayein to purana hissa auto-compress hota hai (`context:` settings).
4. **Auto-fixes**: banned phrases (clichés) mile to targeted rewrite. `revise_pass: true` se har chapter par critique+rewrite pass.
5. **Human edit point**: `output/<slug>/chapters/chNN.md` seedha edit karo. Export aur agle chapters ka context edited text hi use karte hain.
6. **Export**: 6x9 trim, mirrored margins, KDP page-count ke hisaab se gutter, title/copyright/contents, chapter numbering, page numbers (body se 1), scene breaks, justified text, first-line indent.
   DOCX do-pass mein banta hai taaki Contents mein **sahi page numbers** aayein (Word mein right-click -> Update Field se refresh bhi hota hai).

## Quality ke liye (zaroori)
- `author_notes` mein **apni real kahaniyan, jagah, cheezein, opinions** dalo. Isi se book generic nahi lagti.
- Pehle 2 chapters (`--only 1 2`) padho, prompts (`bookgen/prompts.py`) aur tone tune karo, fir baaki chalao.
- Non-fiction mein model facts/statistics invent kar sakta hai. `[VERIFY: ...]` markers search karo aur khud check karo.
- Har chapter ko khud padh ke edit karo: apni voice, apne examples. Heavy human editing hi book ko asli aur behtar banata hai.

## Amazon KDP notes
- **AI disclosure**: KDP upload ke time AI-generated content (text jo AI ne likha, chahe aapne edit kiya ho) declare karna zaroori hai. AI-assisted (aapne likha, AI ne sirf madad ki) alag category hai. Policy KDP ki site par dobara check kar lo.
- KDP ke margin/trim rules page count ke hisaab se badalte hain; upload se pehle KDP Previewer chalao. Gutter `format.margins.gutter` se override hota hai.
- Hindi book ke liye `format.font` ko Devanagari font (Nirmala UI / Noto Serif Devanagari) par set karo aur `language: Hindi`.
- Default font Times New Roman hai kyunki LibreOffice use metric-compatible font se replace karta hai, isse Word aur PDF ka pagination same rehta hai.

## Files
```
main.py              CLI
config.yaml          sab control yahin
bookgen/prompts.py   saare prompts (system, bible, outline, chapter, revise)
bookgen/pipeline.py  chunked generation, rolling context, resume
bookgen/llm.py       Anthropic / OpenAI / mock backend, retries
bookgen/exporter.py  DOCX + PDF formatting
bookgen/images.py    chapter illustrations (providers, fallback, finishing)
tests/test_smoke.py  offline tests
```
Model name `llm.model` mein apne account ke hisaab se badlo. Naye backend ke liye `llm.py` mein ek method add karo.

## Groq setup (.env)
1. `.env.example` ko copy karke `.env` naam do (main.py wale folder mein).
2. Usme `GROQ_API_KEY="..."` aur `GROQ_MODEL=...` daalo. `.env` git mein nahi jaati (.gitignore mein hai).
3. `python main.py all`
Key kabhi chat/GitHub par share mat karo; leak ho to console.groq.com par revoke karke nayi banao.

## Images (free, har chapter ke shuru mein)
`python main.py all` ab chapters ke baad images bhi banata hai. Alag se: `python main.py images`.

Flow: chapter summary -> LLM visual prompt (`images/chNN.prompt.txt`) -> free provider -> Pillow (grayscale, 300 DPI size) -> Word/PDF mein heading ke neeche.

| Setting (`config.yaml` -> `images:`) | Matlab |
|---|---|
| `provider` | `pollinations` (free, key nahi) / `huggingface` (`.env` mein `HF_TOKEN`) / `placeholder` (offline) / `none` |
| `style` | Har image ka common look (isse saari images ek jaisi lagti hain) |
| `grayscale` | `true` = B&W (KDP paperback sasta), `false` = colour |
| `width_in`, `size` | Page par chaudai, aur generate hone wala size |
| `seed` | Same seed + same style = consistent look |

- **Prompt badalna:** `images/chNN.prompt.txt` edit karo, fir `python main.py images --redo-images` (sirf wahi nahi, saari images dobara banengi; jo chapter nahi badalna uska `.jpg` rehne do aur baaki ke prompt file delete/edit karo).
- **Image pasand nahi aayi:** `images/chNN.jpg` delete karo aur `python main.py images` chalao.
- **Service down ho to:** do chapters fail hone par baaki par placeholder art lagta hai aur book phir bhi ban jaati hai. Baad mein `python main.py images` chalao, placeholders asli images se replace ho jaayenge.
- **HF_TOKEN** (huggingface provider ke liye): `.env` mein `HF_TOKEN="hf_..."`, token huggingface.co/settings/tokens se free milta hai.

### KDP aur images: dhyan rakho
- **AI-generated images bhi KDP par declare karne hote hain**, text ki tarah.
- Har free service ke commercial-use ke terms alag hain aur badalte rehte hain. Publish se pehle provider (Pollinations / Hugging Face model) ka license khud check karo.
- AI images mein galtiyan hoti hain (haath, ajeeb cheezein, kabhi text jaisa kuch). Har image dekh ke hi publish karo.
- Free generators ~1024px dete hain, 300 DPI size tak upscale hota hai par detail nahi badhti. 4 inch width par theek rehta hai.
- Colour interior mein KDP print cost kaafi badh jaati hai, isliye default grayscale hai.
