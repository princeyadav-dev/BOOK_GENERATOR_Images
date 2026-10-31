#!/usr/bin/env python3
"""bookgen CLI.

  python main.py all                 # bible -> outline -> chapters -> DOCX + PDF
  python main.py bible|outline|write|images|export
  python main.py write --review      # pause after each chapter so you can edit it
  python main.py write --only 3 5 --redo
  python main.py --backend mock all  # offline dry run, no API key needed
"""
import argparse
import os
import sys
from pathlib import Path

import yaml

from bookgen.exporter import export_book
from bookgen.images import generate_images
from bookgen.llm import LLM
from bookgen.pipeline import Project


def load_dotenv(path=".env"):
    """Minimal .env reader: KEY=value or KEY="value"; real environment variables win."""
    f = Path(path)
    if not f.exists():
        return
    for line in f.read_text("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main():
    ap = argparse.ArgumentParser(description="Topic -> formatted book (DOCX + PDF)")
    ap.add_argument("command", choices=["bible", "outline", "write", "images", "export", "all"])
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--backend", help="override llm.backend (anthropic|openai|mock)")
    ap.add_argument("--model", help="override llm.model")
    ap.add_argument("--review", action="store_true", help="pause after each chapter for human editing")
    ap.add_argument("--only", type=int, nargs="*", help="write only these chapter numbers")
    ap.add_argument("--redo", action="store_true", help="regenerate chapters that already exist")
    ap.add_argument("--redo-images", action="store_true", help="regenerate chapter images (keeps edited prompt files)")
    ap.add_argument("--force", action="store_true", help="regenerate bible/outline even if present")
    a = ap.parse_args()

    load_dotenv()
    cfg = yaml.safe_load(Path(a.config).read_text("utf-8"))
    if a.backend: cfg.setdefault("llm", {})["backend"] = a.backend
    if a.model: cfg.setdefault("llm", {})["model"] = a.model

    llm = LLM(cfg)
    proj = Project(cfg, llm)

    if a.command in ("bible", "all"):
        proj.make_bible(force=a.force)
    if a.command in ("outline", "all"):
        proj.make_outline(force=a.force)
        if a.command == "all" and a.review:
            input(f"Review/edit {proj.outline_path} and {proj.bible_path}, then press Enter... ")
    if a.command in ("write", "all"):
        proj.write_all(only=a.only, review=a.review, redo=a.redo)
    if a.command in ("images", "all"):
        generate_images(proj, redo=a.redo_images)
    if a.command in ("export", "all"):
        outline, chapters = proj.load_for_export()
        if not chapters:
            sys.exit("No chapters found. Run `python main.py write` first.")
        res = export_book(cfg, outline, chapters, proj.dir, images=proj.image_map())
        print(f"\nDOCX: {res['docx']}")
        print(f"PDF : {res['pdf'] or '(not created)'}")
    print(f"\nLLM calls this run: {llm.calls}")


if __name__ == "__main__":
    main()
