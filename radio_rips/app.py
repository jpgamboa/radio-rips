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


def _ytdlp_audio_args() -> list[str]:
    """Return yt-dlp flags for the configured audio format."""
    fmt = _get_setting("ytdlp_format") or "mp3"
    if fmt == "best":
        return ["-x"]
    return ["-x", "--audio-format", fmt, "--audio-quality", "0"]


def _ytdlp_cookie_args() -> list[str]:
    """Return yt-dlp flags for cookie authentication."""
    cookie_method = _get_setting("ytdlp_cookie_method") or "none"
    if cookie_method == "browser":
        browser = _get_setting("ytdlp_cookie_browser") or "chrome"
        return ["--cookies-from-browser", browser]
    elif cookie_method == "file":
        cookie_file = _get_setting("ytdlp_cookie_file")
        if cookie_file and os.path.isfile(cookie_file):
            return ["--cookies", cookie_file]
    return []


def _run_ytdlp(job_id: str, url: str, out_dir: str):
    """Download via yt-dlp (direct YouTube/SoundCloud/etc)."""
    cmd = [_find_bin("yt-dlp")]
    cmd += _ytdlp_audio_args()
    cmd += _ytdlp_cookie_args()
    cmd += [
        "--newline",
        "-o", os.path.join(out_dir, "%(title)s.%(ext)s"),
        url,
    ]
    _stream_process(job_id, cmd, out_dir)


def _run_deemix(job_id: str, url: str, out_dir: str):
    """Download via deemix (Deezer source, configurable quality)."""
    arl = _get_setting("deezer_arl") or config.DEEZER_ARL
    if not arl:
        _finish_job(job_id, out_dir, 1, "Deezer ARL not configured — set it in Settings")
        return
    bitrate = _get_setting("deezer_quality") or "flac"
    _stream_process(job_id, [
        _find_bin("deemix"),
        "--bitrate", bitrate,
        "-p", out_dir,
        url,
    ], out_dir, env={**os.environ, "DEEZER_ARL": arl})


# ---------------------------------------------------------------------------
# Playlist resolvers (Spotify, Apple Music, Tidal → "Artist - Title" lists)
# ---------------------------------------------------------------------------

def _detect_source(url: str) -> str | None:
    """Return 'spotify', 'apple', 'tidal', 'deezer', or None for direct yt-dlp URLs."""
    if "open.spotify.com/" in url or "spotify:" in url:
        return "spotify"
    if "music.apple.com/" in url:
        return "apple"
    if "tidal.com/" in url:
        return "tidal"
    if "deezer.com/" in url:
        return "deezer"
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


