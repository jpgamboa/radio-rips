import os

BASE_DIR = os.environ.get("RADIO_RIPS_DIR", os.getcwd())
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
DATABASE = os.path.join(BASE_DIR, "jobs.db")
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-key-change-me")
