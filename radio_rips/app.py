import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)

from radio_rips import config


def _find_bin(name: str) -> str:
    """Find a binary on PATH, raising a clear error if missing."""
    path = shutil.which(name)
    if not path:
        raise FileNotFoundError(
            f"'{name}' not found on PATH. Install it with: pip install {name}"
        )
    return path

app = Flask(__name__)
app.secret_key = config.SECRET_KEY

# In-memory progress store: { job_id: { "percent": 0-100, "message": str } }
_progress: dict[str, dict] = {}
_progress_lock = threading.Lock()


def _set_progress(job_id: str, percent: int, message: str = ""):
    with _progress_lock:
        _progress[job_id] = {"percent": min(percent, 100), "message": message}


def _get_progress(job_id: str) -> dict:
    with _progress_lock:
        return _progress.get(job_id, {"percent": 0, "message": ""})


def _clear_progress(job_id: str):
    with _progress_lock:
        _progress.pop(job_id, None)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with get_db() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id          TEXT PRIMARY KEY,
                url         TEXT NOT NULL,
                quality     TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'pending',
                created_at  TEXT NOT NULL,
                finished_at TEXT,
                error       TEXT,
                output_dir  TEXT
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )


init_db()


def _get_setting(key: str, default: str = "") -> str:
    with get_db() as db:
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def _set_setting(key: str, value: str):
    with get_db() as db:
        db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=?",
            (key, value, value),
        )

# Regex to pull percentages from spotdl/deemix output lines
_PCT_RE = re.compile(r"(\d{1,3})%")


# ---------------------------------------------------------------------------
# Download workers
# ---------------------------------------------------------------------------

def _stream_process(job_id: str, cmd: list[str], out_dir: str, env=None):
    """Run a subprocess, parse progress from its output, update the store."""
    _set_progress(job_id, 0, "Starting...")
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        last_pct = 0
        all_output = []
        for line in proc.stdout:
            line = line.rstrip()
            all_output.append(line)
            m = _PCT_RE.search(line)
            if m:
                pct = int(m.group(1))
                if pct >= last_pct:
                    last_pct = pct
                    _set_progress(job_id, pct, line)
            elif line:
                _set_progress(job_id, last_pct, line)
        proc.wait(timeout=1800)
        stderr_text = "\n".join(all_output) if proc.returncode != 0 else ""
        _finish_job(job_id, out_dir, proc.returncode, stderr_text)
    except Exception as exc:
        _finish_job(job_id, out_dir, 1, str(exc))
    finally:
        _clear_progress(job_id)


def _run_ytdlp(job_id: str, url: str, out_dir: str):
    """Download via yt-dlp (direct YouTube/SoundCloud/etc, MP3 output)."""
    _stream_process(job_id, [
        _find_bin("yt-dlp"),
        "-x",
        "--audio-format", "mp3",
        "--audio-quality", "0",
        "--newline",
        "-o", os.path.join(out_dir, "%(title)s.%(ext)s"),
        url,
    ], out_dir)


# ---------------------------------------------------------------------------
# Playlist resolvers (Spotify, Apple Music, Tidal → "Artist - Title" lists)
# ---------------------------------------------------------------------------

def _detect_source(url: str) -> str | None:
    """Return 'spotify', 'apple', 'tidal', or None for direct yt-dlp URLs."""
    if "open.spotify.com/" in url or "spotify:" in url:
        return "spotify"
    if "music.apple.com/" in url:
        return "apple"
    if "tidal.com/" in url:
        return "tidal"
    return None


def _resolve_spotify(url: str) -> list[str]:
    client_id = _get_setting("spotify_client_id")
    client_secret = _get_setting("spotify_client_secret")
    if not client_id or not client_secret:
        raise ValueError("Spotify Client ID and Secret must be configured in Settings")
    sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
        client_id=client_id, client_secret=client_secret,
    ))

    if "/track/" in url or "spotify:track:" in url:
        t = sp.track(url)
        return [f"{t['artists'][0]['name']} - {t['name']}"]

    if "/album/" in url or "spotify:album:" in url:
        album = sp.album(url)
        return [f"{t['artists'][0]['name']} - {t['name']}" for t in album["tracks"]["items"]]

    if "/playlist/" in url or "spotify:playlist:" in url:
        tracks = []
        results = sp.playlist_items(url, fields="items.track(name,artists),next")
        while results:
            for item in results["items"]:
                t = item.get("track")
                if t and t.get("name"):
                    tracks.append(f"{t['artists'][0]['name']} - {t['name']}")
            results = sp.next(results) if results.get("next") else None
        return tracks

    raise ValueError(f"Unsupported Spotify URL: {url}")


