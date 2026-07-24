#!/usr/bin/env python3
"""
TV Web Server - Auto-rotating webpage display with GUI configuration.
"""

import hashlib
import re
import json
import os
import signal
import subprocess
import sys
import threading
import webbrowser
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen, Request
from urllib.error import URLError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
STATIC_DIR = os.path.join(BASE_DIR, "static")
CACHE_DIR = os.path.join(BASE_DIR, "cache")

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


def read_frpc_port():
    """Read the localPort from frpc.toml. Returns the port or 80 as default."""
    frpc_conf = os.path.join(BASE_DIR, "frpc.toml")
    try:
        with open(frpc_conf, "r", encoding="utf-8") as f:
            content = f.read()
        match = re.search(r'localPort\s*=\s*(\d+)', content)
        if match:
            return int(match.group(1))
    except Exception:
        pass
    return 80


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


def run_server(host="0.0.0.0", port=80):
    """Start the HTTP server."""
    server = ThreadingHTTPServer((host, port), TVWebHandler)
    print(f"=" * 50)
    print(f"  TV Web Server")
    print(f"  Display:  http://localhost:{port}/")
    print(f"  Admin:    http://localhost:{port}/admin")
    print(f"=" * 50)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
        server.server_close()


def run_gui():
    """Launch the Tkinter GUI for configuration."""
    try:
        import tkinter as tk
        from tkinter import ttk, messagebox, simpledialog
    except ImportError:
        print("Tkinter is not available. Use the web admin panel at /admin instead.")
        return

    config = load_config()

    root = tk.Tk()
    root.title("TV Web Server - 配置管理")
    root.geometry("700x850")
    root.configure(bg="#1a1a2e")

    # Style
    style = ttk.Style()
    style.theme_use("clam")
    style.configure("TFrame", background="#1a1a2e")
    style.configure(
        "TLabel", background="#1a1a2e", foreground="#e0e0e0", font=("Helvetica", 11)
    )
    style.configure(
        "Title.TLabel",
        background="#1a1a2e",
        foreground="#00d4ff",
        font=("Helvetica", 18, "bold"),
    )
    style.configure(
        "TButton",
        font=("Helvetica", 10),
        padding=6,
    )
    style.configure(
        "Accent.TButton",
        font=("Helvetica", 11, "bold"),
        padding=8,
    )

    # Title
    title_frame = ttk.Frame(root)
    title_frame.pack(fill="x", padx=20, pady=(15, 5))
    ttk.Label(title_frame, text="📺 TV Web Server 配置", style="Title.TLabel").pack(
        anchor="w"
    )

    # URL list section
    url_frame = ttk.Frame(root)
    url_frame.pack(fill="both", expand=True, padx=20, pady=10)

    ttk.Label(url_frame, text="网页链接列表:").pack(anchor="w", pady=(0, 5))

    list_frame = ttk.Frame(url_frame)
    list_frame.pack(fill="both", expand=True)

    url_listbox = tk.Listbox(
        list_frame,
        height=5,
        bg="#16213e",
        fg="#e0e0e0",
        selectbackground="#0f3460",
        selectforeground="#00d4ff",
        font=("Consolas", 11),
        relief="flat",
        highlightthickness=1,
        highlightcolor="#0f3460",
    )
    scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=url_listbox.yview)
    url_listbox.configure(yscrollcommand=scrollbar.set)
    url_listbox.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")

    # Populate list
    for url in config.get("urls", []):
        url_listbox.insert("end", url)

    # URL buttons
    btn_frame = ttk.Frame(url_frame)
    btn_frame.pack(fill="x", pady=(8, 0))

    # --- GUI config sync state (track local edits so we don't clobber them) ---
    _gui_config_dirty = tk.BooleanVar(value=False)
    _gui_loading_config = [False]  # guard so programmatic reloads don't mark dirty
    _gui_last_config_version = [get_config_version()]

    def mark_gui_dirty():
        if not _gui_loading_config[0]:
            _gui_config_dirty.set(True)

    def add_url():
        url = simpledialog.askstring("添加链接", "请输入网页链接:", parent=root)
        if url and url.strip():
            url_listbox.insert("end", url.strip())
            mark_gui_dirty()

    def remove_url():
        sel = url_listbox.curselection()
        if sel:
            url_listbox.delete(sel[0])
            mark_gui_dirty()
        else:
            messagebox.showwarning("提示", "请先选择要删除的链接")

    def move_up():
        sel = url_listbox.curselection()
        if sel and sel[0] > 0:
            idx = sel[0]
            text = url_listbox.get(idx)
            url_listbox.delete(idx)
            url_listbox.insert(idx - 1, text)
            url_listbox.selection_set(idx - 1)
            mark_gui_dirty()

    def move_down():
        sel = url_listbox.curselection()
        if sel and sel[0] < url_listbox.size() - 1:
            idx = sel[0]
            text = url_listbox.get(idx)
            url_listbox.delete(idx)
            url_listbox.insert(idx + 1, text)
            url_listbox.selection_set(idx + 1)
            mark_gui_dirty()

    def edit_url():
        sel = url_listbox.curselection()
        if sel:
            old = url_listbox.get(sel[0])
            new = simpledialog.askstring(
                "编辑链接", "修改网页链接:", initialvalue=old, parent=root
            )
            if new and new.strip():
                url_listbox.delete(sel[0])
                url_listbox.insert(sel[0], new.strip())
                mark_gui_dirty()
        else:
            messagebox.showwarning("提示", "请先选择要编辑的链接")

    ttk.Button(btn_frame, text="➕ 添加", command=add_url).pack(
        side="left", padx=(0, 5)
    )
    ttk.Button(btn_frame, text="✏️ 编辑", command=edit_url).pack(
        side="left", padx=(0, 5)
    )
    ttk.Button(btn_frame, text="🗑️ 删除", command=remove_url).pack(
        side="left", padx=(0, 5)
    )
    ttk.Button(btn_frame, text="⬆️ 上移", command=move_up).pack(
        side="left", padx=(0, 5)
    )
    ttk.Button(btn_frame, text="⬇️ 下移", command=move_down).pack(
        side="left", padx=(0, 5)
    )

    # Interval setting
    interval_frame = ttk.Frame(root)
    interval_frame.pack(fill="x", padx=20, pady=10)

    ttk.Label(interval_frame, text="自动切换间隔 (秒):").pack(side="left")
    interval_var = tk.StringVar(value=str(config.get("interval", 10)))
    interval_entry = tk.Entry(
        interval_frame,
        textvariable=interval_var,
        width=8,
        bg="#16213e",
        fg="#00d4ff",
        font=("Consolas", 13),
        relief="flat",
        insertbackground="#00d4ff",
    )
    interval_entry.pack(side="left", padx=10)

    # --- Music section ---
    music_sep = ttk.Separator(root, orient="horizontal")
    music_sep.pack(fill="x", padx=20, pady=(10, 5))

    music_title_frame = ttk.Frame(root)
    music_title_frame.pack(fill="x", padx=20, pady=(0, 5))
    ttk.Label(
        music_title_frame,
        text="🎵 音乐播放",
        font=("Helvetica", 14, "bold"),
        foreground="#00d4ff",
    ).pack(anchor="w")

    music_url_frame = ttk.Frame(root)
    music_url_frame.pack(fill="x", padx=20, pady=(0, 5))

    ttk.Label(music_url_frame, text="音乐链接:").pack(side="left")
    music_url_var = tk.StringVar(value=config.get("music_url", ""))
    music_url_entry = tk.Entry(
        music_url_frame,
        textvariable=music_url_var,
        bg="#16213e",
        fg="#e0e0e0",
        font=("Consolas", 11),
        relief="flat",
        insertbackground="#00d4ff",
    )
    music_url_entry.pack(side="left", fill="x", expand=True, padx=(10, 0))

    # Track interval/music edits so config sync won't clobber in-progress typing
    interval_var.trace_add("write", lambda *a: mark_gui_dirty())
    music_url_var.trace_add("write", lambda *a: mark_gui_dirty())

    music_ctrl_frame = ttk.Frame(root)
    music_ctrl_frame.pack(fill="x", padx=20, pady=(5, 5))

    music_status_var = tk.StringVar(value="● 已停止")
    music_status_label = ttk.Label(
        music_ctrl_frame,
        textvariable=music_status_var,
        foreground="#ff6b6b",
        font=("Helvetica", 11),
    )
    music_status_label.pack(side="right", padx=10)

    def play_music():
        """Download (if needed) and issue play command to clients."""
        url = music_url_var.get().strip()
        if not url:
            messagebox.showwarning("提示", "请先输入音乐链接")
            return

        # Save music URL to config
        cfg = load_config()
        cfg["music_url"] = url
        save_config(cfg)

        music_status_var.set("● 下载中...")
        music_status_label.configure(foreground="#ffaa00")
        root.update_idletasks()

        def do_download():
            success, result = download_music(url)
            if success:
                cache_url = f"/cache/{result}"
                set_music_command("play", cache_url)
                root.after(0, lambda: music_status_var.set("● 播放中"))
                root.after(
                    0,
                    lambda: music_status_label.configure(foreground="#00ff88"),
                )
            else:
                root.after(
                    0,
                    lambda: messagebox.showerror(
                        "错误", f"音乐下载失败:\n{result}"
                    ),
                )
                root.after(0, lambda: music_status_var.set("● 下载失败"))
                root.after(
                    0,
                    lambda: music_status_label.configure(foreground="#ff6b6b"),
                )

        threading.Thread(target=do_download, daemon=True).start()

    def stop_music():
        """Issue stop command to clients."""
        set_music_command("stop")
        music_status_var.set("● 已停止")
        music_status_label.configure(foreground="#ff6b6b")

    ttk.Button(
        music_ctrl_frame,
        text="▶️ 播放音乐",
        style="Accent.TButton",
        command=play_music,
    ).pack(side="left", padx=(0, 5))

    ttk.Button(
        music_ctrl_frame,
        text="⏹️ 停止音乐",
        style="Accent.TButton",
        command=stop_music,
    ).pack(side="left", padx=(0, 5))

    # --- GUI music state sync (detect remote API changes) ---
    _gui_last_music_version = [-1]  # mutable container for closure

    def sync_music_status():
        """Poll the global music state and update GUI labels accordingly."""
        state, cached_url, version, _celebration = get_music_state()
        if version != _gui_last_music_version[0]:
            _gui_last_music_version[0] = version
            if state == "playing":
                music_status_var.set("● 播放中")
                music_status_label.configure(foreground="#00ff88")
            else:
                music_status_var.set("● 已停止")
                music_status_label.configure(foreground="#ff6b6b")
        root.after(2000, sync_music_status)

    # Start the sync loop
    root.after(2000, sync_music_status)

    # --- GUI config sync (reload list when other clients save) ---
    def reload_gui_config():
        """Reload config from disk into the GUI widgets (thread-safe via root.after)."""
        _gui_loading_config[0] = True
        try:
            new_config = load_config()
            # Update URL listbox
            url_listbox.delete(0, "end")
            for url in new_config.get("urls", []):
                url_listbox.insert("end", url)
            # Update interval/music fields only if absent local edits to those fields
            try:
                interval_var.set(str(new_config.get("interval", 10)))
            except Exception:
                pass
            music_url_var.set(new_config.get("music_url", ""))
            # Keep local snapshot of fullscreen_selectors up to date
            config["fullscreen_selectors"] = new_config.get(
                "fullscreen_selectors", DEFAULT_CONFIG["fullscreen_selectors"]
            )
            config["urls"] = new_config.get("urls", [])
            config["interval"] = new_config.get("interval", 10)
            config["music_url"] = new_config.get("music_url", "")
        finally:
            _gui_loading_config[0] = False
        _gui_config_dirty.set(False)
        _gui_last_config_version[0] = get_config_version()

    def sync_config_status():
        """Poll config version; if changed and no local edits, reload the list."""
        current_version = get_config_version()
        if current_version != _gui_last_config_version[0]:
            _gui_last_config_version[0] = current_version
            if not _gui_config_dirty.get():
                reload_gui_config()
        root.after(2000, sync_config_status)

    # Start the config sync loop
    root.after(2000, sync_config_status)

    # --- frpc tunnel section ---
    frpc_sep = ttk.Separator(root, orient="horizontal")
    frpc_sep.pack(fill="x", padx=20, pady=(10, 5))

    frpc_title_frame = ttk.Frame(root)
    frpc_title_frame.pack(fill="x", padx=20, pady=(0, 5))
    ttk.Label(
        frpc_title_frame,
        text="🔗 frpc 远程隧道",
        font=("Helvetica", 14, "bold"),
        foreground="#00d4ff",
    ).pack(anchor="w")

    frpc_ctrl_frame = ttk.Frame(root)
    frpc_ctrl_frame.pack(fill="x", padx=20, pady=(0, 5))

    frpc_process = None
    frpc_running = tk.BooleanVar(value=False)

    frpc_status_var = tk.StringVar(value="● 未启动")
    frpc_status_label = ttk.Label(
        frpc_ctrl_frame,
        textvariable=frpc_status_var,
        foreground="#ff6b6b",
        font=("Helvetica", 11),
    )
    frpc_status_label.pack(side="right", padx=10)

    def start_frpc():
        nonlocal frpc_process
        if frpc_running.get():
            messagebox.showinfo("提示", "frpc 已在运行中")
            return

        frpc_ext = ".exe" if os.name == "nt" else ""
        frpc_bin = os.path.join(BASE_DIR, f"frpc{frpc_ext}")
        frpc_conf = os.path.join(BASE_DIR, "frpc.toml")

        if not os.path.exists(frpc_bin):
            messagebox.showerror("错误", f"找不到 frpc 可执行文件:\n{frpc_bin}")
            return
        if not os.path.exists(frpc_conf):
            messagebox.showerror("错误", f"找不到 frpc 配置文件:\n{frpc_conf}")
            return

        # Sync frpc.toml localPort with current GUI port before launching
        try:
            current_port = int(port_var.get())
            update_frpc_port(current_port)
        except ValueError:
            pass

        try:
            creationflags = 0
            if os.name == "nt":
                creationflags = 0x08000000  # CREATE_NO_WINDOW

            frpc_process = subprocess.Popen(
                [frpc_bin, "-c", frpc_conf],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=BASE_DIR,
                creationflags=creationflags,
            )
            frpc_running.set(True)
            frpc_status_var.set("● 运行中")
            frpc_status_label.configure(foreground="#00ff88")

            # Background thread to read frpc output
            def read_frpc_output():
                try:
                    for line in iter(frpc_process.stdout.readline, b""):
                        print(f"[frpc] {line.decode('utf-8', errors='replace').rstrip()}")
                except Exception:
                    pass
                # Process ended
                frpc_running.set(False)
                root.after(0, lambda: frpc_status_var.set("● 已停止"))
                root.after(
                    0, lambda: frpc_status_label.configure(foreground="#ff6b6b")
                )

            threading.Thread(target=read_frpc_output, daemon=True).start()
        except Exception as e:
            messagebox.showerror("错误", f"启动 frpc 失败:\n{e}")
            frpc_status_var.set("● 启动失败")
            frpc_status_label.configure(foreground="#ff6b6b")

    def stop_frpc():
        nonlocal frpc_process
        if not frpc_running.get() or frpc_process is None:
            messagebox.showinfo("提示", "frpc 未在运行")
            return
        try:
            frpc_process.terminate()
            frpc_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            frpc_process.kill()
        except Exception:
            pass
        frpc_process = None
        frpc_running.set(False)
        frpc_status_var.set("● 已停止")
        frpc_status_label.configure(foreground="#ff6b6b")

    ttk.Button(
        frpc_ctrl_frame,
        text="🚀 启动 frpc",
        style="Accent.TButton",
        command=start_frpc,
    ).pack(side="left", padx=(0, 5))

    ttk.Button(
        frpc_ctrl_frame,
        text="⏹️ 停止 frpc",
        style="Accent.TButton",
        command=stop_frpc,
    ).pack(side="left", padx=(0, 5))

    # Server controls
    server_frame = ttk.Frame(root)
    server_frame.pack(fill="x", padx=20, pady=(5, 5))

    ttk.Label(server_frame, text="服务器端口:").pack(side="left")
    port_var = tk.StringVar(value=str(read_frpc_port()))
    port_entry = tk.Entry(
        server_frame,
        textvariable=port_var,
        width=8,
        bg="#16213e",
        fg="#00d4ff",
        font=("Consolas", 13),
        relief="flat",
        insertbackground="#00d4ff",
    )
    port_entry.pack(side="left", padx=10)

    server_thread = None
    server_running = tk.BooleanVar(value=False)

    status_label = ttk.Label(
        server_frame, text="● 未启动", foreground="#ff6b6b", font=("Helvetica", 11)
    )
    status_label.pack(side="right", padx=10)

    def save_and_apply():
        urls = [url_listbox.get(i) for i in range(url_listbox.size())]
        try:
            interval = max(1, int(interval_var.get()))
        except ValueError:
            messagebox.showerror("错误", "间隔时间必须是整数")
            return

        new_config = {
            "urls": urls,
            "interval": interval,
            "fullscreen_selectors": config.get(
                "fullscreen_selectors", DEFAULT_CONFIG["fullscreen_selectors"]
            ),
            "music_url": music_url_var.get().strip(),
        }
        save_config(new_config)
        _gui_config_dirty.set(False)
        _gui_last_config_version[0] = get_config_version()
        messagebox.showinfo("成功", "配置已保存！正在展示的页面会自动更新。")

    def start_server():
        nonlocal server_thread
        if server_running.get():
            messagebox.showinfo("提示", "服务器已在运行中")
            return
        # Save config first
        save_and_apply_silent()

        try:
            port = int(port_var.get())
        except ValueError:
            messagebox.showerror("错误", "端口号必须是整数")
            return

        # Sync port to frpc.toml so tunnel matches server
        update_frpc_port(port)

        server_thread = threading.Thread(
            target=run_server, args=("0.0.0.0", port), daemon=True
        )
        server_thread.start()
        server_running.set(True)
        status_label.configure(text=f"● 运行中 (:{port})", foreground="#00ff88")
        webbrowser.open(f"http://localhost:{port}/")

    def save_and_apply_silent():
        urls = [url_listbox.get(i) for i in range(url_listbox.size())]
        try:
            interval = max(1, int(interval_var.get()))
        except ValueError:
            interval = 10
        new_config = {
            "urls": urls,
            "interval": interval,
            "fullscreen_selectors": config.get(
                "fullscreen_selectors", DEFAULT_CONFIG["fullscreen_selectors"]
            ),
            "music_url": music_url_var.get().strip(),
        }
        save_config(new_config)
        _gui_config_dirty.set(False)
        _gui_last_config_version[0] = get_config_version()

    # Action buttons
    action_frame = ttk.Frame(root)
    action_frame.pack(fill="x", padx=20, pady=(10, 15))

    ttk.Button(
        action_frame,
        text="💾 保存配置",
        style="Accent.TButton",
        command=save_and_apply,
    ).pack(side="left", padx=(0, 10))

    ttk.Button(
        action_frame,
        text="🚀 启动服务器",
        style="Accent.TButton",
        command=start_server,
    ).pack(side="left", padx=(0, 10))

    def open_display():
        try:
            port = int(port_var.get())
        except ValueError:
            port = 80
        webbrowser.open(f"http://localhost:{port}/")

    def open_admin():
        try:
            port = int(port_var.get())
        except ValueError:
            port = 80
        webbrowser.open(f"http://localhost:{port}/admin")

    ttk.Button(action_frame, text="🖥️ 打开展示页", command=open_display).pack(
        side="left", padx=(0, 10)
    )
    ttk.Button(action_frame, text="⚙️ 打开管理页", command=open_admin).pack(
        side="left", padx=(0, 10)
    )

    # Clean up frpc on window close
    def on_closing():
        if frpc_running.get() and frpc_process is not None:
            try:
                frpc_process.terminate()
                frpc_process.wait(timeout=3)
            except Exception:
                try:
                    frpc_process.kill()
                except Exception:
                    pass
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_closing)
    root.mainloop()


if __name__ == "__main__":
    if "--no-gui" in sys.argv:
        port = 80
        for i, arg in enumerate(sys.argv):
            if arg == "--port" and i + 1 < len(sys.argv):
                port = int(sys.argv[i + 1])
        run_server("0.0.0.0", port)
    else:
        run_gui()
