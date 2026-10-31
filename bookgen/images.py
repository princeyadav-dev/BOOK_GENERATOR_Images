"""Chapter illustrations: LLM writes an image prompt -> free provider renders it -> Pillow finishes it.

Providers (images.provider):
  pollinations  free, no key
  huggingface   free token (HF_TOKEN in .env), default model FLUX.1-schnell
  placeholder   offline decorative art (also the automatic fallback when a download fails)
  none          no images
"""
from __future__ import annotations

import io
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageOps

DEFAULTS = {
    "enabled": True,
    "provider": "pollinations",
    "style": ("soft watercolor illustration, muted warm colors, gentle light, "
              "no text, no letters, no watermark, no logos"),
    "size": "1024x768",             # generated size (px)
    "width_in": 4.0,                # printed width on the page (inches)
    "target_dpi": 300,              # images are upscaled to this DPI at printed width if smaller
    "grayscale": True,              # True = black & white interior (cheaper KDP paperback printing)
    "use_llm_prompts": True,        # LLM turns each chapter summary into a visual prompt
    "seed": 7,                      # same seed + same style = more consistent look across chapters
    "pollinations_model": "flux",
    "hf_model": "black-forest-labs/FLUX.1-schnell",
    "hf_base_url": "https://router.huggingface.co/hf-inference/models",
    "min_seconds_between": 6,       # be polite to free services
    "retries": 3,
    "fail_soft": True,              # on failure use placeholder art instead of stopping
}


def img_cfg(cfg: dict) -> dict:
    merged = dict(DEFAULTS)
    merged.update(cfg.get("images") or {})
    return merged


# ------------------------------------------------------------------ providers
def _http(url: str, data: bytes | None = None, headers: dict | None = None, timeout: int = 180) -> bytes:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "bookgen/1.0", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_pollinations(prompt: str, w: int, h: int, ic: dict) -> bytes:
    q = urllib.parse.quote(prompt, safe="")
    url = (f"https://image.pollinations.ai/prompt/{q}?width={w}&height={h}"
           f"&seed={ic['seed']}&model={ic['pollinations_model']}&nologo=true")
    return _http(url)


def fetch_huggingface(prompt: str, w: int, h: int, ic: dict) -> bytes:
    token = os.getenv("HF_TOKEN")
    if not token:
        raise ValueError("HF_TOKEN not set. Add it to the .env file (free token from huggingface.co/settings/tokens).")
    model = os.getenv("HF_IMAGE_MODEL", ic["hf_model"])
    url = ic["hf_base_url"].rstrip("/") + "/" + model
    body = json.dumps({"inputs": prompt, "parameters": {"width": w, "height": h, "seed": ic["seed"]}}).encode()
    return _http(url, data=body, headers={"Authorization": f"Bearer {token.strip()}",
                                          "Content-Type": "application/json", "Accept": "image/png"})


