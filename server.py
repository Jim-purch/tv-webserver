#!/usr/bin/env python3
"""
TV Web Server - Auto-rotating webpage display, fully configurable
through the built-in web admin panel (/admin).
"""

import hashlib
import re
import json
import os
import signal
import subprocess
import sys
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse
from urllib.request import urlopen, Request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
STATIC_DIR = os.path.join(BASE_DIR, "static")
CACHE_DIR = os.path.join(BASE_DIR, "cache")

DEFAULT_PORT = 8788

DEFAULT_CONFIG = {
    "urls": [],
    "interval": 10,
    "fullscreen_selectors": [
        "全屏显示",
        "全屏",
        "fullscreen",
        "Fullscreen",
        "全螢幕",
    ],
    "music_url": "",
}

# --- Config state (thread-safe, version counter for multi-client sync) ---
_config_lock = threading.Lock()
_config_version = 0


def get_config_version():
    """Get the current config version (bumped on every save)."""
    with _config_lock:
        return _config_version


# --- Music state (thread-safe, persistent with version counter) ---
_music_lock = threading.Lock()
_music_state = "stopped"  # "playing" | "stopped"
_music_cached_url = ""  # The URL of the currently cached/playing music
_music_version = 0  # Monotonic counter, incremented on every state change
_music_celebration_data = {}  # {"salesperson": "", "amount": "", "deal_time": ""}


def set_music_command(cmd, cached_url="", celebration_data=None):
    """Set the music state for clients to poll. cmd: 'play' or 'stop'."""
    global _music_state, _music_cached_url, _music_version, _music_celebration_data
    with _music_lock:
        if cmd == "play":
            _music_state = "playing"
            if cached_url:
                _music_cached_url = cached_url
            _music_celebration_data = celebration_data or {}
        elif cmd == "stop":
            _music_state = "stopped"
            _music_celebration_data = {}
        _music_version += 1


def get_music_state():
    """Get the current music state (persistent, not one-shot)."""
    with _music_lock:
        return _music_state, _music_cached_url, _music_version, _music_celebration_data.copy()


# --- Music caching ---
def ensure_cache_dir():
    """Ensure the cache directory exists."""
    os.makedirs(CACHE_DIR, exist_ok=True)


def _url_to_cache_filename(url):
    """Generate a stable cache filename from a URL."""
    url_hash = hashlib.md5(url.encode()).hexdigest()
    # Try to preserve the file extension
    parsed = urlparse(url)
    ext = os.path.splitext(parsed.path)[1]
    if not ext or len(ext) > 10:
        ext = ".mp3"
    return f"music_{url_hash}{ext}"


def get_cached_music_path(url):
    """Return the cached file path if it exists, else None."""
    if not url:
        return None
    filename = _url_to_cache_filename(url)
    path = os.path.join(CACHE_DIR, filename)
    if os.path.exists(path):
        return path
    return None


def download_music(url, progress_callback=None):
    """Download music from URL to cache. Returns (success, cache_filename)."""
    ensure_cache_dir()
    filename = _url_to_cache_filename(url)
    path = os.path.join(CACHE_DIR, filename)

    # Already cached
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return True, filename

    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=60) as resp:
            total = resp.headers.get("Content-Length")
            total = int(total) if total else None
            downloaded = 0
            with open(path, "wb") as f:
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback and total:
                        progress_callback(downloaded, total)
        return True, filename
    except Exception as e:
        # Clean up partial file
        if os.path.exists(path):
            os.remove(path)
        print(f"Music download failed: {e}")
        return False, str(e)


def load_config():
    """Load configuration from JSON file."""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
            # Ensure fullscreen_selectors exists
            if "fullscreen_selectors" not in config:
                config["fullscreen_selectors"] = DEFAULT_CONFIG["fullscreen_selectors"]
            if "music_url" not in config:
                config["music_url"] = ""
            return config
    except (FileNotFoundError, json.JSONDecodeError):
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()


def save_config(config):
    """Save configuration to JSON file and bump the sync version."""
    global _config_version
    with _config_lock:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        _config_version += 1


