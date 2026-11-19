import json
import subprocess
import threading
import time
from pathlib import Path

import pytest

from adrenalin.common.ocr.codex import CodexError, CodexRunner

FIX = Path(__file__).parent.parent / "fixtures" / "ads_transparency"
SCHEMA = {"type": "object", "properties": {"brand": {"type": "string"}}}


class FakeRun:
    def __init__(self, payload=None, returncode=0, stderr="", delay=0.0):
        self.payload = payload if payload is not None else {"brand": "Rent80"}
        self.returncode, self.stderr, self.delay = returncode, stderr, delay
        self.calls = []
        self.inflight = 0
        self.max_inflight = 0
        self.lock = threading.Lock()

    def __call__(self, argv, **kwargs):
        with self.lock:
            self.calls.append((argv, kwargs))
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
        time.sleep(self.delay)
        if self.returncode == 0:
            Path(argv[argv.index("-o") + 1]).write_text(json.dumps(self.payload))
        with self.lock:
            self.inflight -= 1
        return subprocess.CompletedProcess(argv, self.returncode, "", self.stderr)


def test_codex_invocation_shape(tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(b"x")
    fake = FakeRun()
    CodexRunner(run=fake).extract(img, SCHEMA, "read it")
    argv, kw = fake.calls[0]
    assert argv[0] == "codex"
    assert argv[1:5] == ["exec", "--skip-git-repo-check", "--sandbox", "read-only"]
    for pair in (["-m", "gpt-5.6-luna"], ["-c", "model_reasoning_effort=low"], ["-i", str(img)]):
        i = argv.index(pair[0])
        assert argv[i : i + 2] == pair
    assert "--output-schema" in argv and "-o" in argv
    assert argv[-1] == "read it"
    assert kw["stdin"] is subprocess.DEVNULL
    assert not Path(kw["cwd"]).exists()


def test_codex_returns_parsed_json(tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(b"x")
    r = CodexRunner(run=FakeRun({"brand": "Z"}))
    assert r.extract(img, SCHEMA, "p") == {"brand": "Z"}
    assert r.calls == 1


def test_codex_limit_returns_none_and_counts_skipped(tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(b"x")
    fake = FakeRun()
    r = CodexRunner(limit=1, run=fake)
    assert r.extract(img, SCHEMA, "p") is not None
    assert r.extract(img, SCHEMA, "p") is None
    assert r.skipped == 1 and len(fake.calls) == 1


def test_codex_failure_raises_codex_error(tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(b"x")
    r = CodexRunner(run=FakeRun(returncode=1, stderr="model is not supported"))
    with pytest.raises(CodexError, match="model is not supported"):
        r.extract(img, SCHEMA, "p")


def test_codex_missing_binary_and_timeout(tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(b"x")

    def missing(argv, **kw):
        raise FileNotFoundError("codex")

    def slow(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1)

    with pytest.raises(CodexError):
        CodexRunner(run=missing).extract(img, SCHEMA, "p")
    with pytest.raises(CodexError):
        CodexRunner(run=slow).extract(img, SCHEMA, "p")


def test_codex_concurrency_never_exceeds_limit(tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(b"x")
    fake = FakeRun(delay=0.05)
    r = CodexRunner(run=fake, concurrency=2)
    ts = [threading.Thread(target=r.extract, args=(img, SCHEMA, "p")) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(fake.calls) == 6
    assert fake.max_inflight <= 2


@pytest.mark.live
def test_codex_live_extracts_rent80():
    schema = {
        "type": "object",
        "properties": {"brand": {"type": "string"}},
        "required": ["brand"],
        "additionalProperties": False,
    }
    out = CodexRunner().extract(FIX / "simgad_image.png", schema, "Return the advertiser brand name shown in this ad image.")
    assert out["brand"] == "Rent80"
