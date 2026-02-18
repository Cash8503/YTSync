#!/usr/bin/env python3
"""
YT-Sync: Local YouTube Playlist Manager + Player
Run: python yt_sync.py [--port 8080] [--threads 3]
Manager: http://localhost:8080
Player:  http://localhost:8080/player
"""

import json
import mimetypes
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
import argparse
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
import urllib.request

# ─── Config ───────────────────────────────────────────────────────────────────

BASE_DIR     = Path(__file__).parent / "YTSync"
DATA_FILE    = BASE_DIR / "data.json"
DOWNLOAD_DIR = BASE_DIR / "downloads"
THUMB_CACHE_DIR = BASE_DIR / "thumb_cache"

BASE_DIR.mkdir(exist_ok=True)
DOWNLOAD_DIR.mkdir(exist_ok=True)
THUMB_CACHE_DIR.mkdir(exist_ok=True)

DEFAULT_THREADS  = 3
# FFMPEG_LOCATION should point to the folder containing ffmpeg/ffprobe binaries
FFMPEG_LOCATION  = "ffmpeg"

# ─── Data Store ───────────────────────────────────────────────────────────────

data_lock = threading.Lock()

def load_data():
    if DATA_FILE.exists():
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"playlists": {}, "settings": {"download_dir": str(DOWNLOAD_DIR), "threads": DEFAULT_THREADS}}

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

# ─── yt-dlp helpers ───────────────────────────────────────────────────────────

def check_ytdlp():
    return shutil.which("yt-dlp") is not None

def build_ydl_args(video_id, quality, audio_only, out_dir):
    url  = f"https://www.youtube.com/watch?v={video_id}"
    args = ["yt-dlp", "--no-playlist"]
    if audio_only:
        args += ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
    else:
        fmt_map = {
            "best":  "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "1080p": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]",
            "720p":  "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]",
            "480p":  "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480]",
            "360p":  "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[height<=360]",
        }
        args += ["-f", fmt_map.get(quality, fmt_map["best"]), "--merge-output-format", "mp4"]
    tmpl = str(out_dir / "%(title)s [%(id)s].%(ext)s")
    if FFMPEG_LOCATION:
        args += ["--ffmpeg-location", FFMPEG_LOCATION]
    args += ["-o", tmpl, "--write-info-json", "--no-write-playlist-metafiles",
             "--progress", "--newline", url]
    return args

def fetch_playlist_info(url):
    cmd    = ["yt-dlp", "--flat-playlist", "-J", "--no-warnings", url]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "yt-dlp failed")
    data    = json.loads(result.stdout)
    entries = data.get("entries", [data])
    videos  = []
    for e in entries:
        if e:
            videos.append({
                "id":         e.get("id", ""),
                "title":      e.get("title", "Unknown"),
                "uploader":   e.get("uploader", ""),
                "duration":   e.get("duration"),
                "thumbnail":  e.get("thumbnail", ""),
                "url":        f"https://www.youtube.com/watch?v={e.get('id','')}",
                "downloaded": False,
                "file_path":  None,
            })
    return {
        "title":  data.get("title", data.get("webpage_url_basename", "Playlist")),
        "videos": videos,
    }

# ─── Progress parsing ─────────────────────────────────────────────────────────

_RE_PROGRESS = re.compile(
    r"\[download\]\s+([\d.]+)%\s+of\s+~?\s*([\d.]+\s*\S+)\s+at\s+([\d.]+\s*\S+/s)\s+ETA\s+([\d:]+)"
)
_RE_DEST = re.compile(r'(?:Destination:|Merging formats into)\s+"?([^"\n]+)"?')

def parse_line(line):
    m = _RE_PROGRESS.search(line)
    if m:
        return {"pct": float(m.group(1)), "size": m.group(2).strip(),
                "speed": m.group(3).strip(), "eta": m.group(4).strip()}
    return None

# ─── Thread-pool Job Queue ────────────────────────────────────────────────────

jobs      = {}
jobs_lock = threading.Lock()
job_queue = queue.Queue()

def _worker():
    while True:
        job_id = job_queue.get()
        try:
            _run_job(job_id)
        except Exception as e:
            with jobs_lock:
                if job_id in jobs:
                    jobs[job_id]["status"]   = "error"
                    jobs[job_id]["error"]    = str(e)
                    jobs[job_id]["finished"] = time.time()
        finally:
            job_queue.task_done()

def start_thread_pool(n):
    for _ in range(n):
        threading.Thread(target=_worker, daemon=True).start()