def _resolve_apple_music(url: str) -> list[str]:
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    resp.raise_for_status()

    tracks = []
    # Apple Music embeds JSON-LD with MusicRecording schema
    for match in re.finditer(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        resp.text, re.DOTALL,
    ):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue

        # Can be a single object or a list
        items = data if isinstance(data, list) else [data]
        for item in items:
            if item.get("@type") == "MusicAlbum":
                for t in item.get("tracks", []):
                    artist = t.get("byArtist", {}).get("name", "")
                    name = t.get("name", "")
                    if artist and name:
                        tracks.append(f"{artist} - {name}")
            elif item.get("@type") == "MusicPlaylist":
                for t in item.get("track", []):
                    artist = t.get("byArtist", {}).get("name", "")
                    name = t.get("name", "")
                    if artist and name:
                        tracks.append(f"{artist} - {name}")
            elif item.get("@type") == "MusicRecording":
                artist = item.get("byArtist", {}).get("name", "")
                name = item.get("name", "")
                if artist and name:
                    tracks.append(f"{artist} - {name}")

    if not tracks:
        # Fallback: try parsing meta tags for a single song page
        title_match = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', resp.text)
        if title_match:
            tracks.append(title_match.group(1))

    if not tracks:
        raise ValueError("Could not parse tracks from Apple Music page")
    return tracks


def _resolve_tidal(url: str) -> list[str]:
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    resp.raise_for_status()

    tracks = []
    # Tidal embeds Next.js JSON data with track info
    for match in re.finditer(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        resp.text, re.DOTALL,
    ):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if item.get("@type") == "MusicRecording":
                artist = item.get("byArtist", {}).get("name", "")
                name = item.get("name", "")
                if artist and name:
                    tracks.append(f"{artist} - {name}")

    if not tracks:
        # Fallback: look for __NEXT_DATA__ JSON blob
        nd = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', resp.text, re.DOTALL)
        if nd:
            try:
                ndata = json.loads(nd.group(1))
                # Navigate to track items in the page props
                modules = (
                    ndata.get("props", {})
                    .get("pageProps", {})
                )
                # Try playlist/album track items
                for key in ("playlist", "album"):
                    obj = modules.get(key, {})
                    for t in obj.get("items", obj.get("tracks", [])):
                        # Items can be nested under "item"
                        track = t.get("item", t) if isinstance(t, dict) else t
                        title = track.get("title", "")
                        artists = track.get("artists", [])
                        artist = artists[0].get("name", "") if artists else ""
                        if artist and title:
                            tracks.append(f"{artist} - {title}")
            except (json.JSONDecodeError, KeyError, IndexError):
                pass

    if not tracks:
        # Last resort: og:title
        title_match = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', resp.text)
        if title_match:
            tracks.append(title_match.group(1))

    if not tracks:
        raise ValueError("Could not parse tracks from Tidal page")
    return tracks


_RESOLVERS = {
    "spotify": _resolve_spotify,
    "apple": _resolve_apple_music,
    "tidal": _resolve_tidal,
}