def update_frpc_port(port):
    """Update the localPort in frpc.toml to match the server port."""
    frpc_conf = os.path.join(BASE_DIR, "frpc.toml")
    try:
        with open(frpc_conf, "r", encoding="utf-8") as f:
            content = f.read()
        new_content = re.sub(r'(localPort\s*=\s*)\d+', f'\\g<1>{port}', content)
        if new_content != content:
            with open(frpc_conf, "w", encoding="utf-8") as f:
                f.write(new_content)
            print(f"Updated frpc.toml localPort to {port}")
        return True
    except Exception as e:
        print(f"Failed to update frpc.toml: {e}")
        return False


# --- frpc tunnel process management (moved from GUI, thread-safe) ---
_frpc_lock = threading.Lock()
_frpc_process = None


def _frpc_paths():
    """Return (frpc_binary_path, frpc_config_path)."""
    frpc_ext = ".exe" if os.name == "nt" else ""
    return (
        os.path.join(BASE_DIR, f"frpc{frpc_ext}"),
        os.path.join(BASE_DIR, "frpc.toml"),
    )


def start_frpc_process(server_port):
    """Start the frpc subprocess, syncing localPort to this server's port.

    Returns (success, message).
    """
    global _frpc_process
    with _frpc_lock:
        if _frpc_process is not None and _frpc_process.poll() is None:
            return False, "frpc 已在运行中"

        frpc_bin, frpc_conf = _frpc_paths()
        if not os.path.exists(frpc_bin):
            return False, f"找不到 frpc 可执行文件:\n{frpc_bin}"
        if not os.path.exists(frpc_conf):
            return False, f"找不到 frpc 配置文件:\n{frpc_conf}"

        # Sync frpc.toml localPort so the tunnel matches this server's port
        update_frpc_port(server_port)

        try:
            creationflags = 0
            if os.name == "nt":
                creationflags = 0x08000000  # CREATE_NO_WINDOW

            proc = subprocess.Popen(
                [frpc_bin, "-c", frpc_conf],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=BASE_DIR,
                creationflags=creationflags,
            )
        except Exception as e:
            return False, f"启动 frpc 失败:\n{e}"

        _frpc_process = proc

    def read_output():
        try:
            for line in iter(proc.stdout.readline, b""):
                print(f"[frpc] {line.decode('utf-8', errors='replace').rstrip()}")
        except Exception:
            pass

    threading.Thread(target=read_output, daemon=True).start()
    return True, "frpc 已启动"


def stop_frpc_process():
    """Terminate the frpc subprocess if running. Safe to call repeatedly.

    Returns (success, message).
    """
    global _frpc_process
    with _frpc_lock:
        proc = _frpc_process
        if proc is None or proc.poll() is not None:
            _frpc_process = None
            return False, "frpc 未在运行"
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        except Exception:
            pass
        _frpc_process = None
    return True, "frpc 已停止"


def get_frpc_status():
    """Return {"running": bool, "tunnel_url": str} parsed from frpc.toml."""
    with _frpc_lock:
        running = _frpc_process is not None and _frpc_process.poll() is None
    _, frpc_conf = _frpc_paths()
    try:
        with open(frpc_conf, "r", encoding="utf-8") as f:
            content = f.read()
        addr = re.search(r'serverAddr\s*=\s*["\']([^"\']+)["\']', content)
        rport = re.search(r'remotePort\s*=\s*(\d+)', content)
        tunnel_url = (
            f"http://{addr.group(1)}:{rport.group(1)}" if addr and rport else ""
        )
        return {"running": running, "tunnel_url": tunnel_url}
    except Exception:
        return {"running": running, "tunnel_url": ""}
    try:
        with open(frpc_conf, "r", encoding="utf-8") as f:
            content = f.read()
        new_content = re.sub(r'(localPort\s*=\s*)\d+', f'\\g<1>{port}', content)
        if new_content != content:
            with open(frpc_conf, "w", encoding="utf-8") as f:
                f.write(new_content)
            print(f"Updated frpc.toml localPort to {port}")
        return True
    except Exception as e:
        print(f"Failed to update frpc.toml: {e}")
        return False


