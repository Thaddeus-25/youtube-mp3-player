"""
MP3 & YouTube Audio Player
--------------------------
A desktop GUI application that plays local MP3/WAV files (via pygame) and
streams ad-free audio directly from YouTube (via yt-dlp + python-vlc),
without ever downloading video/audio files or ads to disk.
"""

import os
import re
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox
from urllib.parse import parse_qs, urlparse

import pygame

try:
    import vlc
    VLC_AVAILABLE = True
    VLC_IMPORT_ERROR = None
except Exception as exc:  # python-vlc failed to locate/load libvlc
    vlc = None
    VLC_AVAILABLE = False
    VLC_IMPORT_ERROR = str(exc)

try:
    import yt_dlp
    YTDLP_AVAILABLE = True
except Exception:
    yt_dlp = None
    YTDLP_AVAILABLE = False


# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------
BG = "#1a1a24"
BG_PANEL = "#22222e"
FG = "#e6e6ef"
FG_MUTED = "#9a9aab"
ACCENT = "#3ddc84"
ACCENT_DARK = "#2aa862"
BTN_BG = "#2c2c3c"
BTN_ACTIVE = "#3a3a4d"
TROUGH = "#33333f"
ERROR_COLOR = "#ff6b6b"
OK_COLOR = "#6bffb0"

FONT_TITLE = ("Segoe UI", 15, "bold")
FONT_STATUS = ("Segoe UI", 10)
FONT_LABEL = ("Segoe UI", 9)
FONT_BTN = ("Segoe UI", 10)

def parse_youtube_url(url):
    """Classify a URL as a YouTube / YouTube Music video or playlist link.

    Returns a ("video" | "playlist" | None, list_id) tuple. A watch URL that
    also carries a "list=" param (e.g. a video opened from within a playlist)
    is treated as a single video, not the playlist, to avoid surprising the
    user who just wanted that one track.
    """
    if not url:
        return None, None
    candidate = url if re.match(r"^https?://", url, re.IGNORECASE) else f"https://{url}"
    parsed = urlparse(candidate)
    host = parsed.netloc.lower()
    if not (host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")):
        return None, None

    query = parse_qs(parsed.query)
    list_id = query.get("list", [None])[0]
    video_id = query.get("v", [None])[0]
    path = parsed.path.rstrip("/")

    if host == "youtu.be" and not video_id:
        video_id = path.lstrip("/") or None

    if list_id and (path.endswith("/playlist") or not video_id):
        return "playlist", list_id
    if video_id or path.startswith("/shorts/"):
        return "video", None
    return None, None


def format_time(seconds):
    if seconds is None or seconds < 0:
        return "--:--"
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:d}:{s:02d}"


class AudioPlayerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("MP3 & YouTube Audio Player")
        self.root.geometry("560x620")
        self.root.minsize(520, 560)
        self.root.configure(bg=BG)

        # --- playback state ---
        self.active_engine = None          # None | "local" | "youtube"
        self.local_path = None
        self.local_duration = None         # seconds, may be None if unknown
        self.local_offset = 0.0            # seconds, position play() was started from
        self.local_started_at = None       # time.time() reference
        self.local_is_paused = False

        self.vlc_instance = vlc.Instance("--quiet") if VLC_AVAILABLE else None
        self.vlc_player = None
        self.youtube_title = None

        self.volume = 70  # 0-100, shared across engines
        self.user_seeking = False
        self._pending_skip = None  # after() id for a scheduled skip past a bad track

        # --- playlist state ---
        self.playlist = []          # list of file paths
        self.playlist_index = -1    # index of currently loaded/playing track, -1 if none
        self.playlist_visible = True

        # --- mini player state ---
        self.mini_mode = False
        self._full_geometry = None
        self._drag_offset = (0, 0)

        pygame.mixer.init()

        self._build_ui()
        self._poll_progress()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        self.full_view = tk.Frame(self.root, bg=BG)
        self.full_view.pack(fill="both", expand=True)

        title_row = tk.Frame(self.full_view, bg=BG)
        title_row.pack(fill="x", pady=(14, 6))
        tk.Label(
            title_row, text="🎵  MP3 & YouTube Audio Player",
            font=FONT_TITLE, bg=BG, fg=FG,
        ).pack(side="left", padx=(16, 0))
        self._make_button(title_row, "🗕 Mini Player", self._toggle_mini_mode).pack(side="right", padx=(0, 16))

        # ---- Load buttons (top) ----
        load_row = tk.Frame(self.full_view, bg=BG)
        load_row.pack(fill="x", padx=16, pady=(0, 10))
        self._make_button(load_row, "Load Track", self.load_track).pack(side="left", padx=(0, 4))
        self._make_button(load_row, "Load Folder", self.load_folder).pack(side="left", padx=4)

        # ---- Now playing / status panel ----
        status_panel = tk.Frame(self.full_view, bg=BG_PANEL)
        status_panel.pack(fill="x", padx=16, pady=(0, 10))

        self.status_var = tk.StringVar(value="No track loaded")
        tk.Label(
            status_panel, textvariable=self.status_var, font=FONT_STATUS,
            bg=BG_PANEL, fg=FG, wraplength=500, justify="left", anchor="w",
        ).pack(fill="x", padx=12, pady=(10, 2))

        self.error_var = tk.StringVar(value="")
        self.error_label = tk.Label(
            status_panel, textvariable=self.error_var, font=FONT_LABEL,
            bg=BG_PANEL, fg=ERROR_COLOR, wraplength=500, justify="left", anchor="w",
        )
        self.error_label.pack(fill="x", padx=12, pady=(0, 8))

        # progress / scrub bar
        progress_row = tk.Frame(status_panel, bg=BG_PANEL)
        progress_row.pack(fill="x", padx=12, pady=(0, 10))

        self.elapsed_var = tk.StringVar(value="0:00")
        self.duration_var = tk.StringVar(value="--:--")

        tk.Label(progress_row, textvariable=self.elapsed_var, font=FONT_LABEL,
                 bg=BG_PANEL, fg=FG_MUTED, width=6).pack(side="left")

        self.progress_scale = tk.Scale(
            progress_row, from_=0, to=1000, orient="horizontal",
            showvalue=False, bg=BG_PANEL, fg=FG, troughcolor=TROUGH,
            highlightthickness=0, bd=0, sliderrelief="flat",
            activebackground=ACCENT, length=300,
        )
        self.progress_scale.pack(side="left", fill="x", expand=True, padx=8)
        self.progress_scale.bind("<ButtonPress-1>", self._on_seek_start)
        self.progress_scale.bind("<ButtonRelease-1>", self._on_seek_end)

        tk.Label(progress_row, textvariable=self.duration_var, font=FONT_LABEL,
                 bg=BG_PANEL, fg=FG_MUTED, width=6).pack(side="left")

        # ---- Playlist (collapsible) ----
        playlist_section = tk.Frame(self.full_view, bg=BG)
        playlist_section.pack(fill="both", expand=True, padx=16, pady=(0, 10))

        self.playlist_toggle_btn = tk.Button(
            playlist_section, text="▼ Playlist", command=self._toggle_playlist,
            font=FONT_BTN, bg=BG_PANEL, fg=FG, activebackground=BTN_ACTIVE,
            activeforeground=FG, relief="flat", bd=0, anchor="w", padx=10,
            cursor="hand2",
        )
        self.playlist_toggle_btn.pack(fill="x", ipady=6)

        self.playlist_body = tk.Frame(playlist_section, bg=BG)
        self.playlist_body.pack(fill="both", expand=True)

        scrollbar = tk.Scrollbar(self.playlist_body, orient="vertical")
        self.playlist_box = tk.Listbox(
            self.playlist_body, height=6, bg=BTN_BG, fg=FG, relief="flat",
            highlightthickness=0, bd=0, selectbackground=ACCENT,
            selectforeground=BG, activestyle="none",
            yscrollcommand=scrollbar.set,
        )
        scrollbar.config(command=self.playlist_box.yview)
        self.playlist_box.pack(side="left", fill="both", expand=True, pady=(4, 0))
        scrollbar.pack(side="left", fill="y", pady=(4, 0))
        self.playlist_box.bind("<Double-Button-1>", self._on_playlist_double_click)

        # ---- YouTube controls ----
        yt_frame = tk.LabelFrame(
            self.full_view, text="YouTube / YouTube Music Streaming (ad-free, no download, playlists supported)",
            font=FONT_LABEL,
            bg=BG, fg=FG_MUTED, bd=1, labelanchor="nw",
        )
        yt_frame.pack(fill="x", padx=16, pady=(0, 10))

        yt_row = tk.Frame(yt_frame, bg=BG)
        yt_row.pack(fill="x", padx=10, pady=10)

        self.url_var = tk.StringVar()
        url_entry = tk.Entry(
            yt_row, textvariable=self.url_var, font=FONT_LABEL,
            bg=BTN_BG, fg=FG, insertbackground=FG, relief="flat",
        )
        url_entry.pack(side="left", fill="x", expand=True, ipady=4, padx=(0, 8))
        url_entry.bind("<Return>", lambda e: self.stream_youtube())

        self.stream_btn = self._make_button(yt_row, "Stream YouTube Audio", self.stream_youtube)
        self.stream_btn.pack(side="left")

        if not VLC_AVAILABLE:
            yt_row2 = tk.Frame(yt_frame, bg=BG)
            yt_row2.pack(fill="x", padx=10, pady=(0, 8))
            tk.Label(
                yt_row2,
                text="VLC not available - see terminal/log for details. YouTube "
                     "streaming is disabled until libVLC loads correctly.",
                font=FONT_LABEL, bg=BG, fg=ERROR_COLOR, wraplength=480, justify="left",
            ).pack(anchor="w")
            self.stream_btn.configure(state="disabled")

        # ---- Playback controls (bottom) ----
        controls_row = tk.Frame(self.full_view, bg=BG)
        controls_row.pack(pady=(0, 8))

        self._make_button(controls_row, "⏮ Prev", self.prev_track).pack(side="left", padx=4)
        self.playpause_btn = self._make_button(controls_row, "▶ Play", self._toggle_play_pause)
        self.playpause_btn.pack(side="left", padx=4)
        self._make_button(controls_row, "⏹ Stop", self.stop).pack(side="left", padx=4)
        self._make_button(controls_row, "⏭ Next", self.next_track).pack(side="left", padx=4)

        # ---- Volume ----
        vol_frame = tk.Frame(self.full_view, bg=BG)
        vol_frame.pack(fill="x", padx=16, pady=(0, 14))

        tk.Label(vol_frame, text="🔊 Volume", font=FONT_LABEL, bg=BG, fg=FG_MUTED).pack(side="left")
        self.volume_scale = tk.Scale(
            vol_frame, from_=0, to=100, orient="horizontal",
            showvalue=False, bg=BG, fg=FG, troughcolor=TROUGH,
            highlightthickness=0, bd=0, sliderrelief="flat",
            activebackground=ACCENT, length=200, command=self._on_volume_change,
        )
        self.volume_scale.set(self.volume)
        self.volume_scale.pack(side="left", padx=8)

        self._build_mini_view()
        self.root.bind("<space>", self._on_space)

    def _on_space(self, event):
        if isinstance(event.widget, tk.Entry):
            return  # let the URL box receive normal spaces
        self._toggle_play_pause()
        return "break"

    def _make_button(self, parent, text, command):
        return tk.Button(
            parent, text=text, command=command, font=FONT_BTN,
            bg=BTN_BG, fg=FG, activebackground=BTN_ACTIVE, activeforeground=FG,
            relief="flat", bd=0, padx=10, pady=6, cursor="hand2",
        )

    # ------------------------------------------------------------------
    # Playlist collapse/expand
    # ------------------------------------------------------------------
    def _toggle_playlist(self):
        self.playlist_visible = not self.playlist_visible
        if self.playlist_visible:
            self.playlist_body.pack(fill="both", expand=True)
            self.playlist_toggle_btn.configure(text="▼ Playlist")
        else:
            self.playlist_body.pack_forget()
            self.playlist_toggle_btn.configure(text="▶ Playlist")

    # ------------------------------------------------------------------
    # Mini player (compact, always-on-top, draggable floating window)
    # ------------------------------------------------------------------
    def _build_mini_view(self):
        self.mini_view = tk.Frame(self.root, bg=BG_PANEL)

        top_row = tk.Frame(self.mini_view, bg=BG_PANEL)
        top_row.pack(fill="x", padx=8, pady=(6, 2))

        # Buttons are packed before the label: with pack(), later widgets get
        # squeezed out first when space runs short, so the label (not the
        # buttons) must be the one that shrinks when the title is long.
        self._make_button(top_row, "✕", self.on_close).pack(side="right", padx=(2, 0))
        self._make_button(top_row, "🗗", self._exit_mini_mode).pack(side="right")

        self.mini_track_var = tk.StringVar(value="No track loaded")
        song_label = tk.Label(
            top_row, textvariable=self.mini_track_var, font=FONT_LABEL,
            bg=BG_PANEL, fg=FG, anchor="w",
        )
        song_label.pack(side="left", fill="x", expand=True)

        # Dragging: click-and-drag anywhere on the top row moves the window.
        for widget in (self.mini_view, top_row, song_label):
            widget.bind("<ButtonPress-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._do_drag)

        controls_row = tk.Frame(self.mini_view, bg=BG_PANEL)
        controls_row.pack(pady=(0, 8))

        self._make_button(controls_row, "⏮", self.prev_track).pack(side="left", padx=3)
        self.mini_playpause_btn = self._make_button(controls_row, "▶", self._toggle_play_pause)
        self.mini_playpause_btn.pack(side="left", padx=3)
        self._make_button(controls_row, "⏭", self.next_track).pack(side="left", padx=3)

    def _start_drag(self, event):
        self._drag_offset = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def _do_drag(self, event):
        x = event.x_root - self._drag_offset[0]
        y = event.y_root - self._drag_offset[1]
        self.root.geometry(f"+{x}+{y}")

    def _toggle_mini_mode(self):
        if self.mini_mode:
            self._exit_mini_mode()
        else:
            self._enter_mini_mode()

    def _enter_mini_mode(self):
        if self.mini_mode:
            return
        self._full_geometry = self.root.geometry()
        self.full_view.pack_forget()
        self.mini_view.pack(fill="both", expand=True)

        width, height = 260, 90
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x = screen_w - width - 20
        y = screen_h - height - 60  # sit just above the taskbar
        self.root.minsize(1, 1)  # full view's minsize would otherwise clamp us back up
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.resizable(False, False)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.mini_mode = True

    def _exit_mini_mode(self):
        if not self.mini_mode:
            return
        self.mini_view.pack_forget()
        self.root.overrideredirect(False)
        self.root.attributes("-topmost", False)
        self.root.resizable(True, True)
        self.root.minsize(520, 560)
        self.full_view.pack(fill="both", expand=True)
        if self._full_geometry:
            self.root.geometry(self._full_geometry)
        self.mini_mode = False

    def _current_track_label(self):
        if self.active_engine == "local" and self.local_path:
            return os.path.basename(self.local_path)
        if self.active_engine == "youtube" and self.youtube_title:
            return self.youtube_title
        if self.local_path:
            return os.path.basename(self.local_path)
        return "No track loaded"

    def _is_playing(self):
        if self.active_engine == "local":
            return not self.local_is_paused and pygame.mixer.music.get_busy()
        if self.active_engine == "youtube" and self.vlc_player is not None:
            return self.vlc_player.get_state() == vlc.State.Playing
        return False

    def _toggle_play_pause(self):
        if self.active_engine == "local":
            self.resume() if self.local_is_paused else self.pause()
        elif self.active_engine == "youtube" and self.vlc_player is not None:
            self.resume() if self.vlc_player.get_state() == vlc.State.Paused else self.pause()
        elif self.playlist and 0 <= self.playlist_index < len(self.playlist):
            # Nothing active (stopped or finished): restart the current entry.
            self._play_index(self.playlist_index)
        else:
            self.play_local()  # surfaces the "no track loaded" hint

    # ------------------------------------------------------------------
    # Status / error helpers
    # ------------------------------------------------------------------
    def _set_status(self, text):
        self.status_var.set(text)

    def _set_error(self, text):
        self.error_var.set(text)

    def _clear_error(self):
        self.error_var.set("")

    # ------------------------------------------------------------------
    # Local playback
    # ------------------------------------------------------------------
    def load_track(self):
        path = filedialog.askopenfilename(
            title="Select an audio file",
            filetypes=[("Audio files", "*.mp3 *.wav"), ("All files", "*.*")],
        )
        if not path:
            return
        if not os.path.isfile(path):
            self._set_error("Selected file no longer exists.")
            return

        self.playlist = [{"type": "local", "path": path}]
        self._refresh_playlist_listbox()
        self._load_local_path(path, index=0)

    def load_folder(self):
        folder = filedialog.askdirectory(title="Select a folder of audio files")
        if not folder:
            return

        try:
            names = sorted(
                f for f in os.listdir(folder)
                if f.lower().endswith((".mp3", ".wav"))
            )
        except OSError as exc:
            self._set_error(f"Could not read folder: {exc}")
            return

        if not names:
            self._set_error("No .mp3 or .wav files found in that folder.")
            return

        self.playlist = [{"type": "local", "path": os.path.join(folder, name)} for name in names]
        self._refresh_playlist_listbox()
        self._clear_error()
        self._set_status(f"Loaded playlist: {len(self.playlist)} track(s) from {os.path.basename(folder)}")
        self._load_local_path(self.playlist[0]["path"], index=0)

    def _refresh_playlist_listbox(self):
        self.playlist_box.delete(0, "end")
        for item in self.playlist:
            if item["type"] == "local":
                label = os.path.basename(item["path"])
            else:
                label = f"▶ {item.get('title') or item['url']}"
            self.playlist_box.insert("end", label)

    def _highlight_playlist_index(self, index):
        self.playlist_box.selection_clear(0, "end")
        if 0 <= index < self.playlist_box.size():
            self.playlist_box.selection_set(index)
            self.playlist_box.see(index)

    def _on_playlist_double_click(self, _event):
        selection = self.playlist_box.curselection()
        if not selection:
            return
        self._play_index(selection[0])

    def _play_index(self, index):
        if index < 0 or index >= len(self.playlist):
            return
        self._cancel_pending_skip()
        item = self.playlist[index]
        self.playlist_index = index
        if item["type"] == "local":
            self._load_local_path(item["path"], index=index)
            if not self.play_local():
                self._schedule_skip()
        else:
            self._highlight_playlist_index(index)
            self._play_youtube_item(item)

    def _schedule_skip(self):
        """Auto-advance past an unavailable track. Never wraps around, so a
        playlist where everything is broken stops at the end instead of
        cycling forever."""
        if not (self.playlist and 0 <= self.playlist_index < len(self.playlist) - 1):
            return
        self._set_status("Track unavailable — skipping to next…")
        self._cancel_pending_skip()
        self._pending_skip = self.root.after(700, self._do_skip)

    def _do_skip(self):
        self._pending_skip = None
        self.next_track()

    def _cancel_pending_skip(self):
        if self._pending_skip is not None:
            self.root.after_cancel(self._pending_skip)
            self._pending_skip = None

    def next_track(self):
        if not self.playlist:
            return
        self._play_index((self.playlist_index + 1) % len(self.playlist))

    def prev_track(self):
        if not self.playlist:
            return
        self._play_index((self.playlist_index - 1) % len(self.playlist))

    def _load_local_path(self, path, index):
        self.local_path = path
        self.playlist_index = index
        self.local_duration = None
        try:
            self.local_duration = pygame.mixer.Sound(path).get_length()
        except Exception:
            # Duration probe failed (unsupported codec quirk); playback may still work.
            self.local_duration = None

        self._clear_error()
        self._set_status(f"Loaded: {os.path.basename(path)}")
        self.duration_var.set(format_time(self.local_duration))
        self.elapsed_var.set("0:00")
        self.progress_scale.set(0)
        self._highlight_playlist_index(index)

    def play_local(self):
        """Start the loaded local track. Returns True on success."""
        if not self.local_path:
            self._set_error("No local track loaded. Click 'Load Track' first.")
            return False
        if not os.path.isfile(self.local_path):
            self._set_error("The loaded file could not be found on disk.")
            return False

        self._stop_youtube(silent=True)

        try:
            pygame.mixer.music.load(self.local_path)
            pygame.mixer.music.set_volume(self.volume / 100)
            pygame.mixer.music.play(start=0.0)
        except Exception as exc:
            self._set_error(f"Could not play file: {exc}")
            return False

        self.active_engine = "local"
        self.local_offset = 0.0
        self.local_started_at = time.time()
        self.local_is_paused = False
        self._clear_error()
        self._set_status(f"Playing: {os.path.basename(self.local_path)}")
        return True

    def pause(self):
        if self.active_engine == "local":
            if not self.local_is_paused:
                pygame.mixer.music.pause()
                self.local_offset = self._local_elapsed()
                self.local_is_paused = True
                self._set_status(f"Paused: {os.path.basename(self.local_path)}")
        elif self.active_engine == "youtube" and self.vlc_player is not None:
            self.vlc_player.set_pause(1)
            self._set_status(f"Paused: {self.youtube_title or 'YouTube stream'}")

    def resume(self):
        if self.active_engine == "local":
            if self.local_is_paused:
                pygame.mixer.music.unpause()
                self.local_started_at = time.time()
                self.local_is_paused = False
                self._set_status(f"Playing: {os.path.basename(self.local_path)}")
        elif self.active_engine == "youtube" and self.vlc_player is not None:
            self.vlc_player.set_pause(0)
            self._set_status(f"Playing: {self.youtube_title or 'YouTube stream'}")

    def stop(self):
        self._cancel_pending_skip()
        if self.active_engine == "local":
            pygame.mixer.music.stop()
            self.active_engine = None
            self.local_offset = 0.0
            self.local_is_paused = False
            self._set_status("Stopped")
            self.elapsed_var.set("0:00")
            self.progress_scale.set(0)
        elif self.active_engine == "youtube":
            self._stop_youtube(silent=False)

    def _local_elapsed(self):
        if self.active_engine != "local":
            return 0.0
        if self.local_is_paused or self.local_started_at is None:
            return self.local_offset
        return self.local_offset + (time.time() - self.local_started_at)

    # ------------------------------------------------------------------
    # YouTube streaming
    # ------------------------------------------------------------------
    def stream_youtube(self):
        if not VLC_AVAILABLE:
            self._set_error("VLC is not available; cannot stream YouTube audio.")
            return
        if not YTDLP_AVAILABLE:
            self._set_error("yt-dlp is not installed; cannot stream YouTube audio.")
            return

        url = self.url_var.get().strip()
        if not url:
            self._set_error("Enter a YouTube or YouTube Music URL first.")
            return
        kind, _list_id = parse_youtube_url(url)
        if kind is None:
            self._set_error("That doesn't look like a valid YouTube/YouTube Music URL.")
            return

        self._clear_error()
        self._cancel_pending_skip()
        self.stream_btn.configure(state="disabled")

        if kind == "playlist":
            self._set_status("Resolving playlist…")
            thread = threading.Thread(target=self._extract_playlist_and_load, args=(url,), daemon=True)
        else:
            self._set_status("Resolving stream…")
            thread = threading.Thread(target=self._extract_and_play, args=(url, False), daemon=True)
        thread.start()

    def _play_youtube_item(self, item):
        if not VLC_AVAILABLE:
            self._set_error("VLC is not available; cannot stream YouTube audio.")
            return
        if not YTDLP_AVAILABLE:
            self._set_error("yt-dlp is not installed; cannot stream YouTube audio.")
            return

        self._clear_error()
        self._set_status(f"Resolving: {item.get('title') or item['url']}…")
        thread = threading.Thread(target=self._extract_and_play, args=(item["url"], True), daemon=True)
        thread.start()

    def _extract_playlist_and_load(self, url):
        ydl_opts = {
            "extract_flat": "in_playlist",
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as exc:
            self.root.after(0, self._on_youtube_error, f"Could not resolve playlist: {exc}")
            return
        except Exception as exc:
            self.root.after(0, self._on_youtube_error, f"Unexpected error: {exc}")
            return

        items = []
        for entry in info.get("entries") or []:
            if not entry:
                continue
            video_id = entry.get("id")
            if not video_id:
                continue
            items.append({
                "type": "youtube",
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "title": entry.get("title") or video_id,
            })

        if not items:
            self.root.after(0, self._on_youtube_error, "Playlist is empty or could not be read.")
            return

        playlist_title = info.get("title") or "YouTube playlist"
        self.root.after(0, self._on_youtube_playlist_loaded, items, playlist_title)

    def _on_youtube_playlist_loaded(self, items, playlist_title):
        self.playlist = items
        self.playlist_index = -1
        self._refresh_playlist_listbox()
        self._clear_error()
        self._set_status(f"Loaded playlist: {len(items)} track(s) — {playlist_title}")
        self.stream_btn.configure(state="normal")
        self._play_index(0)

    def _extract_and_play(self, url, is_playlist_item):
        ydl_opts = {
            "format": "bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as exc:
            self.root.after(0, self._on_youtube_error, f"Could not resolve video: {exc}", is_playlist_item)
            return
        except Exception as exc:
            self.root.after(0, self._on_youtube_error, f"Unexpected error: {exc}", is_playlist_item)
            return

        stream_url = info.get("url")
        if not stream_url and info.get("requested_formats"):
            stream_url = info["requested_formats"][0].get("url")
        title = info.get("title", "YouTube stream")

        if not stream_url:
            self.root.after(0, self._on_youtube_error,
                            "No playable audio stream found for this video.", is_playlist_item)
            return

        self.root.after(0, self._start_vlc_playback, stream_url, title, url, is_playlist_item)

    def _start_vlc_playback(self, stream_url, title, original_url, is_playlist_item):
        pygame.mixer.music.stop()  # ensure local playback yields to streaming

        try:
            if self.vlc_player is not None:
                self.vlc_player.stop()
            media = self.vlc_instance.media_new(stream_url)
            self.vlc_player = self.vlc_instance.media_player_new()
            self.vlc_player.set_media(media)
            self.vlc_player.audio_set_volume(self.volume)
            result = self.vlc_player.play()
            if result == -1:
                raise RuntimeError("VLC failed to start playback")
        except Exception as exc:
            self._on_youtube_error(f"Playback failed: {exc}", is_playlist_item)
            return

        self.active_engine = "youtube"
        self.youtube_title = title
        self._clear_error()
        self._set_status(f"Streaming: {title}")
        self.stream_btn.configure(state="normal")
        self.elapsed_var.set("0:00")
        self.duration_var.set("--:--")
        self.progress_scale.set(0)

        if is_playlist_item:
            if 0 <= self.playlist_index < len(self.playlist):
                self.playlist[self.playlist_index]["title"] = title
                self._refresh_playlist_listbox()
                self._highlight_playlist_index(self.playlist_index)
        else:
            self.playlist = [{"type": "youtube", "url": original_url, "title": title}]
            self.playlist_index = 0
            self._refresh_playlist_listbox()
            self._highlight_playlist_index(0)

    def _on_youtube_error(self, message, skip=False):
        self._set_error(message)
        self._set_status("Idle")
        self.stream_btn.configure(state="normal")
        if skip:
            self._schedule_skip()

    def _stop_youtube(self, silent):
        if self.vlc_player is not None:
            try:
                self.vlc_player.stop()
            except Exception:
                pass
        if self.active_engine == "youtube":
            self.active_engine = None
            self.youtube_title = None
            if not silent:
                self._set_status("Stopped")
                self.elapsed_var.set("0:00")
                self.progress_scale.set(0)

    # ------------------------------------------------------------------
    # Volume
    # ------------------------------------------------------------------
    def _on_volume_change(self, value):
        self.volume = int(float(value))
        if self.active_engine == "local":
            pygame.mixer.music.set_volume(self.volume / 100)
        elif self.active_engine == "youtube" and self.vlc_player is not None:
            self.vlc_player.audio_set_volume(self.volume)

    # ------------------------------------------------------------------
    # Seeking / scrubbing
    # ------------------------------------------------------------------
    def _on_seek_start(self, _event):
        self.user_seeking = True

    def _on_seek_end(self, _event):
        self.user_seeking = False
        slider_value = self.progress_scale.get()  # 0-1000

        if self.active_engine == "local" and self.local_duration:
            target_seconds = (slider_value / 1000.0) * self.local_duration
            was_paused = self.local_is_paused
            try:
                pygame.mixer.music.play(start=target_seconds)
                if was_paused:
                    pygame.mixer.music.pause()
                self.local_offset = target_seconds
                self.local_started_at = time.time()
                self.local_is_paused = was_paused
            except Exception as exc:
                self._set_error(f"Seek failed: {exc}")

        elif self.active_engine == "youtube" and self.vlc_player is not None:
            length_ms = self.vlc_player.get_length()
            if length_ms and length_ms > 0:
                target_ms = int((slider_value / 1000.0) * length_ms)
                try:
                    self.vlc_player.set_time(target_ms)
                except Exception as exc:
                    self._set_error(f"Seek failed: {exc}")

    # ------------------------------------------------------------------
    # Periodic UI updates
    # ------------------------------------------------------------------
    def _poll_progress(self):
        if not self.user_seeking:
            if self.active_engine == "local" and self.local_duration:
                elapsed = min(self._local_elapsed(), self.local_duration)
                self.elapsed_var.set(format_time(elapsed))
                self.progress_scale.set(int((elapsed / self.local_duration) * 1000))
            elif self.active_engine == "youtube" and self.vlc_player is not None:
                length_ms = self.vlc_player.get_length()
                time_ms = self.vlc_player.get_time()
                if length_ms and length_ms > 0:
                    self.duration_var.set(format_time(length_ms / 1000))
                    self.progress_scale.set(int((time_ms / length_ms) * 1000))
                if time_ms and time_ms >= 0:
                    self.elapsed_var.set(format_time(time_ms / 1000))

            finished_local = (
                self.active_engine == "local"
                and not self.local_is_paused
                and not pygame.mixer.music.get_busy()
            )
            finished_youtube = (
                self.active_engine == "youtube"
                and self.vlc_player is not None
                and self.vlc_player.get_state() in (vlc.State.Ended, vlc.State.Error)
            )

            if finished_local or finished_youtube:
                self.active_engine = None
                self.local_is_paused = False
                self.local_offset = 0.0
                self.youtube_title = None
                self.progress_scale.set(1000)

                has_next = (
                    self.playlist
                    and 0 <= self.playlist_index < len(self.playlist) - 1
                )
                if has_next:
                    self.root.after(0, self.next_track)
                else:
                    self._set_status("Finished")

        label = self._current_track_label()
        if len(label) > 30:  # keep the mini player's 🗗/✕ buttons visible
            label = label[:29] + "…"
        self.mini_track_var.set(label)
        is_playing = self._is_playing()
        self.mini_playpause_btn.configure(text="⏸" if is_playing else "▶")
        self.playpause_btn.configure(text="⏸ Pause" if is_playing else "▶ Play")

        self.root.after(250, self._poll_progress)

    # ------------------------------------------------------------------
    def on_close(self):
        try:
            pygame.mixer.music.stop()
        except Exception:
            pass
        if self.vlc_player is not None:
            try:
                self.vlc_player.stop()
            except Exception:
                pass
        self.root.destroy()


def main():
    root = tk.Tk()
    app = AudioPlayerApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