def _run_playlist(job_id: str, url: str, out_dir: str, source: str):
    """Resolve tracks from a streaming service, then download each via yt-dlp."""
    try:
        _set_progress(job_id, 0, f"Resolving {source.title()} tracks...")
        tracks = _RESOLVERS[source](url)
        if not tracks:
            _finish_job(job_id, out_dir, 1, f"No tracks found from {source.title()} URL")
            return

        total = len(tracks)
        errors = []
        ytdlp = _find_bin("yt-dlp")

        for i, query in enumerate(tracks, 1):
            pct = int((i - 1) / total * 100)
            _set_progress(job_id, pct, f"[{i}/{total}] {query}")

            proc = subprocess.run(
                [
                    ytdlp, "-x",
                    "--audio-format", "mp3",
                    "--audio-quality", "0",
                    "--newline",
                    "-o", os.path.join(out_dir, "%(title)s.%(ext)s"),
                    f"ytsearch1:{query}",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=300,
            )
            if proc.returncode != 0:
                errors.append(f"Failed: {query}")

        if errors and len(errors) == total:
            _finish_job(job_id, out_dir, 1, "\n".join(errors))
        elif errors:
            now = datetime.now(timezone.utc).isoformat()
            with get_db() as db:
                db.execute(
                    "UPDATE jobs SET status=?, finished_at=?, error=? WHERE id=?",
                    ("done", now, "\n".join(errors), job_id),
                )
        else:
            _finish_job(job_id, out_dir, 0, "")
    except Exception as exc:
        _finish_job(job_id, out_dir, 1, str(exc))
    finally:
        _clear_progress(job_id)


def _finish_job(job_id: str, out_dir: str, returncode: int, stderr: str):
    now = datetime.now(timezone.utc).isoformat()
    status = "done" if returncode == 0 else "error"
    error = stderr.strip() if returncode != 0 else None
    _set_progress(job_id, 100 if returncode == 0 else 0, "")
    with get_db() as db:
        db.execute(
            "UPDATE jobs SET status=?, finished_at=?, error=? WHERE id=?",
            (status, now, error, job_id),
        )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/submit", methods=["POST"])
def submit():
    url = request.form.get("url", "").strip()
    quality = request.form.get("quality", "mp3")

    if not url:
        return redirect(url_for("index"))

    job_id = uuid.uuid4().hex[:12]
    download_dir = _get_setting("download_dir") or config.DOWNLOAD_DIR
    out_dir = os.path.join(download_dir, job_id)
    os.makedirs(out_dir, exist_ok=True)

    now = datetime.now(timezone.utc).isoformat()
    with get_db() as db:
        db.execute(
            "INSERT INTO jobs (id, url, quality, status, created_at, output_dir) VALUES (?,?,?,?,?,?)",
            (job_id, url, quality, "running", now, out_dir),
        )

    source = _detect_source(url)
    if source:
        thread = threading.Thread(
            target=_run_playlist, args=(job_id, url, out_dir, source), daemon=True,
        )
    else:
        thread = threading.Thread(
            target=_run_ytdlp, args=(job_id, url, out_dir), daemon=True,
        )
    thread.start()

    return redirect(url_for("job_detail", job_id=job_id))


@app.route("/job/<job_id>")
def job_detail(job_id: str):
    with get_db() as db:
        job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job:
        return "Job not found", 404
    files = _list_job_files(job)
    return render_template("job.html", job=job, files=files)


@app.route("/api/job/<job_id>")
def api_job_status(job_id: str):
    with get_db() as db:
        job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job:
        return jsonify({"error": "not found"}), 404
    progress = _get_progress(job_id)
    return jsonify({
        "id": job["id"],
        "status": job["status"],
        "error": job["error"],
        "files": _list_job_files(job),
        "progress": progress["percent"],
        "progress_message": progress["message"],
    })


@app.route("/history")
def history():
    with get_db() as db:
        jobs = db.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT 200"
        ).fetchall()
    return render_template("history.html", jobs=jobs)


@app.route("/files/<job_id>/<path:filename>")
def serve_file(job_id: str, filename: str):
    safe_dir = os.path.join(config.DOWNLOAD_DIR, job_id)
    return send_from_directory(safe_dir, filename, as_attachment=True)


@app.route("/settings")
def settings():
    return render_template(
        "settings.html",
        download_dir=_get_setting("download_dir") or config.DOWNLOAD_DIR,
        spotify_client_id=_get_setting("spotify_client_id"),
        spotify_client_secret=_get_setting("spotify_client_secret"),
    )


@app.route("/settings", methods=["POST"])
def settings_save():
    download_dir = request.form.get("download_dir", "").strip()
    spotify_client_id = request.form.get("spotify_client_id", "").strip()
    spotify_client_secret = request.form.get("spotify_client_secret", "").strip()

    if download_dir:
        os.makedirs(download_dir, exist_ok=True)
        _set_setting("download_dir", download_dir)

    _set_setting("spotify_client_id", spotify_client_id)
    _set_setting("spotify_client_secret", spotify_client_secret)

    return redirect(url_for("settings"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _list_job_files(job) -> list[dict]:
    out_dir = job["output_dir"]
    if not out_dir or not os.path.isdir(out_dir):
        return []
    files = []
    for p in sorted(Path(out_dir).rglob("*")):
        if p.is_file():
            rel = p.relative_to(out_dir)
            files.append({
                "name": str(rel),
                "size": p.stat().st_size,
                "url": url_for("serve_file", job_id=job["id"], filename=str(rel)),
            })
    return files


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
    app.run(host="0.0.0.0", port=5000, debug=True)


if __name__ == "__main__":
    main()
