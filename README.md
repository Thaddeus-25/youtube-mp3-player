# MP3 & YouTube Audio Player

A lightweight desktop GUI audio player, built with Tkinter, that plays local MP3/WAV files and streams ad-free audio directly from YouTube / YouTube Music — without ever downloading video or audio files to disk.

## Features

- **Local playback** — load a single MP3/WAV file or an entire folder as a playlist.
- **YouTube / YouTube Music streaming** — paste a video, Shorts, or playlist URL and stream audio directly (no downloads, no ads).
- **Unified playlist** — mix local tracks and YouTube entries in one collapsible playlist with double-click to play, next/prev, and auto-advance.
- **Transport controls** — play, pause, resume, stop, seek/scrub bar with live elapsed/duration display.
- **Volume control** — shared slider that works across both playback engines.
- **Mini player mode** — collapse to a small, draggable, always-on-top floating widget with play/pause and next/prev.

## Requirements

- Python 3
- [VLC media player](https://www.videolan.org/vlc/) installed on your system (required by `python-vlc` for YouTube audio streaming)
- Python packages (see `requirements.txt`):
  - `pygame`
  - `yt-dlp`
  - `python-vlc`
  - `pillow`

## Setup & Running

### Windows (easiest)

Double-click `run.bat`. It installs/updates dependencies and launches the app automatically.

### Manual

```bash
pip install -r requirements.txt
python player_app.py
```

## Usage

- **Load Track** / **Load Folder** — load a single audio file or all `.mp3`/`.wav` files in a folder as a playlist.
- **YouTube URL field** — paste a YouTube or YouTube Music video/Shorts/playlist link and click **Stream YouTube Audio** (or press Enter).
- **Playlist panel** — double-click any entry to jump to it; use ⏮/⏭ to move between tracks.
- **🗕 Mini Player** — switch to a compact floating window; click 🗗 to return to the full view.

## Notes

- If VLC isn't installed or `python-vlc` can't locate `libvlc`, YouTube streaming is disabled and an error is shown in the UI, but local playback still works.
- Playlist URLs opened as a single video (i.e. a `list=` param on a `watch` URL) are treated as that one video, not the whole playlist.
