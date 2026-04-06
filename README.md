# Radio Rips

A self-hosted web UI for ripping audio from YouTube, Spotify, Apple Music, and Tidal.

Paste a URL, hit "Rip It", and get MP3 files. Spotify, Apple Music, and Tidal links are automatically resolved to YouTube and downloaded via [yt-dlp](https://github.com/yt-dlp/yt-dlp).

## Supported Sources

| Source | URL format | API key needed? |
|--------|-----------|-----------------|
| YouTube / SoundCloud / etc. | Direct link | No |
| Spotify (track, album, playlist) | `open.spotify.com/...` | Yes (free) |
| Apple Music (track, album, playlist) | `music.apple.com/...` | No |
| Tidal (track, album, playlist) | `tidal.com/...` | No |

## Install

### Prerequisites

You need **Python 3.10+** and **ffmpeg** (required by yt-dlp for audio conversion).

**macOS (Homebrew):**

```bash
brew install python ffmpeg
```

**Ubuntu / Debian:**

```bash
sudo apt update && sudo apt install python3 python3-pip python3-venv ffmpeg
```

**Arch Linux:**

```bash
sudo pacman -S python python-pip ffmpeg
```

### Install Radio Rips

```bash
# Clone the repo
git clone https://github.com/jpgamboa/radio-rips.git
cd radio-rips

# Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install the package (pulls in yt-dlp, flask, spotipy automatically)
pip install .
```

### Run

```bash
source .venv/bin/activate   # if not already active
radio-rips
```

Open **http://localhost:5000** in your browser.

## Setup

### Spotify (optional)

Spotify URLs require a free API key. Go to **Settings** in the web UI for step-by-step instructions, or:

1. Go to the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)
2. Create an app (name it anything, set redirect URI to `http://localhost`)
3. Copy the **Client ID** and **Client Secret**
4. Paste them into the Settings page

### Download Directory

By default, files are saved to a `downloads/` folder in the current working directory. You can change this in **Settings**.

## How It Works

- **YouTube / SoundCloud / direct links** — downloaded directly by yt-dlp
- **Spotify / Apple Music / Tidal** — track metadata (artist + title) is extracted from the URL, then each track is searched on YouTube (`ytsearch1:"Artist - Title"`) and downloaded via yt-dlp

All audio is output as MP3 at the highest quality (V0 VBR).
