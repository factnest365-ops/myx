from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

YTDLP = shutil.which("yt-dlp") or "/opt/homebrew/bin/yt-dlp"
TIMEOUT = 20


class SourcesError(RuntimeError):
    """Human-readable source failure."""


def _run_ytdlp(args: list[str]) -> str:
    try:
        proc = subprocess.run(
            [YTDLP, *args],
            text=True,
            capture_output=True,
            timeout=TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SourcesError("yt-dlp timed out") from exc
    except OSError as exc:
        raise SourcesError(f"could not run yt-dlp: {exc}") from exc
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "yt-dlp failed").strip().splitlines()[-1]
        raise SourcesError(msg)
    return proc.stdout


def _clean_artist(value: Any) -> str | None:
    if not value:
        return None
    text = str(value)
    text = re.sub(r"\s*-\s*Topic$", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _thumb(entry: dict[str, Any]) -> str | None:
    thumbs = entry.get("thumbnails") or []
    if isinstance(thumbs, list) and thumbs:
        url = thumbs[-1].get("url") if isinstance(thumbs[-1], dict) else None
        if url:
            return str(url)
    return entry.get("thumbnail")


def _shape(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": entry.get("id"),
        "title": entry.get("title"),
        "artist": _clean_artist(entry.get("uploader") or entry.get("channel") or entry.get("artist")),
        "duration": entry.get("duration"),
        "thumb": _thumb(entry),
    }


def search(q: str) -> list[dict[str, Any]]:
    data = json.loads(_run_ytdlp(["--dump-single-json", "--no-warnings", f"ytsearch20:{q}"]))
    return [_shape(e) for e in data.get("entries", []) if isinstance(e, dict)]


def mix_chain(video_id: str) -> list[str]:
    url = f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"
    out = _run_ytdlp([
        "--flat-playlist",
        "--playlist-end",
        "25",
        "--no-warnings",
        "-j",
        "--extractor-args",
        "youtube:player_client=web,music",
        url,
    ])
    ids: list[str] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        vid = item.get("id")
        if vid:
            ids.append(str(vid))
    return ids


def meta(video_id: str) -> dict[str, Any]:
    data = json.loads(_run_ytdlp(["--dump-single-json", "--no-warnings", f"https://www.youtube.com/watch?v={video_id}"]))
    return _shape(data)


def cache_thumb(url_or_id: str, thumb_url: str) -> str:
    cache = Path.home() / "Library" / "Caches" / "myx" / "thumbs"
    cache.mkdir(parents=True, exist_ok=True)
    suffix = Path(urllib.parse.urlparse(thumb_url).path).suffix or ".jpg"
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", url_or_id)[-120:]
    path = cache / f"{safe}{suffix}"
    if path.exists() and path.stat().st_size:
        return str(path)
    req = urllib.request.Request(thumb_url, headers={"User-Agent": "myx/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            path.write_bytes(resp.read())
    except (OSError, urllib.error.URLError) as exc:
        raise SourcesError(f"could not cache thumbnail: {exc}") from exc
    return str(path)


def lyrics(title: str, artist: str, duration_s: int | float | None = None) -> dict[str, str | None]:
    params: dict[str, str] = {"track_name": title, "artist_name": artist}
    if duration_s is not None:
        params["duration"] = str(int(duration_s))
    url = "https://lrclib.net/api/get?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "myx/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            if resp.status == 404:
                return {"lrc": None, "plain": None}
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"lrc": None, "plain": None}
        raise SourcesError(f"lyrics lookup failed: HTTP {exc.code}") from exc
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise SourcesError(f"lyrics lookup failed: {exc}") from exc
    if not isinstance(data, dict):
        return {"lrc": None, "plain": None}
    return {"lrc": data.get("syncedLyrics"), "plain": data.get("plainLyrics")}
