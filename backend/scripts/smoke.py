#!/usr/bin/env python3
"""End-to-end smoke test against the running server on :8000."""
import io
import json
import urllib.request

import cv2
import numpy as np

B = "http://127.0.0.1:8000"


def req(method, path, data=None, files=None, expect=200):
    body, headers = None, {}
    if files:
        boundary = "xBOUNDARY"
        parts = []
        for name, (fname, blob) in files.items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                f'filename="{fname}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
                + blob + b"\r\n"
            )
        body = b"".join(parts) + f"--{boundary}--\r\n".encode()
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(B + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r) as resp:
            out = json.loads(resp.read())
            assert resp.status == expect, (path, resp.status, out)
            return out
    except urllib.error.HTTPError as e:
        payload = e.read().decode()
        assert e.code == expect, f"{method} {path}: got {e.code}, want {expect}: {payload}"
        return json.loads(payload)


def png(arr):
    return cv2.imencode(".png", arr)[1].tobytes()


img = np.full((48, 64, 3), 180, np.uint8)
cv2.rectangle(img, (10, 10), (40, 40), (60, 60, 200), -1)

print("1. upload image")
im = req("POST", "/images?name=demo", files={"file": ("a.png", png(img))}, expect=201)
print("  ", im)

print("2. create annotation + save mask v1 (alice)")
ann = req("POST", f"/images/{im['id']}/annotations", {"label": "cat"}, expect=201)
mask1 = np.zeros((48, 64), np.uint8)
mask1[10:30, 10:30] = 255
r = req("PUT", f"/annotations/{ann['id']}/mask?author=alice&base_version=0",
        files={"file": ("m.png", png(mask1))})
print("  ", r)

print("3. bob overlaps from stale base -> 409 conflict, nothing written")
mask2 = np.zeros((48, 64), np.uint8)
mask2[15:35, 15:35] = 255
conflict = req("PUT", f"/annotations/{ann['id']}/mask?author=bob&base_version=0",
               files={"file": ("m.png", png(mask2))}, expect=409)
print("   conflict regions:", conflict["detail"]["conflicts"][0]["regions"])
assert req("GET", f"/annotations/{ann['id']}")["current_version"] == 1

print("4. submit -> reviewer rejects a region -> fix -> resubmit -> approve")
req("POST", f"/annotations/{ann['id']}/submit", {"actor": "alice", "expected_version": 1})
rej = req("POST", f"/annotations/{ann['id']}/review/reject",
          {"actor": "carol", "expected_version": 1,
           "regions": [{"polygon": [[10, 10], [20, 10], [20, 20], [10, 20]], "comment": "毛边"}]})
assert rej["open_regions"], "region must be open"
mask1[10:20, 10:20] = 0  # fix the rejected corner
req("PUT", f"/annotations/{ann['id']}/mask?author=alice&base_version=1",
    files={"file": ("m.png", png(mask1))})
sub = req("POST", f"/annotations/{ann['id']}/submit", {"actor": "alice", "expected_version": 2})
assert sub["open_regions"] == [], "touched region must resolve"
req("POST", f"/annotations/{ann['id']}/review/approve", {"actor": "carol", "expected_version": 2})
print("   approved")

print("5. replace image -> annotation goes stale -> explicit migrate")
big = cv2.resize(img, (128, 96))
rep = req("POST", f"/images/{im['id']}/replace", files={"file": ("b.png", png(big))})
assert rep["stale_annotation_ids"] == [ann["id"]]
stale_save = req("PUT", f"/annotations/{ann['id']}/mask?author=alice&base_version=2",
                 files={"file": ("m.png", png(mask1))}, expect=409)
assert stale_save["detail"]["kind"] == "stale"
mig = req("POST", f"/annotations/{ann['id']}/migrate", {"actor": "alice"}, expect=201)
print("   migrated to", mig)

print("6. re-approve migrated annotation, then export frozen versions")
req("POST", f"/annotations/{ann['id']}/submit", {"actor": "alice", "expected_version": 3})
req("POST", f"/annotations/{ann['id']}/review/approve", {"actor": "carol", "expected_version": 3})
job = req("POST", "/exports", {"name": "smoke"}, expect=201)
import time
for _ in range(50):
    j = req("GET", f"/exports/{job['id']}")
    if j["status"] in ("completed", "failed"):
        break
    time.sleep(0.2)
print("   export:", j["status"], f"{j['done_items']}/{j['total_items']}", "->", j["output_dir"])
assert j["status"] == "completed"
retry = req("POST", f"/exports/{job['id']}/retry", expect=409)  # completed jobs can't retry
print("   retry on completed job correctly refused (409)")

print("\nSMOKE OK")