def _odesli_lookup(song_url: str) -> str | None:
    """Use Odesli (song.link) API to get 'Artist - Title' from any song URL."""
    try:
        resp = requests.get(
            "https://api.song.link/v1-alpha.1/links",
            params={"url": song_url},
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        # Pick the first available entity for metadata
        for entity in data.get("entitiesByUniqueId", {}).values():
            artist = entity.get("artistName", "")
            title = entity.get("title", "")
            if artist and title:
                return f"{artist} - {title}"
    except Exception:
        pass
    return None


def _resolve_apple_music(url: str) -> list[str]:
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    resp.raise_for_status()

    # Collect raw track info: list of (name, artist, url)
    raw_tracks: list[tuple[str, str, str]] = []

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
            if item.get("@type") == "MusicComposition":
                audio = item.get("audio", {})
                by = audio.get("byArtist", [])
                artist = by[0].get("name", "") if isinstance(by, list) and by else by.get("name", "") if isinstance(by, dict) else ""
                name = item.get("name", "")
                if name:
                    raw_tracks.append((name, artist, item.get("url", "")))

            elif item.get("@type") in ("MusicPlaylist", "MusicAlbum"):
                track_key = "track" if "track" in item else "tracks"
                for t in item.get(track_key, []):
                    name = t.get("name", "")
                    by = t.get("byArtist", {})
                    artist = by.get("name", "") if isinstance(by, dict) else ""
                    if not artist and isinstance(by, list) and by:
                        artist = by[0].get("name", "")
                    track_url = t.get("url", "")
                    if name:
                        raw_tracks.append((name, artist, track_url))

    # Enrich tracks missing artist info via Odesli
    tracks = []
    for name, artist, track_url in raw_tracks:
        if artist:
            tracks.append(f"{artist} - {name}")
        elif track_url:
            enriched = _odesli_lookup(track_url)
            tracks.append(enriched if enriched else name)
        else:
            tracks.append(name)

    if not tracks:
        title_match = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', resp.text)
        if title_match:
            tracks.append(title_match.group(1))

    if not tracks:
        raise ValueError("Could not parse tracks from Apple Music page")
    return tracks


def _extract_tidal_id(url: str) -> tuple[str, str]:
    """Extract resource type and ID from a Tidal URL."""
    m = re.search(r'tidal\.com/(?:browse/)?(track|album|playlist)/([a-f0-9-]+)', url)
    if not m:
        raise ValueError(f"Could not parse Tidal URL: {url}")
    return m.group(1), m.group(2)


_TIDAL_API = "https://api.tidal.com/v1"
_TIDAL_TOKEN = "CzET4vdadNUFQ5JU"  # public web client token


def _tidal_api_get(path: str, params: dict | None = None) -> dict:
    """Call the public Tidal API."""
    p = {"countryCode": "US", "limit": 100, **(params or {})}
    resp = requests.get(
        f"{_TIDAL_API}{path}",
        params=p,
        headers={"X-Tidal-Token": _TIDAL_TOKEN},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _resolve_tidal(url: str) -> list[str]:
    rtype, rid = _extract_tidal_id(url)

    if rtype == "track":
        data = _tidal_api_get(f"/tracks/{rid}")
        artist = data.get("artist", {}).get("name", "")
        title = data.get("title", "")
        return [f"{artist} - {title}"] if artist and title else [title]

    if rtype == "album":
        data = _tidal_api_get(f"/albums/{rid}/tracks")
        tracks = []
        for t in data.get("items", []):
            artist = t.get("artist", {}).get("name", "")
            title = t.get("title", "")
            if title:
                tracks.append(f"{artist} - {title}" if artist else title)
        return tracks

    if rtype == "playlist":
        tracks = []
        offset = 0
        while True:
            data = _tidal_api_get(f"/playlists/{rid}/tracks", {"offset": offset})
            items = data.get("items", [])
            if not items:
                break
            for t in items:
                artist = t.get("artist", {}).get("name", "")
                title = t.get("title", "")
                if title:
                    tracks.append(f"{artist} - {title}" if artist else title)
            offset += len(items)
            if offset >= data.get("totalNumberOfItems", 0):
                break
        return tracks

    raise ValueError(f"Unsupported Tidal resource type: {rtype}")


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

            cmd = [ytdlp] + _ytdlp_audio_args() + _ytdlp_cookie_args() + [
                "--newline",
                "--match-filter", "duration<600",
                "--max-downloads", "1",
                "-o", os.path.join(out_dir, "%(title)s.%(ext)s"),
                f"ytsearch5:{query} official audio",
            ]
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=300,
            )
            # returncode 101 = --max-downloads limit reached (success)
            if proc.returncode not in (0, 101):
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


def _search_deezer(query: str) -> str | None:
    """Search the public Deezer API and return the first track URL, or None."""
    try:
        resp = requests.get(
            "https://api.deezer.com/search",
            params={"q": query, "limit": 1},
            timeout=10,
        )
        data = resp.json()
        if data.get("data"):
            return data["data"][0].get("link")
    except Exception:
        pass
    return None


def _run_playlist_deezer(job_id: str, url: str, out_dir: str, source: str):
    """Resolve tracks from a streaming service, find on Deezer, download via deemix."""
    try:
        arl = _get_setting("deezer_arl") or config.DEEZER_ARL
        if not arl:
            _finish_job(job_id, out_dir, 1, "Deezer ARL not configured — set it in Settings")
            return

        _set_progress(job_id, 0, f"Resolving {source.title()} tracks...")
        tracks = _RESOLVERS[source](url)
        if not tracks:
            _finish_job(job_id, out_dir, 1, f"No tracks found from {source.title()} URL")
            return

        total = len(tracks)
        errors = []
        bitrate = _get_setting("deezer_quality") or "flac"
        deemix = _find_bin("deemix")

        for i, query in enumerate(tracks, 1):
            pct = int((i - 1) / total * 100)
            _set_progress(job_id, pct, f"[{i}/{total}] Searching Deezer: {query}")

            deezer_url = _search_deezer(query)
            if not deezer_url:
                errors.append(f"Not found on Deezer: {query}")
                continue

            _set_progress(job_id, pct, f"[{i}/{total}] Downloading: {query}")
            proc = subprocess.run(
                [deemix, "--bitrate", bitrate, "-p", out_dir, deezer_url],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=300,
                env={**os.environ, "DEEZER_ARL": arl},
            )
            # returncode 101 = --max-downloads limit reached (success)
            if proc.returncode not in (0, 101):
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
    has_deezer = bool(_get_setting("deezer_arl") or config.DEEZER_ARL)
    return render_template("index.html", has_deezer=has_deezer)


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
    backend = request.form.get("backend", "youtube")

    if source == "deezer":
        # Deezer URL always goes to deemix
        thread = threading.Thread(
            target=_run_deemix, args=(job_id, url, out_dir), daemon=True,
        )
    elif source and backend == "deezer":
        # Streaming URL (Spotify/Apple/Tidal) → resolve tracks → Deezer
        thread = threading.Thread(
            target=_run_playlist_deezer, args=(job_id, url, out_dir, source), daemon=True,
        )
    elif source:
        # Streaming URL → resolve tracks → YouTube
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
        ytdlp_format=_get_setting("ytdlp_format") or "mp3",
        ytdlp_cookie_method=_get_setting("ytdlp_cookie_method") or "none",
        ytdlp_cookie_browser=_get_setting("ytdlp_cookie_browser") or "chrome",
        ytdlp_cookie_file=_get_setting("ytdlp_cookie_file"),
        deezer_arl=_get_setting("deezer_arl") or config.DEEZER_ARL,
        deezer_quality=_get_setting("deezer_quality") or "flac",
    )