def make_placeholder(w: int, h: int, n: int) -> bytes:
    """Offline decorative landscape (gradient sky, sun, layered hills). Never fails."""
    rnd = random.Random(n)
    top = tuple(rnd.randint(170, 235) for _ in range(3))
    bot = tuple(min(255, c + rnd.randint(10, 30)) for c in top)
    im = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(im)
    for y in range(h):
        t = y / h
        d.line([(0, y), (w, y)], fill=tuple(int(top[i] * (1 - t) + bot[i] * t) for i in range(3)))
    sx, sy, r = rnd.randint(w // 4, 3 * w // 4), rnd.randint(h // 6, h // 3), h // 7
    d.ellipse([sx - r, sy - r, sx + r, sy + r], fill=(250, 245, 225))
    for layer in range(3):
        base = int(h * (0.55 + 0.12 * layer))
        shade = 150 - layer * 35
        pts = [(0, h)]
        for x in list(range(0, w, max(8, w // 40))) + [w]:
            pts.append((x, base - int(h * 0.08 * (0.5 + 0.5 * __import__("math").sin(x / (60 + 25 * layer) + rnd.random())))))
        pts.append((w, h))
        d.polygon(pts, fill=(shade, shade - 5, shade - 12))
    im = im.filter(ImageFilter.GaussianBlur(1.2))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


# ------------------------------------------------------------------- finishing
def finalize(raw: bytes, path: Path, ic: dict):
    im = Image.open(io.BytesIO(raw))
    im.load()
    im = im.convert("RGB")
    target_w = int(float(ic["width_in"]) * int(ic["target_dpi"]))
    if im.width < target_w:                       # meet print DPI (does not add detail)
        im = im.resize((target_w, round(im.height * target_w / im.width)), Image.LANCZOS)
    if ic["grayscale"]:
        im = ImageOps.autocontrast(ImageOps.grayscale(im), cutoff=1)
    im.save(path, "JPEG", quality=92, dpi=(int(ic["target_dpi"]),) * 2)


# ---------------------------------------------------------------------- prompts
def _clean(s: str) -> str:
    s = " ".join((s or "").replace('"', "").split())
    return s[:420]


def make_prompt(proj, n: int, title: str, ic: dict) -> str:
    from . import prompts
    meta = json.loads(proj.ch_meta(n).read_text("utf-8")) if proj.ch_meta(n).exists() else {}
    summary = meta.get("summary", "")
    bible = proj.read_bible()
    setting = bible.get("setting") or bible.get("thesis") or ""
    if ic["use_llm_prompts"]:
        try:
            raw = proj.llm.complete("You write concise, concrete image-generation prompts.",
                                    prompts.image_prompt(title, summary, str(setting)),
                                    max_tokens=700, temperature=0.8)
            p = _clean(raw)
            if len(p) > 15:
                return p
        except Exception as e:
            print(f"  [warn] LLM image prompt failed ({e}); using summary instead")
    return _clean(f"{title}. {summary or setting}")


# --------------------------------------------------------------------- the loop
def _render(prompt: str, n: int, w: int, h: int, ic: dict) -> bytes:
    provider = ic["provider"]
    if provider == "placeholder":
        return make_placeholder(w, h, n)
    fetch = {"pollinations": fetch_pollinations, "huggingface": fetch_huggingface}.get(provider)
    if fetch is None:
        raise ValueError(f"Unknown images.provider: {provider}")
    last = None
    for attempt in range(int(ic["retries"])):
        try:
            raw = fetch(prompt, w, h, ic)
            Image.open(io.BytesIO(raw)).verify()      # make sure it's really an image, not an error page
            return raw
        except ValueError:
            raise
        except Exception as e:
            last = e
            wait = 8 * (attempt + 1)
            print(f"  [img] {type(e).__name__}: {str(e)[:120]}; retry in {wait}s ({attempt + 1}/{ic['retries']})")
            time.sleep(wait)
    raise RuntimeError(f"image download failed: {last}")


def generate_images(proj, redo: bool = False) -> dict[int, Path]:
    ic = img_cfg(proj.cfg)
    if not ic["enabled"] or ic["provider"] == "none":
        print("images disabled; skipping")
        return {}
    out = proj.dir / "images"
    out.mkdir(parents=True, exist_ok=True)
    w, h = (int(x) for x in str(ic["size"]).lower().split("x"))
    done = {}
    failures = 0                  # circuit breaker: free services go down; don't waste minutes per chapter
    offline = False
    for n in proj.done_chapters():
        img_path = out / f"ch{n:02d}.jpg"
        pfile = out / f"ch{n:02d}.prompt.txt"
        marker = out / f"ch{n:02d}.placeholder"
        is_stub = marker.exists() and ic["provider"] != "placeholder"   # earlier fallback: retry for real
        if img_path.exists() and not redo and not is_stub:
            done[n] = img_path
            continue
        title, _ = proj.read_chapter(n)
        if pfile.exists():
            prompt = pfile.read_text("utf-8").strip()           # your edited prompt wins
        else:
            prompt = make_prompt(proj, n, title, ic)
            pfile.write_text(prompt + "\n", "utf-8")
        full = f"{prompt}. {ic['style']}"
        print(f"Image for chapter {n}: {prompt[:90]}...")
        used_stub = False
        if offline:
            raw, network, used_stub = make_placeholder(w, h, n), False, True
        else:
            try:
                raw = _render(full, n, w, h, ic)
                network = ic["provider"] != "placeholder"
                failures = 0
            except Exception as e:
                if not ic["fail_soft"] or (isinstance(e, ValueError) and "provider" in str(e)):
                    raise
                failures += 1
                print(f"  [warn] {e}; using placeholder art for chapter {n}")
                raw, network, used_stub = make_placeholder(w, h, n), False, True
                if failures >= 2:
                    offline = True
                    print("  [warn] image service seems down; using placeholders for the remaining chapters. "
                          "Run `python main.py images` later to replace them with real images.")
        finalize(raw, img_path, ic)
        if used_stub and ic["provider"] != "placeholder":
            marker.write_text("placeholder", "utf-8")
        elif marker.exists():
            marker.unlink()
        done[n] = img_path
        if network:
            time.sleep(float(ic["min_seconds_between"]))
    stubs = sum(1 for n in done if (out / f"ch{n:02d}.placeholder").exists())
    print(f"  {len(done)} images in {out}" + (f" ({stubs} are placeholders)" if stubs else ""))
    return done