class TVWebHandler(SimpleHTTPRequestHandler):
    """Custom HTTP request handler for the TV web server."""

    # Use HTTP/1.1 to work properly behind Nginx reverse proxy
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            self._serve_file("index.html", "text/html")
        elif path == "/admin" or path == "/admin.html":
            self._serve_file("admin.html", "text/html")
        elif path == "/api/config":
            cfg = load_config()
            self._serve_json({**cfg, "version": get_config_version()})
        elif path == "/api/config_version":
            self._serve_json({"version": get_config_version()})
        elif path == "/api/music_command":
            state, cached_url, version, celebration = get_music_state()
            self._serve_json({"state": state, "music_url": cached_url, "version": version, "celebration": celebration})
        elif path == "/api/frpc_status":
            self._serve_json(get_frpc_status())
        elif path.startswith("/cache/"):
            # Serve cached music files
            filename = path.split("/cache/", 1)[1]
            # Sanitize filename to prevent directory traversal
            filename = os.path.basename(filename)
            file_path = os.path.join(CACHE_DIR, filename)
            if os.path.exists(file_path):
                ext = os.path.splitext(file_path)[1].lower()
                audio_types = {
                    ".mp3": "audio/mpeg",
                    ".wav": "audio/wav",
                    ".ogg": "audio/ogg",
                    ".m4a": "audio/mp4",
                    ".aac": "audio/aac",
                    ".flac": "audio/flac",
                    ".wma": "audio/x-ms-wma",
                }
                ct = audio_types.get(ext, "application/octet-stream")
                self._serve_file_path(file_path, ct)
            else:
                self.send_error(404)
        elif path.startswith("/static/"):
            # Serve static files
            file_path = os.path.join(BASE_DIR, path.lstrip("/"))
            if os.path.exists(file_path):
                ext = os.path.splitext(file_path)[1]
                content_types = {
                    ".css": "text/css",
                    ".js": "application/javascript",
                    ".png": "image/png",
                    ".jpg": "image/jpeg",
                    ".svg": "image/svg+xml",
                    ".ico": "image/x-icon",
                }
                ct = content_types.get(ext, "application/octet-stream")
                self._serve_file_path(file_path, ct)
            else:
                self.send_error(404)
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/config":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                new_config = json.loads(body.decode("utf-8"))
                # Validate
                if "urls" not in new_config or "interval" not in new_config:
                    self._serve_json({"error": "Missing required fields"}, 400)
                    return
                new_config["interval"] = max(1, int(new_config["interval"]))
                if "fullscreen_selectors" not in new_config:
                    new_config["fullscreen_selectors"] = DEFAULT_CONFIG[
                        "fullscreen_selectors"
                    ]
                if "music_url" not in new_config:
                    new_config["music_url"] = load_config().get("music_url", "")
                save_config(new_config)
                self._serve_json({"status": "ok", "config": new_config, "version": get_config_version()})
            except (json.JSONDecodeError, ValueError) as e:
                self._serve_json({"error": str(e)}, 400)
        elif path == "/api/music_url":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body.decode("utf-8"))
                music_url = data.get("music_url", "").strip()
                config = load_config()
                config["music_url"] = music_url
                save_config(config)
                self._serve_json({"status": "ok", "music_url": music_url})
            except (json.JSONDecodeError, ValueError) as e:
                self._serve_json({"error": str(e)}, 400)
        elif path == "/api/remote/music":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body.decode("utf-8"))
                action = data.get("action", "").strip().lower()

                if action == "play":
                    # Optionally accept a music_url in the request body
                    music_url = data.get("music_url", "").strip()
                    if not music_url:
                        cfg = load_config()
                        music_url = cfg.get("music_url", "")
                    if not music_url:
                        self._serve_json(
                            {
                                "status": "error",
                                "action": "play",
                                "message": "未配置音乐链接，请先在config或请求中提供music_url",
                            },
                            400,
                        )
                        return

                    # Extract celebration data
                    celebration_data = {
                        "salesperson": data.get("salesperson", "").strip(),
                        "amount": data.get("amount", "").strip(),
                        "deal_time": data.get("deal_time", "").strip(),
                    }

                    # Save music URL to config
                    cfg = load_config()
                    cfg["music_url"] = music_url
                    save_config(cfg)

                    # Download synchronously (mirrors GUI play_music logic)
                    success, result = download_music(music_url)
                    if success:
                        cache_url = f"/cache/{result}"
                        set_music_command("play", cache_url, celebration_data)
                        self._serve_json(
                            {
                                "status": "ok",
                                "action": "play",
                                "message": "音乐播放指令已发送",
                                "music_url": music_url,
                                "cache_url": cache_url,
                                "celebration": celebration_data,
                            }
                        )
                    else:
                        self._serve_json(
                            {
                                "status": "error",
                                "action": "play",
                                "message": f"音乐下载失败: {result}",
                            },
                            500,
                        )

                elif action == "stop":
                    set_music_command("stop")
                    self._serve_json(
                        {
                            "status": "ok",
                            "action": "stop",
                            "message": "音乐已停止",
                        }
                    )

                else:
                    self._serve_json(
                        {
                            "status": "error",
                            "message": f"无效的action: '{action}'，支持: play, stop",
                        },
                        400,
                    )
            except json.JSONDecodeError:
                self._serve_json(
                    {"status": "error", "message": "请求体必须是有效的JSON"},
                    400,
                )
            except Exception as e:
                self._serve_json(
                    {"status": "error", "message": f"服务器内部错误: {str(e)}"},
                    500,
                )
        elif path == "/api/frpc_control":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body.decode("utf-8"))
                action = data.get("action", "").strip().lower()

                if action == "start":
                    success, message = start_frpc_process(self.server.server_port)
                    self._serve_json(
                        {"status": "ok" if success else "error", "action": "start", "message": message},
                        200 if success else 400,
                    )
                elif action == "stop":
                    success, message = stop_frpc_process()
                    self._serve_json(
                        {"status": "ok" if success else "error", "action": "stop", "message": message},
                        200 if success else 400,
                    )
                else:
                    self._serve_json(
                        {"status": "error", "message": f"无效的action: '{action}'，支持: start, stop"},
                        400,
                    )
            except json.JSONDecodeError:
                self._serve_json(
                    {"status": "error", "message": "请求体必须是有效的JSON"},
                    400,
                )
        else:
            self.send_error(404)

    def _serve_json(self, data, status=200):
        content = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(content))
        self.end_headers()
        self.wfile.write(content)

    def _serve_file(self, filename, content_type):
        file_path = os.path.join(STATIC_DIR, filename)
        self._serve_file_path(file_path, content_type)

    def _serve_file_path(self, file_path, content_type):
        try:
            with open(file_path, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", len(content))
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_error(404)

    def log_message(self, format, *args):
        """Custom log format."""
        print(f"[{self.log_date_time_string()}] {format % args}")


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle requests in separate threads for concurrent access."""
    daemon_threads = True


def run_server(host="0.0.0.0", port=DEFAULT_PORT):
    """Start the HTTP server."""

    def _graceful_exit(signum, frame):
        print(f"\nReceived signal {signum}, shutting down...")
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _graceful_exit)

    server = ThreadingHTTPServer((host, port), TVWebHandler)
    print(f"=" * 50)
    print(f"  TV Web Server")
    print(f"  Display:  http://localhost:{port}/")
    print(f"  Admin:    http://localhost:{port}/admin")
    print("=" * 50)
    try:
        server.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        # Clean up the frpc tunnel process on exit
        stop_frpc_process()
        server.server_close()
        print("\nServer stopped.")


if __name__ == "__main__":
    port = DEFAULT_PORT
    for i, arg in enumerate(sys.argv):
        if arg == "--port" and i + 1 < len(sys.argv):
            port = int(sys.argv[i + 1])
    run_server("0.0.0.0", port)
