# YT-Sync — Project Guide for Claude

## What This Is

**YT-Sync** is a self-hosted YouTube playlist manager and local media player. It runs a Python HTTP server that exposes a web UI for managing playlists, downloading videos/audio via `yt-dlp`, and playing locally-downloaded media through a browser-based player.

## Project Structure

```
audioooo/
├── yt_sync.py            # Main Python server (all backend logic)
├── yt_sync_ui.html       # Manager UI  → served at /
├── yt_sync_player.html   # Player UI   → served at /player
├── ffmpeg/               # Bundled ffmpeg/ffprobe binaries
│   ├── ffmpeg
│   └── ffprobe
├── YTSync/               # Runtime data directory (auto-created)
│   ├── data.json         # Playlist/video state
│   ├── downloads/        # Downloaded media files
│   └── thumb_cache/      # Cached thumbnails (JPEG)
├── git.ignore
└── .vscode/sftp.json     # SFTP deploy config (host: 192.168.50.26:2224)
```

## How to Run

```bash
python yt_sync.py [--port 7777] [--host 0.0.0.0] [--threads 3]
```

- Manager UI: `http://localhost:7777`
- Player UI:  `http://localhost:7777/player`

**Dependencies:**
- Python 3 (stdlib only — no pip installs needed for the server itself)
- `yt-dlp` must be on PATH (`pip install yt-dlp`)
- `ffmpeg`/`ffprobe` bundled in `./ffmpeg/` directory

## Architecture

### Backend (`yt_sync.py`)

Single-file Python HTTP server using `http.server.BaseHTTPRequestHandler`. No frameworks.

**Key sections:**
- **Config** (top of file): `BASE_DIR`, `DOWNLOAD_DIR`, `THUMB_CACHE_DIR`, `FFMPEG_LOCATION`
- **Data store**: JSON file (`YTSync/data.json`), protected by `data_lock` (threading.Lock)
- **yt-dlp helpers**: `fetch_playlist_info()`, `build_ydl_args()` — all downloads go through `yt-dlp` subprocess
- **Thumbnail system**: Downloaded to `YTSync/thumb_cache/<video_id>.jpg`; served at `/api/thumb/<video_id>`
- **Job queue**: Thread pool with `queue.Queue` + `threading.Lock`; status tracked in `jobs` dict
- **HTTP handler** (`Handler`): Routes GET/POST/DELETE manually by path string matching
- **Media streaming**: `_serve_file()` supports HTTP Range requests for seeking

### API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Manager UI |
| GET | `/player` | Player UI |
| GET | `/api/playlists` | List all playlists |
| GET | `/api/playlist/<id>` | Single playlist |
| GET | `/api/thumb/<video_id>` | Thumbnail image |
| GET | `/api/stream/<pl_id>/<vid_id>` | Media stream (Range-aware) |
| GET | `/api/jobs` | All download jobs |
| GET | `/api/status` | Server status |
| GET | `/api/settings` | Settings |
| POST | `/api/playlist/add` | Add playlist by URL |
| POST | `/api/playlist/sync` | Re-sync playlist from YouTube |
| POST | `/api/video/add` | Add video to playlist |
| POST | `/api/download` | Queue download jobs |
| POST | `/api/settings/update` | Update settings |
| POST | `/api/jobs/clear` | Clear finished jobs |
| DELETE | `/api/playlist/<id>` | Delete playlist |
| DELETE | `/api/video/remove` | Remove video from playlist |

### Frontend

Two single-file HTML pages with vanilla JS — no build step, no bundler.

- **`yt_sync_ui.html`**: Playlist manager. Add/sync playlists, browse videos, queue downloads, monitor job progress.
- **`yt_sync_player.html`**: Media player. Playlist selector, track list with search/filter, video/audio playback with full controls (shuffle, loop, speed, fullscreen). Keyboard shortcuts: Space, arrows, P/N/S/L/M/F.

**Fonts:** Syne (headings) + DM Mono (monospace) from Google Fonts.

### Data Format (`YTSync/data.json`)

```json
{
  "playlists": {
    "<pl_id>": {
      "id": "...",
      "url": "...",
      "title": "...",
      "added": 1234567890.0,
      "synced": 1234567890.0,
      "videos": [
        {
          "id": "<youtube_video_id>",
          "title": "...",
          "uploader": "...",
          "duration": 123,
          "thumbnail": "https://...",
          "url": "https://www.youtube.com/watch?v=...",
          "downloaded": false,
          "file_path": null,
          "quality": "best",
          "audio_only": false
        }
      ]
    }
  },
  "settings": {
    "download_dir": "...",
    "threads": 3
  }
}
```

## Deployment

SFTP deploy target: `192.168.50.26:2224` (user: `Cash8503`) — configured in `.vscode/sftp.json` with `uploadOnSave: true`.

## Development Notes

- **No hot-reload**: Restart `yt_sync.py` after any Python changes.
- **HTML files are served directly**: Edits to `.html` files take effect on browser refresh.
- **Thread safety**: Always use `data_lock` when reading/writing `data.json`, `jobs_lock` for the `jobs` dict.
- **ffmpeg location**: Set by `FFMPEG_LOCATION = "ffmpeg"` (relative path to the `ffmpeg/` folder). The server auto-fixes executable permissions on startup.
- **Download quality options**: `best`, `1080p`, `720p`, `480p`, `360p` — or audio-only (extracts to MP3).
- **Thumbnail fallback**: Tries `maxresdefault.jpg` first, falls back to URL stored in playlist data.
