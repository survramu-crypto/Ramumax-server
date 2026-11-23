"""
Video App Backend Server (Python, no extra packages needed)

Features:
  GET /api/home                -> home page feed (latest + popular videos)
  GET /api/search?q=text       -> search videos by title/description/tags
  GET /api/videos/<id>         -> single video details
  GET /stream/<id>             -> play video (supports Range for seeking)
  GET /download/<id>           -> download video file
  GET /thumbs/<file>           -> thumbnail images

Run:   python server.py
Then open: http://localhost:8000/api/home

Put your .mp4 files in the  videos/  folder and thumbnails in  thumbs/ .
On startup the server auto-adds every new .mp4 in videos/ to the database.
"""
import json
import mimetypes
import os
import re
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VIDEO_DIR = os.path.join(BASE_DIR, "videos")
THUMB_DIR = os.path.join(BASE_DIR, "thumbs")
DB_PATH = os.path.join(BASE_DIR, "videos.db")
PORT = int(os.environ.get("PORT", 8000))

os.makedirs(VIDEO_DIR, exist_ok=True)
os.makedirs(THUMB_DIR, exist_ok=True)


# ---------------------------------------------------------------- database
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                tags TEXT DEFAULT '',
                filename TEXT UNIQUE NOT NULL,
                thumbnail TEXT DEFAULT '',
                views INTEGER DEFAULT 0,
                downloads INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )"""
        )


def scan_videos():
    """Add any new .mp4/.webm/.mkv file from videos/ into the database."""
    exts = (".mp4", ".webm", ".mkv", ".mov")
    with db() as c:
        for name in sorted(os.listdir(VIDEO_DIR)):
            if not name.lower().endswith(exts):
                continue
            exists = c.execute("SELECT 1 FROM videos WHERE filename=?", (name,)).fetchone()
            if exists:
                continue
            stem = os.path.splitext(name)[0]
            title = stem.replace("_", " ").replace("-", " ").title()
            thumb = ""
            for ext in (".jpg", ".jpeg", ".png", ".webp"):
                if os.path.exists(os.path.join(THUMB_DIR, stem + ext)):
                    thumb = stem + ext
                    break
            c.execute(
                "INSERT INTO videos (title, tags, filename, thumbnail) VALUES (?,?,?,?)",
                (title, stem.replace("_", ",").replace("-", ","), name, thumb),
            )


def seed_demo():
    """If no videos yet, add demo rows so the app has something to show."""
    with db() as c:
        n = c.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        if n:
            return
        demo = [
            ("Python Basics Tutorial", "Learn Python from scratch", "python,coding,tutorial", "demo1.mp4"),
            ("Android App Development", "Build your first Android app", "android,kotlin,app", "demo2.mp4"),
            ("College Fest Highlights", "Best moments of annual fest", "college,fest,vlog", "demo3.mp4"),
            ("Cricket Match Highlights", "Top moments of the match", "cricket,sports", "demo4.mp4"),
        ]
        for t, d, tg, f in demo:
            c.execute("INSERT INTO videos (title, description, tags, filename) VALUES (?,?,?,?)", (t, d, tg, f))


# ---------------------------------------------------------------- helpers
def video_json(row, host):
    return {
        "id": row["id"],
        "title": row["title"],
        "description": row["description"],
        "tags": [t for t in row["tags"].split(",") if t],
        "views": row["views"],
        "downloads": row["downloads"],
        "created_at": row["created_at"],
        "thumbnail_url": f"{host}/thumbs/{row['thumbnail']}" if row["thumbnail"] else None,
        "stream_url": f"{host}/stream/{row['id']}",
        "download_url": f"{host}/download/{row['id']}",
    }


_trend_cache = {"t": 0, "data": None}


def youtube_trending(api_key):
    """Official YouTube Data API (metadata only), cached for 10 minutes."""
    import time
    import urllib.request
    if _trend_cache["data"] and time.time() - _trend_cache["t"] < 600:
        return _trend_cache["data"]
    url = ("https://www.googleapis.com/youtube/v3/videos?part=snippet,statistics"
           "&chart=mostPopular&regionCode=IN&maxResults=25&key=" + api_key)
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            items = json.loads(resp.read().decode("utf-8")).get("items", [])
        _trend_cache.update(t=time.time(), data=items)
        return items
    except Exception as e:  # noqa
        print("YouTube API error:", e, file=sys.stderr)
        return _trend_cache["data"] or []


# ---------------------------------------------------------------- handler
class Handler(BaseHTTPRequestHandler):
    server_version = "VideoServer/1.0"

    # ---- small utilities
    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def host(self):
        """Base URL with scheme (works behind Render's HTTPS proxy too)."""
        scheme = self.headers.get("X-Forwarded-Proto", "http").split(",")[0].strip()
        return f"{scheme}://{self.headers.get('Host', f'localhost:{PORT}')}"

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Range, Content-Type")
        self.end_headers()

    # ---- routing
    def do_GET(self):
        url = urlparse(self.path)
        path = url.path.rstrip("/") or "/"
        qs = parse_qs(url.query)
        try:
            if path == "/":
                return self.send_json({"status": "ok", "message": "Video server running",
                                       "endpoints": ["/api/home", "/api/search?q=", "/api/videos/<id>",
                                                     "/stream/<id>", "/download/<id>"]})
            if path == "/health":
                return self.send_json({"status": "online"})
            if path == "/trending":
                return self.api_trending()
            if path == "/downloadable-videos":
                return self.api_downloadable(qs)
            if path == "/api/home":
                return self.api_home()
            if path == "/api/search":
                return self.api_search(qs)
            m = re.fullmatch(r"/api/videos/(\d+)", path)
            if m:
                return self.api_video(int(m.group(1)))
            m = re.fullmatch(r"/(stream|download)/(\d+)", path)
            if m:
                return self.serve_video(int(m.group(2)), download=(m.group(1) == "download"))
            if path.startswith("/thumbs/"):
                return self.serve_thumb(unquote(path[len("/thumbs/"):]))
            self.send_json({"error": "Not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client closed connection (normal while seeking/cancelling)
        except Exception as e:  # noqa
            print("ERROR:", e, file=sys.stderr)
            self.send_json({"error": "Server error"}, 500)

    # ---- VIDMAX app endpoints
    def api_trending(self):
        """YouTube-API-shaped list. Uses the official YouTube Data API if
        YOUTUBE_API_KEY is set (metadata only), else your own library."""
        key = os.environ.get("YOUTUBE_API_KEY", "").strip()
        if key:
            items = youtube_trending(key)
            if items:
                return self.send_json(items)
        h = self.host()
        with db() as c:
            rows = c.execute("SELECT * FROM videos ORDER BY views DESC, id DESC LIMIT 25").fetchall()
        out = []
        for r in rows:
            out.append({
                "id": str(r["id"]),
                "snippet": {
                    "title": r["title"],
                    "channelTitle": "VIDMAX",
                    "thumbnails": {"high": {"url": f"{h}/thumbs/{r['thumbnail']}" if r["thumbnail"] else ""}},
                },
                "statistics": {"viewCount": str(r["views"])},
                "stream_url": f"{h}/stream/{r['id']}",
            })
        self.send_json(out)

    def api_downloadable(self, qs):
        """Search your own library; returns formats the app can download."""
        q = (qs.get("q", [""])[0]).strip()
        like = f"%{q}%"
        with db() as c:
            rows = c.execute(
                """SELECT * FROM videos
                   WHERE title LIKE ? OR description LIKE ? OR tags LIKE ?
                   ORDER BY views DESC, id DESC LIMIT 30""",
                (like, like, like),
            ).fetchall() if q else []
        h = self.host()
        results = []
        for r in rows:
            path = os.path.join(VIDEO_DIR, r["filename"])
            size_mb = round(os.path.getsize(path) / 1048576, 1) if os.path.isfile(path) else 0
            ext = os.path.splitext(r["filename"])[1].lstrip(".").lower() or "mp4"
            results.append({
                "id": str(r["id"]),
                "title": r["title"],
                "thumbnail": f"{h}/thumbs/{r['thumbnail']}" if r["thumbnail"] else "",
                "qualities": [{
                    "type": "audio" if ext in ("mp3", "m4a") else "video",
                    "quality": "Original",
                    "format": ext,
                    "size_mb": size_mb,
                    "url": f"{h}/download/{r['id']}",
                    "stream_url": f"{h}/stream/{r['id']}",
                }],
            })
        self.send_json({"query": q, "results": results})

    # ---- API endpoints
    def api_home(self):
        h = self.host()
        with db() as c:
            latest = c.execute("SELECT * FROM videos ORDER BY id DESC LIMIT 10").fetchall()
            popular = c.execute("SELECT * FROM videos ORDER BY views DESC, id DESC LIMIT 10").fetchall()
            total = c.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        self.send_json({
            "total_videos": total,
            "sections": [
                {"title": "Latest", "videos": [video_json(r, h) for r in latest]},
                {"title": "Popular", "videos": [video_json(r, h) for r in popular]},
            ],
        })

    def api_search(self, qs):
        q = (qs.get("q", [""])[0]).strip()
        page = max(int(qs.get("page", ["1"])[0]), 1)
        size = min(max(int(qs.get("size", ["20"])[0]), 1), 50)
        if not q:
            return self.send_json({"query": "", "count": 0, "results": []})
        like = f"%{q}%"
        with db() as c:
            rows = c.execute(
                """SELECT * FROM videos
                   WHERE title LIKE ? OR description LIKE ? OR tags LIKE ?
                   ORDER BY views DESC, id DESC LIMIT ? OFFSET ?""",
                (like, like, like, size, (page - 1) * size),
            ).fetchall()
        h = self.host()
        self.send_json({"query": q, "page": page, "count": len(rows),
                        "results": [video_json(r, h) for r in rows]})

    def api_video(self, vid):
        with db() as c:
            row = c.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone()
        if not row:
            return self.send_json({"error": "Video not found"}, 404)
        self.send_json(video_json(row, self.host()))

    # ---- file serving
    def serve_thumb(self, name):
        full = os.path.normpath(os.path.join(THUMB_DIR, name))
        if not full.startswith(THUMB_DIR) or not os.path.isfile(full):
            return self.send_json({"error": "Not found"}, 404)
        self.send_file(full, download=False, ranged=False)

    def serve_video(self, vid, download):
        with db() as c:
            row = c.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone()
            if not row:
                return self.send_json({"error": "Video not found"}, 404)
            full = os.path.normpath(os.path.join(VIDEO_DIR, row["filename"]))
            if not full.startswith(VIDEO_DIR) or not os.path.isfile(full):
                return self.send_json({"error": "Video file missing on server"}, 404)
            # count a view only on the first request (no Range or Range starts at 0)
            rng = self.headers.get("Range")
            if not rng or rng.startswith("bytes=0-"):
                col = "downloads" if download else "views"
                c.execute(f"UPDATE videos SET {col}={col}+1 WHERE id=?", (vid,))
        self.send_file(full, download=download, ranged=True, download_name=row["filename"])

    def send_file(self, full, download, ranged, download_name=None):
        size = os.path.getsize(full)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range")
        if ranged and rng:
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())
            if m:
                s, e = m.groups()
                if s == "" and e:          # last N bytes
                    start = max(size - int(e), 0)
                else:
                    start = int(s or 0)
                    if e:
                        end = min(int(e), size - 1)
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Access-Control-Allow-Origin", "*")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.end_headers()
        with open(full, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {self.address_string()} {fmt % args}")


if __name__ == "__main__":
    init_db()
    scan_videos()
    seed_demo()
    print(f"Server chal raha hai: http://0.0.0.0:{PORT}")
    print("Android emulator se use karo: http://10.0.2.2:%d" % PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