@app.route("/settings", methods=["POST"])
def settings_save():
    download_dir = request.form.get("download_dir", "").strip()
    spotify_client_id = request.form.get("spotify_client_id", "").strip()
    spotify_client_secret = request.form.get("spotify_client_secret", "").strip()
    deezer_arl = request.form.get("deezer_arl", "").strip()
    deezer_quality = request.form.get("deezer_quality", "flac").strip()

    if download_dir:
        os.makedirs(download_dir, exist_ok=True)
        _set_setting("download_dir", download_dir)

    ytdlp_format = request.form.get("ytdlp_format", "mp3").strip()

    ytdlp_cookie_method = request.form.get("ytdlp_cookie_method", "none").strip()
    ytdlp_cookie_browser = request.form.get("ytdlp_cookie_browser", "chrome").strip()
    ytdlp_cookie_file = request.form.get("ytdlp_cookie_file", "").strip()

    _set_setting("spotify_client_id", spotify_client_id)
    _set_setting("spotify_client_secret", spotify_client_secret)
    _set_setting("ytdlp_format", ytdlp_format)
    _set_setting("ytdlp_cookie_method", ytdlp_cookie_method)
    _set_setting("ytdlp_cookie_browser", ytdlp_cookie_browser)
    _set_setting("ytdlp_cookie_file", ytdlp_cookie_file)
    _set_setting("deezer_arl", deezer_arl)
    _set_setting("deezer_quality", deezer_quality)

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
