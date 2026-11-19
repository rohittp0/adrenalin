from adrenalin.common.cache import ExtractionCache


def test_cache_roundtrip_and_model_scoping(tmp_path):
    c = ExtractionCache(tmp_path / "c.db")
    try:
        _body(c, tmp_path)
    finally:
        c.close()


def _body(c, tmp_path):
    assert c.get("k", "codex", "m1") is None
    c.put("k", "codex", "m1", {"brand": "Rent80"})
    assert c.get("k", "codex", "m1") == {"brand": "Rent80"}
    assert c.get("k", "codex", "m2") is None
    assert c.get("k", "ocr") is None
    c.put("k", "codex", "m1", {"brand": "X"})
    assert c.get("k", "codex", "m1") == {"brand": "X"}
    # persists across instances
    c2 = ExtractionCache(tmp_path / "c.db")
    try:
        assert c2.get("k", "codex", "m1") == {"brand": "X"}
    finally:
        c2.close()
