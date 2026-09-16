import os
import tempfile

# must be set before any app module is imported
_TMP_STORAGE = tempfile.mkdtemp(prefix="annot_test_storage_")
os.environ["STORAGE_DIR"] = _TMP_STORAGE
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg2://annotator:annotator@127.0.0.1:5432/annotation_test",
)

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import Base, engine
from app.main import app


@pytest.fixture(scope="session", autouse=True)
def _schema():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture(autouse=True)
def _clean_tables():
    yield
    with engine.begin() as conn:
        # annotations <-> arbitrations have a circular FK; one CASCADE
        # truncate handles cycles that per-table DELETEs cannot order
        names = ", ".join(t.name for t in Base.metadata.sorted_tables)
        conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


@pytest.fixture()
def client():
    return TestClient(app)


# ---------- helpers ----------


def make_image_bytes(w=64, h=48, color=(200, 180, 160)) -> bytes:
    img = np.full((h, w, 3), color, dtype=np.uint8)
    cv2.circle(img, (w // 2, h // 2), min(w, h) // 4, (30, 30, 200), -1)
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


def make_mask_bytes(w=64, h=48, rects=()) -> bytes:
    """rects: list of (x, y, w, h) filled with 255."""
    mask = np.zeros((h, w), dtype=np.uint8)
    for x, y, rw, rh in rects:
        mask[y : y + rh, x : x + rw] = 255
    ok, buf = cv2.imencode(".png", mask)
    assert ok
    return buf.tobytes()


def upload_image(client: TestClient, name="img", w=64, h=48) -> dict:
    r = client.post(
        f"/images?name={name}",
        files={"file": ("img.png", make_image_bytes(w, h), "image/png")},
    )
    assert r.status_code == 201, r.text
    return r.json()


def create_annotation(client: TestClient, image_id: int, label="cat") -> dict:
    r = client.post(f"/images/{image_id}/annotations", json={"label": label})
    assert r.status_code == 201, r.text
    return r.json()


def save_mask(client: TestClient, ann_id: int, mask: bytes, author: str, base: int, **kw):
    params = {"author": author, "base_version": base, **kw}
    return client.put(
        f"/annotations/{ann_id}/mask",
        params=params,
        files={"file": ("mask.png", mask, "image/png")},
    )
