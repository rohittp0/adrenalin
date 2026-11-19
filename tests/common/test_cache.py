from adrenalin.common.cache import ExtractionCache


def test_cache_roundtrip_and_model_scoping(tmp_path):
    c = ExtractionCache(tmp_path / "c.db")
    assert c.get("k", "codex", "m1") is None
    c.put("k", "codex", "m1", {"brand": "Rent80"})
    assert c.get("k", "codex", "m1") == {"brand": "Rent80"}
    assert c.get("k", "codex", "m2") is None
    assert c.get("k", "ocr") is None
    c.put("k", "codex", "m1", {"brand": "X"})
    assert c.get("k", "codex", "m1") == {"brand": "X"}
    # persists across instances
    assert ExtractionCache(tmp_path / "c.db").get("k", "codex", "m1") == {"brand": "X"}