def _update_queue_positions():
    pos = 1
    for j in jobs.values():
        if j["status"] == "queued":
            j["queue_pos"] = pos
            pos += 1

def add_job(playlist_id, video_id, title, quality, audio_only):
    job_id = str(uuid.uuid4())[:8]
    with jobs_lock:
        jobs[job_id] = {
            "id": job_id, "playlist_id": playlist_id,
            "video_id": video_id, "title": title,
            "quality": quality, "audio_only": audio_only,
            "status": "queued", "queue_pos": 0,
            "progress": 0.0, "speed": "", "eta": "", "size": "",
            "phase": "queued", "log": [],
            "started": None, "finished": None, "file": None, "error": None,
        }
        _update_queue_positions()
    job_queue.put(job_id)
    return job_id

def _run_job(job_id):
    with jobs_lock:
        jobs[job_id]["status"]  = "running"
        jobs[job_id]["started"] = time.time()
        jobs[job_id]["phase"]   = "starting"
        _update_queue_positions()

    data = load_data()
    pl   = data["playlists"].get(jobs[job_id]["playlist_id"])
    if not pl:
        with jobs_lock:
            jobs[job_id]["status"]   = "error"
            jobs[job_id]["error"]    = "Playlist not found"
            jobs[job_id]["finished"] = time.time()
        return

    out_dir = DOWNLOAD_DIR / jobs[job_id]["playlist_id"]
    out_dir.mkdir(exist_ok=True)

    with jobs_lock:
        job_snap = dict(jobs[job_id])

    args = build_ydl_args(job_snap["video_id"], job_snap["quality"],
                          job_snap["audio_only"], out_dir)
    output_file = None

    # Debug: log the command being run
    print(f"[Job {job_id}] Running: {' '.join(args)}")

    try:
        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
        for raw in proc.stdout:
            line   = raw.rstrip()
            parsed = parse_line(line)
            with jobs_lock:
                j = jobs[job_id]
                j["log"].append(line)
                if len(j["log"]) > 60:
                    j["log"] = j["log"][-60:]
                if parsed:
                    j["progress"] = parsed["pct"]
                    j["speed"]    = parsed["speed"]
                    j["eta"]      = parsed["eta"]
                    j["size"]     = parsed["size"]
                low = line.lower()
                if "[download]" in line:
                    if "audio" in low:      j["phase"] = "audio"
                    elif "video" in low:    j["phase"] = "video"
                    elif parsed and j["phase"] in ("starting", "queued"):
                        j["phase"] = "downloading"
                if "[merger]" in low:
                    j["phase"] = "merging"; j["progress"] = 99.0
                    j["speed"] = ""; j["eta"] = ""
                if "[extractaudio]" in low:
                    j["phase"] = "converting"; j["progress"] = 99.0
                    j["speed"] = ""; j["eta"] = ""
                dm = _RE_DEST.search(line)
                if dm:
                    output_file = dm.group(1).strip().strip('"').strip("'")

        proc.wait()

        if proc.returncode == 0:
            if not output_file or not Path(output_file).exists():
                vid_id = jobs[job_id]["video_id"]
                files  = sorted(out_dir.glob(f"*{vid_id}*"),
                                key=lambda p: p.stat().st_mtime, reverse=True)
                for f in files:
                    if f.suffix in (".mp4", ".mp3", ".webm", ".mkv", ".m4a"):
                        output_file = str(f)
                        break
            with jobs_lock:
                j = jobs[job_id]
                j["status"]   = "done"; j["progress"] = 100.0
                j["speed"]    = ""; j["eta"] = ""; j["phase"] = "done"
                j["file"]     = output_file; j["finished"] = time.time()
            with data_lock:
                data = load_data()
                pl   = data["playlists"].get(jobs[job_id]["playlist_id"])
                if pl:
                    for v in pl["videos"]:
                        if v["id"] == jobs[job_id]["video_id"]:
                            v["downloaded"] = True
                            v["file_path"]  = output_file
                            v["quality"]    = jobs[job_id]["quality"]
                            v["audio_only"] = jobs[job_id]["audio_only"]
                            break
                    save_data(data)
        else:
            with jobs_lock:
                j    = jobs[job_id]
                hint = next((l.strip() for l in reversed(j["log"]) if l.strip()), "yt-dlp error")
                j["status"]   = "error"; j["error"]    = hint
                j["finished"] = time.time()

    except Exception as e:
        with jobs_lock:
            jobs[job_id]["status"]   = "error"
            jobs[job_id]["error"]    = str(e)
            jobs[job_id]["finished"] = time.time()

# ─── HTTP Server ──────────────────────────────────────────────────────────────

