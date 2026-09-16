"""Mask primitives built on OpenCV. Masks are single-channel uint8, 0 or 255."""

import hashlib

import cv2
import numpy as np


def decode_image(data: bytes) -> np.ndarray:
    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise ValueError("cannot decode image")
    return img


def decode_mask(data: bytes) -> np.ndarray:
    """Decode an uploaded mask to binary {0,255} single channel."""
    img = decode_image(data)
    if img.ndim == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(img, 127, 255, cv2.THRESH_BINARY)
    return binary


def encode_mask(mask: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", mask)
    if not ok:
        raise ValueError("cannot encode mask")
    return buf.tobytes()


def empty_mask(height: int, width: int) -> np.ndarray:
    return np.zeros((height, width), dtype=np.uint8)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def changed_pixels(before: np.ndarray, after: np.ndarray) -> np.ndarray:
    """Boolean mask of pixels that differ between two versions."""
    return cv2.bitwise_xor(before, after) > 0


def intersects(a: np.ndarray, b: np.ndarray) -> bool:
    return bool(np.any(np.logical_and(a, b)))


def overlap(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return np.logical_and(a, b)


def region_bboxes(bool_mask: np.ndarray, min_area: int = 1) -> list[dict]:
    """Bounding boxes of connected components — used to describe conflict regions."""
    n, _, stats, _ = cv2.connectedComponentsWithStats(bool_mask.astype(np.uint8), connectivity=8)
    boxes = []
    for i in range(1, n):
        x, y, w, h, area = (int(v) for v in stats[i])
        if area >= min_area:
            boxes.append({"x": x, "y": y, "w": w, "h": h, "pixels": area})
    return boxes


def rasterize_polygon(points: list[list[float]], height: int, width: int) -> np.ndarray:
    mask = np.zeros((height, width), dtype=np.uint8)
    pts = np.array(points, dtype=np.int32).reshape((-1, 1, 2))
    cv2.fillPoly(mask, [pts], 255)
    return mask


def resize_mask(mask: np.ndarray, width: int, height: int) -> np.ndarray:
    """Nearest-neighbour resize for migration onto a differently-sized image."""
    resized = cv2.resize(mask, (width, height), interpolation=cv2.INTER_NEAREST)
    _, binary = cv2.threshold(resized, 127, 255, cv2.THRESH_BINARY)
    return binary


def count_pixels(bool_mask: np.ndarray) -> int:
    return int(np.count_nonzero(bool_mask))
