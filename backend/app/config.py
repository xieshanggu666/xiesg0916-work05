import os

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg2://annotator:annotator@127.0.0.1:5432/annotation",
)
STORAGE_DIR = os.environ.get("STORAGE_DIR", os.path.join(os.path.dirname(__file__), "..", "storage"))
STORAGE_DIR = os.path.abspath(STORAGE_DIR)

# subdirectories under STORAGE_DIR
IMAGE_DIR = os.path.join(STORAGE_DIR, "images")
MASK_DIR = os.path.join(STORAGE_DIR, "masks")
EXPORT_DIR = os.path.join(STORAGE_DIR, "exports")

for d in (STORAGE_DIR, IMAGE_DIR, MASK_DIR, EXPORT_DIR):
    os.makedirs(d, exist_ok=True)
