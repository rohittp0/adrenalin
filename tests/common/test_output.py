import json

import pytest

from adrenalin.common.output import EntityCollision, entity_dir, slugify, write_csv_atomic, cache_dir, write_bytes_atomic, write_json_atomic


def test_slugify():
    assert slugify("Lascade LLC") == "lascade-llc"
    assert slugify("  Café & Co. ") == "cafe-co"


def test_entity_dir_creates_and_reuses(tmp_path):
    d = entity_dir(tmp_path, "ads-transparency", "AR1", "Lascade LLC")
    assert d == tmp_path / "ads-transparency" / "lascade-llc"
    assert json.loads((d / "entity.json").read_text()) == {"id": "AR1", "name": "Lascade LLC"}
    assert entity_dir(tmp_path, "ads-transparency", "AR1", "Lascade LLC") == d


def test_entity_dir_same_slug_different_id_raises(tmp_path):
    entity_dir(tmp_path, "t", "AR1", "Lascade LLC")
    with pytest.raises(EntityCollision, match="AR1"):
        entity_dir(tmp_path, "t", "AR2", "Lascade, LLC")


def test_cache_dir(tmp_path):
    assert cache_dir(tmp_path) == tmp_path / ".cache"
    assert cache_dir(tmp_path).is_dir()


def test_write_csv_atomic_leaves_no_temp_files(tmp_path):
    path = tmp_path / "ads.csv"
    n = write_csv_atomic(path, ["a", "b"], [{"a": 1, "b": 2}, {"a": 3, "b": 4}])
    assert n == 2
    assert [p.name for p in tmp_path.iterdir()] == ["ads.csv"]
    assert path.read_text().splitlines()[0] == "a,b"


def test_write_csv_atomic_keeps_old_file_when_rows_raise(tmp_path):
    path = tmp_path / "ads.csv"
    path.write_text("old")

    def rows():
        yield {"a": 1}
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        write_csv_atomic(path, ["a"], rows())
    assert path.read_text() == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["ads.csv"]


def test_entity_dir_falls_back_to_id_slug_when_name_has_no_ascii(tmp_path):
    d = entity_dir(tmp_path, "t", "AR123", "株式会社")
    assert d == tmp_path / "t" / "ar123"
    assert json.loads((d / "entity.json").read_text()) == {"id": "AR123", "name": "株式会社"}


def test_write_csv_atomic_defuses_spreadsheet_formulas(tmp_path):
    import csv

    p = tmp_path / "a.csv"
    rows = [{"a": "=HYPERLINK(\"http://x\")", "b": "+1 555", "c": "-30% off", "d": "@SUM(A1)", "e": "\tTab"},
            {"a": "Book now - save", "b": 5, "c": -1.5, "d": "", "e": None}]
    write_csv_atomic(p, ["a", "b", "c", "d", "e"], rows)
    got = list(csv.DictReader(p.open(encoding="utf-8")))
    assert got[0] == {"a": "'=HYPERLINK(\"http://x\")", "b": "'+1 555", "c": "'-30% off", "d": "'@SUM(A1)",
                      "e": "'\tTab"}
    assert got[1] == {"a": "Book now - save", "b": "5", "c": "-1.5", "d": "", "e": ""}  # numbers untouched


def test_write_bytes_atomic_creates_parents_and_leaves_no_temp_files(tmp_path):
    path = tmp_path / "a" / "b" / "x.bin"
    write_bytes_atomic(path, b"one")
    write_bytes_atomic(path, b"two")
    assert path.read_bytes() == b"two"
    assert [p.name for p in path.parent.iterdir()] == ["x.bin"]


def test_write_json_atomic_writes_readable_utf8_json(tmp_path):
    path = tmp_path / "x.json"
    write_json_atomic(path, {"name": "Café", "n": 1})
    assert "Café" in path.read_text(encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8")) == {"name": "Café", "n": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]
