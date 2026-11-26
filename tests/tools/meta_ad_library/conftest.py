import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from meta_fakes import HOSTS, Cooldown, FakeMeta, PacedSession  # noqa: E402


@pytest.fixture
def make_session(tmp_path):
    made = []

    def make(fake: FakeMeta) -> PacedSession:
        s = PacedSession(Cooldown(tmp_path / "cooldown.sqlite3", clock=lambda: 1_000_000.0), HOSTS, transport=fake,
                         sleep=lambda _: None, clock=lambda: 1_000_000.0)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()
