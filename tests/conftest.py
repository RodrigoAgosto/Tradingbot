import sqlite3

import pytest

from weatherbot import db


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    yield c
    c.close()