MIME_MAP = {
    ".mp4": "video/mp4", ".webm": "video/webm", ".mkv": "video/x-matroska",
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".ogg": "audio/ogg",
    ".flac": "audio/flac",
}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html, code=200):
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n)) if n else {}

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,DELETE,PUT")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _serve_file(self, fpath: Path):
        """Serve a media file with HTTP Range support for seeking."""
        suffix    = fpath.suffix.lower()
        mime_type = MIME_MAP.get(suffix, mimetypes.guess_type(str(fpath))[0] or "application/octet-stream")
        file_size = fpath.stat().st_size
        range_hdr = self.headers.get("Range")

        if range_hdr:
            # Parse "bytes=start-end"
            try:
                byte_range = range_hdr.strip().replace("bytes=", "")
                start_str, end_str = byte_range.split("-")
                start = int(start_str) if start_str else 0
                end   = int(end_str)   if end_str   else file_size - 1
                end   = min(end, file_size - 1)
            except Exception:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{file_size}")
                self.end_headers()
                return

            chunk = end - start + 1
            self.send_response(206)
            self.send_header("Content-Type", mime_type)
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
            self.send_header("Content-Length", str(chunk))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            with open(fpath, "rb") as f:
                f.seek(start)
                remaining = chunk
                while remaining:
                    buf  = f.read(min(65536, remaining))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    remaining -= len(buf)
        else:
            self.send_response(200)
            self.send_header("Content-Type", mime_type)
            self.send_header("Content-Length", str(file_size))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            with open(fpath, "rb") as f:
                while True:
                    buf = f.read(65536)
                    if not buf:
                        break
                    self.wfile.write(buf)

    # ── GET ──────────────────────────────────────────────────────────────────

    def do_GET(self):
        path = urlparse(self.path).path

        # ── Pages ──
        if path in ("/", "/player"):
            ui = Path(__file__).parent / "yt_sync_player.html"
            self.send_html(ui.read_text(encoding="utf-8") if ui.exists()
                           else "<h1>Player UI not found</h1>", 200 if ui.exists() else 404)
            return

        if path in ("/editor", "/index.html"):
            ui = Path(__file__).parent / "yt_sync_ui.html"
            self.send_html(ui.read_text(encoding="utf-8") if ui.exists()
                           else "<h1>Manager UI not found</h1>", 200 if ui.exists() else 404)
            return

        # ── Media streaming ──
        # /api/stream/<playlist_id>/<video_id>
        if path.startswith("/api/stream/"):
            parts = path.strip("/").split("/")  # ['api','stream',pl_id,vid_id]
            if len(parts) < 4:
                return self.send_json({"error": "Bad stream URL"}, 400)
            pl_id, vid_id = parts[2], parts[3]
            data  = load_data()
            pl    = data["playlists"].get(pl_id)
            if not pl:
                return self.send_json({"error": "Playlist not found"}, 404)
            video = next((v for v in pl["videos"] if v["id"] == vid_id), None)
            if not video or not video.get("file_path"):
                return self.send_json({"error": "Not downloaded"}, 404)
            fpath = Path(video["file_path"])
            if not fpath.exists():
                return self.send_json({"error": "File missing on disk"}, 404)
            self._serve_file(fpath)
            return

        # ── Thumbnails ──
        if path.startswith("/api/thumb/") and not path.startswith("/api/thumbs/"):
            video_id = path.split("/")[-1]
            if not re.match(r'^[a-zA-Z0-9_-]+$', video_id):
                self.send_response(400)
                self.end_headers()
                return
            cache_file = THUMB_CACHE_DIR / f"{video_id}.jpg"
            if not cache_file.exists():
                try:
                    urllib.request.urlretrieve(
                        f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg",
                        str(cache_file)
                    )
                except Exception:
                    self.send_response(404)
                    self.end_headers()
                    return
            body = cache_file.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/thumbs/prefetch":
            query = urlparse(self.path).query
            ids = []
            for param in query.split("&"):
                if param.startswith("ids="):
                    ids = [i.strip() for i in param[4:].split(",") if i.strip()]
                    break

            def _batch_fetch(video_ids):
                def _fetch_one(vid_id):
                    if not re.match(r'^[a-zA-Z0-9_-]+$', vid_id):
                        return
                    cf = THUMB_CACHE_DIR / f"{vid_id}.jpg"
                    if cf.exists():
                        return
                    try:
                        urllib.request.urlretrieve(
                            f"https://i.ytimg.com/vi/{vid_id}/mqdefault.jpg",
                            str(cf)
                        )
                    except Exception:
                        pass

                threads = []
                for vid_id in video_ids[:50]:
                    t = threading.Thread(target=_fetch_one, args=(vid_id,), daemon=True)
                    t.start()
                    threads.append(t)
                for t in threads:
                    t.join(timeout=15)

            threading.Thread(target=_batch_fetch, args=(ids,), daemon=True).start()
            self.send_json({"queued": len(ids)})
            return

        # ── API ──
        if path == "/api/status":
            with jobs_lock:
                active = sum(1 for j in jobs.values() if j["status"] == "running")
                queued = sum(1 for j in jobs.values() if j["status"] == "queued")
            data = load_data()
            self.send_json({
                "ytdlp":        check_ytdlp(),
                "download_dir": str(DOWNLOAD_DIR),
                "active_jobs":  active,
                "queued_jobs":  queued,
                "threads":      data.get("settings", {}).get("threads", DEFAULT_THREADS),
            })

        elif path == "/api/playlists":
            data = load_data()
            self.send_json({"playlists": list(data["playlists"].values())})

        elif path.startswith("/api/playlist/"):
            pl_id = path.split("/")[-1]
            data  = load_data()
            pl    = data["playlists"].get(pl_id)
            self.send_json(pl if pl else {"error": "Not found"}, 200 if pl else 404)

        elif path == "/api/jobs":
            with jobs_lock:
                self.send_json({"jobs": list(jobs.values())})

        elif path == "/api/settings":
            self.send_json(load_data().get("settings", {}))

        else:
            self.send_json({"error": "Not found"}, 404)

    # ── POST ─────────────────────────────────────────────────────────────────

    def do_POST(self):
        path = urlparse(self.path).path
        body = self.read_body()

        if path == "/api/playlist/add":
            url = body.get("url", "").strip()
            if not url:
                return self.send_json({"error": "URL required"}, 400)
            try:
                info  = fetch_playlist_info(url)
                pl_id = str(uuid.uuid4())[:8]
                pl    = {"id": pl_id, "url": url, "title": info["title"],
                         "videos": info["videos"], "added": time.time(), "synced": time.time()}
                with data_lock:
                    data = load_data()
                    data["playlists"][pl_id] = pl
                    save_data(data)
                self.send_json(pl)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)

        elif path == "/api/playlist/sync":
            pl_id = body.get("id")
            with data_lock:
                data = load_data()
                pl   = data["playlists"].get(pl_id)
            if not pl:
                return self.send_json({"error": "Playlist not found"}, 404)
            try:
                info         = fetch_playlist_info(pl["url"])
                existing     = {v["id"]: v for v in pl["videos"]}
                pl["videos"] = [existing[v["id"]] if v["id"] in existing else v
                                for v in info["videos"]]
                pl["title"]  = info["title"]
                pl["synced"] = time.time()
                with data_lock:
                    data = load_data()
                    data["playlists"][pl_id] = pl
                    save_data(data)
                self.send_json(pl)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)

        elif path == "/api/video/add":
            pl_id = body.get("playlist_id")
            url   = body.get("url", "").strip()
            with data_lock:
                data = load_data()
                pl   = data["playlists"].get(pl_id)
            if not pl:
                return self.send_json({"error": "Playlist not found"}, 404)
            try:
                info     = fetch_playlist_info(url)
                existing = {v["id"] for v in pl["videos"]}
                added    = [v for v in info["videos"] if v["id"] not in existing]
                pl["videos"].extend(added)
                with data_lock:
                    data = load_data()
                    data["playlists"][pl_id] = pl
                    save_data(data)
                self.send_json({"added": added})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)

        elif path == "/api/download":
            pl_id      = body.get("playlist_id")
            video_ids  = body.get("video_ids", [])
            quality    = body.get("quality", "best")
            audio_only = body.get("audio_only", False)
            if not pl_id or not video_ids:
                return self.send_json({"error": "playlist_id and video_ids required"}, 400)
            data      = load_data()
            pl        = data["playlists"].get(pl_id, {})
            title_map = {v["id"]: v.get("title", v["id"]) for v in pl.get("videos", [])}
            job_ids   = [add_job(pl_id, vid, title_map.get(vid, vid), quality, audio_only)
                         for vid in video_ids]
            self.send_json({"jobs": job_ids})

        elif path == "/api/settings/update":
            with data_lock:
                data = load_data()
                data.setdefault("settings", {}).update(body)
                save_data(data)
            self.send_json(data["settings"])

        elif path == "/api/jobs/clear":
            with jobs_lock:
                done = [jid for jid, j in jobs.items() if j["status"] in ("done", "error")]
                for jid in done:
                    del jobs[jid]
            self.send_json({"cleared": len(done)})

        elif path == "/api/video/delete-file":
            pl_id     = body.get("playlist_id")
            video_ids = body.get("video_ids", [])
            if isinstance(video_ids, str):
                video_ids = [video_ids]
            if not pl_id or not video_ids:
                return self.send_json({"error": "playlist_id and video_ids required"}, 400)
            deleted = 0
            with data_lock:
                data = load_data()
                pl   = data["playlists"].get(pl_id)
                if not pl:
                    return self.send_json({"error": "Playlist not found"}, 404)
                for v in pl["videos"]:
                    if v["id"] in video_ids and v.get("file_path"):
                        try:
                            fpath = Path(v["file_path"])
                            if fpath.exists():
                                fpath.unlink()
                                deleted += 1
                        except Exception:
                            pass
                        v["downloaded"] = False
                        v["file_path"]  = None
                        v.pop("quality", None)
                        v.pop("audio_only", None)
                save_data(data)
            self.send_json({"deleted": deleted})

        else:
            self.send_json({"error": "Not found"}, 404)

    # ── DELETE ───────────────────────────────────────────────────────────────

    def do_DELETE(self):
        path = urlparse(self.path).path

        if path.startswith("/api/playlist/"):
            pl_id = path.split("/")[-1]
            with data_lock:
                data = load_data()
                if pl_id in data["playlists"]:
                    del data["playlists"][pl_id]
                    save_data(data)
                    return self.send_json({"ok": True})
            self.send_json({"error": "Not found"}, 404)

        elif path == "/api/video/remove":
            body     = self.read_body()
            pl_id    = body.get("playlist_id")
            video_id = body.get("video_id")
            with data_lock:
                data = load_data()
                pl   = data["playlists"].get(pl_id)
                if not pl:
                    return self.send_json({"error": "Playlist not found"}, 404)
                pl["videos"] = [v for v in pl["videos"] if v["id"] != video_id]
                save_data(data)
            self.send_json({"ok": True})

        else:
            self.send_json({"error": "Not found"}, 404)

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="YT-Sync: Local YouTube Playlist Manager")
    parser.add_argument("--port",    type=int, default=7777)
    parser.add_argument("--host",    default="0.0.0.0")
    parser.add_argument("--threads", type=int, default=None,
                        help=f"Concurrent downloads (default: {DEFAULT_THREADS})")
    args = parser.parse_args()

    data    = load_data()
    threads = args.threads or data.get("settings", {}).get("threads", DEFAULT_THREADS)

    if not check_ytdlp():
        print("WARNING: yt-dlp not found. Install: pip install yt-dlp")

    print(f"YT-Sync  ->  http://{args.host}:{args.port}")
    print(f"Player   ->  http://{args.host}:{args.port}/player")
    print(f"Downloads:   {DOWNLOAD_DIR}")
    print(f"Threads:     {threads} concurrent downloads")
    print(f"FFmpeg:      {FFMPEG_LOCATION}")
    
    # Check if ffmpeg exists and is executable
    import os
    import stat
    ffmpeg_exe = os.path.join(FFMPEG_LOCATION, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    ffprobe_exe = os.path.join(FFMPEG_LOCATION, "ffprobe.exe" if os.name == "nt" else "ffprobe")
    
    if os.path.exists(ffmpeg_exe):
        # Check if it's executable
        is_executable = os.access(ffmpeg_exe, os.X_OK)
        if is_executable:
            print(f"             ✓ Found and executable: {ffmpeg_exe}")
        else:
            print(f"             ⚠ Found but NOT executable: {ffmpeg_exe}")
            # Try to auto-fix permissions
            try:
                os.chmod(ffmpeg_exe, os.stat(ffmpeg_exe).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                if os.path.exists(ffprobe_exe):
                    os.chmod(ffprobe_exe, os.stat(ffprobe_exe).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                print(f"             ✓ Auto-fixed: made ffmpeg/ffprobe executable")
            except Exception as e:
                print(f"             ✗ Could not fix permissions: {e}")
                print(f"             Manual fix: chmod +x {ffmpeg_exe}")
    else:
        print(f"             ✗ Not found: {ffmpeg_exe}")
        print(f"             Place ffmpeg binaries in: {FFMPEG_LOCATION}")
    
    print("Press Ctrl+C to stop.\n")

    start_thread_pool(threads)
    server = HTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nYT-Sync stopped.")

if __name__ == "__main__":
    main()
