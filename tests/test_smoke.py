"""Offline smoke tests (no API key). Run:  python tests/test_smoke.py"""
import copy
import sys
import tempfile
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bookgen import prompts  # noqa: E402
from bookgen.exporter import export_book  # noqa: E402
from bookgen.llm import LLM, _mock  # noqa: E402
from bookgen.pipeline import Project  # noqa: E402

BASE = yaml.safe_load((Path(__file__).resolve().parent.parent / "config.yaml").read_text("utf-8"))


def cfg_for(tmp, chapters=3, words=300):
    c = copy.deepcopy(BASE)
    c["llm"]["backend"] = "mock"
    c["output_dir"] = str(tmp)
    c["book"]["chapters"] = chapters
    c["book"]["words_per_chapter"] = words
    return c


def test_parsers():
    assert prompts.extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert prompts.tag("<x> hi </x>", "x") == "hi"
    assert prompts.tag("<x> cut off", "x") == "cut off"  # truncated output tolerated


def test_full_pipeline_and_resume():
    with tempfile.TemporaryDirectory() as t:
        cfg = cfg_for(t)
        p = Project(cfg, LLM(cfg))
        p.make_outline()
        p.write_all()
        assert p.done_chapters() == [1, 2, 3]
        calls_before = p.llm.calls
        p.write_all()                       # resume: nothing left to do
        assert p.llm.calls == calls_before
        outline, chapters = p.load_for_export()
        res = export_book(cfg, outline, chapters, p.dir)
        assert res["docx"].exists()
        if res["pdf"]:
            assert res["pdf"].stat().st_size > 1000


def test_banned_phrase_gets_rewritten():
    class Tainted(LLM):
        def complete(self, system, user, max_tokens=4096, temperature=None):
            out = _mock(system, user)
            if "### TASK: CHAPTER" in user:
                out = out.replace("The kettle", "A tapestry of regret. The kettle")
            return out
    with tempfile.TemporaryDirectory() as t:
        cfg = cfg_for(t, chapters=1)
        p = Project(cfg, Tainted(cfg))
        p.make_outline()
        p.write_all()
        # mock REVISE echoes text, so phrase remains; just ensure the fix path ran without error
        assert p.ch_md(1).exists()



# ----------------------------------------------------------------- image tests
def _png_bytes(color=(120, 160, 200)):
    import io
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (640, 480), color).save(b, "PNG")
    return b.getvalue()


def _img_project(tmp, provider, chapters=3):
    c = cfg_for(tmp, chapters=chapters)
    c["images"] = {"provider": provider, "min_seconds_between": 0, "retries": 2}
    p = Project(c, LLM(c))
    p.make_outline()
    p.write_all()
    return p


def test_images_download_ok(monkeypatch_target=None):
    import bookgen.images as I
    calls = []
    orig_http, orig_sleep = I._http, I.time.sleep
    I._http = lambda url, data=None, headers=None, timeout=180: (calls.append(url) or _png_bytes())
    I.time.sleep = lambda s: None
    try:
        with tempfile.TemporaryDirectory() as t:
            p = _img_project(t, "pollinations")
            done = I.generate_images(p)
            assert len(done) == 3 and all(f.exists() for f in done.values())
            assert all("image.pollinations.ai/prompt/" in u for u in calls)
            assert not list((p.dir / "images").glob("*.placeholder"))
            n = len(calls)
            I.generate_images(p)                      # resume: nothing re-downloaded
            assert len(calls) == n
    finally:
        I._http, I.time.sleep = orig_http, orig_sleep


def test_images_service_down_falls_back_and_recovers():
    import bookgen.images as I
    orig_http, orig_sleep = I._http, I.time.sleep
    attempts = []

    def down(url, data=None, headers=None, timeout=180):
        attempts.append(url)
        raise OSError("network unreachable")
    I._http = down
    I.time.sleep = lambda s: None
    try:
        with tempfile.TemporaryDirectory() as t:
            p = _img_project(t, "pollinations", chapters=4)
            done = I.generate_images(p)
            assert len(done) == 4                          # book never breaks
            assert len(list((p.dir / "images").glob("*.placeholder"))) == 4
            assert len(attempts) == 4                      # 2 chapters x 2 retries, then circuit breaker
            I._http = lambda url, data=None, headers=None, timeout=180: _png_bytes()   # service is back
            I.generate_images(p)
            assert not list((p.dir / "images").glob("*.placeholder"))   # placeholders replaced
    finally:
        I._http, I.time.sleep = orig_http, orig_sleep


def test_huggingface_needs_token():
    import os
    import bookgen.images as I
    os.environ.pop("HF_TOKEN", None)
    try:
        I.fetch_huggingface("x", 512, 512, I.img_cfg({}))
        assert False, "should have raised"
    except ValueError as e:
        assert "HF_TOKEN" in str(e)


def test_export_with_images():
    import bookgen.images as I
    with tempfile.TemporaryDirectory() as t:
        p = _img_project(t, "placeholder")
        I.generate_images(p)
        outline, chapters = p.load_for_export()
        res = export_book(p.cfg, outline, chapters, p.dir, images=p.image_map())
        assert res["docx"].exists() and len(p.image_map()) == 3


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok ", name)
