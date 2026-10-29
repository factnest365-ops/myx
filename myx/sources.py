from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from . import __version__

USER_AGENT = f"myx/{__version__}"

YTDLP = shutil.which("yt-dlp") or "/opt/homebrew/bin/yt-dlp"
TIMEOUT = 20
MAX_THUMB_BYTES = 10 * 1024 * 1024  # 10 MiB cap against unbounded stream DoS
MAX_LYRICS_BYTES = 2 * 1024 * 1024  # 2 MiB cap against oversized API payloads
_SAFE_IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

# Match ANSI CSI / OSC / APC / DCS sequences so untrusted titles/lyrics cannot inject terminal escapes
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b\[[0-9;?]*[A-Za-z]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\|$)"
    r"|\x1b_[^\x1b]*(?:\x1b\\|$)"
    r"|\x1b[PX^][^\x1b]*(?:\x1b\\|$)"
    r"|\x1b."
)


class SourcesError(RuntimeError):
    """Human-readable source failure."""


def _sanitize_text(value: Any, *, multiline: bool = False) -> str | None:
    """Strip terminal escape sequences and control characters from external strings."""
    if value is None:
        return None
    text = _ANSI_ESCAPE_RE.sub("", str(value))
    if multiline:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        cleaned = "".join(
            ch
            for ch in text
            if ch == "\n" or (ord(ch) >= 0x20 and not (0x7F <= ord(ch) <= 0x9F))
        )
        return cleaned.strip() or None
    cleaned = "".join(
        " " if ch in ("\r", "\n", "\t") else ch
        for ch in text
        if ch in ("\r", "\n", "\t")
        or (ord(ch) >= 0x20 and not (0x7F <= ord(ch) <= 0x9F))
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or None


def _validate_video_id(video_id: str) -> str:
    """Ensure video_id is a safe identifier and cannot inject URL params or CLI flags."""
    vid = str(video_id or "").strip()
    if not vid or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", vid):
        raise SourcesError(f"invalid video id: {vid!r}")
    return vid


def _validate_http_url(url: str) -> str:
    """Validate that a URL uses http/https and does not target local/private endpoints."""
    raw = str(url or "").strip()
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme not in ("https", "http"):
        raise SourcesError(f"disallowed URL scheme: {parsed.scheme or 'none'}")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise SourcesError("missing URL hostname")
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise SourcesError(f"disallowed local hostname: {host}")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        raise SourcesError(f"disallowed non-public IP address: {host}")
    return raw


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        resolved = urllib.parse.urljoin(req.full_url, newurl)
        _validate_http_url(resolved)
        return super().redirect_request(req, fp, code, msg, headers, resolved)


def _audio_cache_dir() -> Path:
    if env_dir := os.environ.get("MYX_CACHE_DIR"):
        return Path(env_dir) / "audio"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "myx" / "audio"
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else (Path.home() / ".cache")
    return base / "myx" / "audio"


def _thumb_cache_dir() -> Path:
    if env_dir := os.environ.get("MYX_CACHE_DIR"):
        return Path(env_dir) / "thumbs"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "myx" / "thumbs"
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else (Path.home() / ".cache")
    return base / "myx" / "thumbs"


def _run_ytdlp(args: list[str]) -> str:
    bin_path = shutil.which("yt-dlp") or YTDLP
    try:
        proc = subprocess.run(
            [bin_path, *args],
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
        lines = (proc.stderr or proc.stdout or "yt-dlp failed").strip().splitlines()
        raise SourcesError(lines[-1] if lines else "yt-dlp failed")
    return proc.stdout


def _clean_artist(value: Any) -> str | None:
    text = _sanitize_text(value)
    if not text:
        return None
    text = re.sub(r"\s*-\s*Topic$", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _thumb(entry: dict[str, Any]) -> str | None:
    thumbs = entry.get("thumbnails") or []
    if isinstance(thumbs, list) and thumbs:
        for candidate in reversed(thumbs):
            url = candidate.get("url") if isinstance(candidate, dict) else None
            if isinstance(url, str) and url.startswith(("https://", "http://")):
                return url
        url = thumbs[-1].get("url") if isinstance(thumbs[-1], dict) else None
        if url:
            return str(url)
    raw = entry.get("thumbnail")
    return str(raw) if raw else None


def _extract_chapters(entry: dict[str, Any]) -> list[dict[str, Any]]:
    raw_chapters = entry.get("chapters")
    if not isinstance(raw_chapters, list) or not raw_chapters:
        return []
    chapters: list[dict[str, Any]] = []
    for ch in raw_chapters[:100]:
        if not isinstance(ch, dict):
            continue
        try:
            start = float(ch.get("start_time", 0.0))
            end = float(ch.get("end_time", start))
        except (TypeError, ValueError):
            continue
        if start < 0:
            continue
        chapters.append(
            {
                "title": _sanitize_text(ch.get("title")) or f"Chapter {len(chapters) + 1}",
                "start_time": round(start, 2),
                "end_time": round(max(start, end), 2),
            }
        )
    return chapters


def _shape(entry: dict[str, Any]) -> dict[str, Any]:
    raw_id = entry.get("id")
    clean_id = _sanitize_text(raw_id) if raw_id is not None else None
    shaped: dict[str, Any] = {
        "id": clean_id,
        "title": _sanitize_text(entry.get("title")),
        "artist": _clean_artist(
            entry.get("uploader") or entry.get("channel") or entry.get("artist")
        ),
        "duration": entry.get("duration"),
        "thumb": _thumb(entry),
    }
    chapters = _extract_chapters(entry)
    if chapters:
        shaped["chapters"] = chapters
    return shaped


def search(q: str) -> list[dict[str, Any]]:
    clean_q = (_sanitize_text(q) or "").strip()
    if not clean_q:
        return []
    data = json.loads(
        _run_ytdlp(["--dump-single-json", "--no-warnings", "--", f"ytsearch20:{clean_q}"])
    )
    return [_shape(e) for e in data.get("entries", []) if isinstance(e, dict)]


def mix_chain_items(video_id: str) -> list[dict[str, Any]]:
    vid = _validate_video_id(video_id)
    url = f"https://www.youtube.com/watch?v={vid}&list=RD{vid}"
    out = _run_ytdlp(
        [
            "--flat-playlist",
            "--playlist-end",
            "25",
            "--no-warnings",
            "-j",
            "--extractor-args",
            "youtube:player_client=web,music",
            "--",
            url,
        ]
    )
    items: list[dict[str, Any]] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and item.get("id"):
            items.append(_shape(item))
    return items


def mix_chain(video_id: str) -> list[str]:
    return [str(item["id"]) for item in mix_chain_items(video_id) if item.get("id")]


def meta(video_id: str) -> dict[str, Any]:
    vid = _validate_video_id(video_id)
    data = json.loads(
        _run_ytdlp(
            [
                "--dump-single-json",
                "--no-warnings",
                "--",
                f"https://www.youtube.com/watch?v={vid}",
            ]
        )
    )
    return _shape(data)


def stream_url(video_id: str) -> str:
    """Resolve a direct bestaudio CDN URL (IPv4-forced for reliable streaming)."""
    vid = _validate_video_id(video_id)
    out = _run_ytdlp(
        [
            "--force-ipv4",
            "--no-warnings",
            "--get-url",
            "-f",
            "bestaudio",
            "--",
            f"https://www.youtube.com/watch?v={vid}",
        ]
    )
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith(("https://", "http://")):
            return line
    raise SourcesError("yt-dlp returned no stream url")


def cache_thumb(url_or_id: str, thumb_url: str) -> str:
    validated_url = _validate_http_url(thumb_url)
    cache = _thumb_cache_dir()
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw_suffix = Path(urllib.parse.urlparse(validated_url).path).suffix.lower()
    suffix = raw_suffix if raw_suffix in _SAFE_IMG_EXTS else ".jpg"
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(url_or_id or "thumb")).strip(".")[-120:] or "thumb"
    path = cache / f"{safe}{suffix}"
    if path.is_symlink():
        raise SourcesError("refusing to use symlinked thumbnail cache path")
    if path.exists() and path.stat().st_size > 0:
        return str(path)
    req = urllib.request.Request(validated_url, headers={"User-Agent": USER_AGENT})
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    try:
        with opener.open(req, timeout=TIMEOUT) as resp:
            payload = resp.read(MAX_THUMB_BYTES + 1)
            if len(payload) > MAX_THUMB_BYTES:
                raise SourcesError("thumbnail exceeds maximum allowed size")
            if not payload:
                raise SourcesError("empty thumbnail response")
        fd, tmp_name = tempfile.mkstemp(dir=str(cache), prefix=f".{safe}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except SourcesError:
        raise
    except (OSError, urllib.error.URLError) as exc:
        raise SourcesError(f"could not cache thumbnail: {exc}") from exc
    return str(path)


def lyrics(
    title: str, artist: str, duration_s: float | None = None
) -> dict[str, str | None]:
    clean_title = _sanitize_text(title) or ""
    clean_artist = _sanitize_text(artist) or ""
    if not clean_title:
        return {"lrc": None, "plain": None}
    params: dict[str, str] = {"track_name": clean_title, "artist_name": clean_artist}
    if duration_s is not None:
        try:
            dur_i = int(float(duration_s))
            if dur_i > 0:
                params["duration"] = str(dur_i)
        except (TypeError, ValueError, OverflowError):
            pass
    url = "https://lrclib.net/api/get?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            if resp.status == 404:
                return {"lrc": None, "plain": None}
            raw_bytes = resp.read(MAX_LYRICS_BYTES + 1)
            if len(raw_bytes) > MAX_LYRICS_BYTES:
                raise SourcesError("lyrics payload exceeds maximum allowed size")
            data = json.loads(raw_bytes.decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"lrc": None, "plain": None}
        raise SourcesError(f"lyrics lookup failed: HTTP {exc.code}") from exc
    except SourcesError:
        raise
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise SourcesError(f"lyrics lookup failed: {exc}") from exc
    if not isinstance(data, dict):
        return {"lrc": None, "plain": None}
    return {
        "lrc": _sanitize_text(data.get("syncedLyrics"), multiline=True),
        "plain": _sanitize_text(data.get("plainLyrics"), multiline=True),
    }


def playlist_items(playlist_url_or_id: str, limit: int = 100) -> list[dict[str, Any]]:
    """Fetch tracks from a public YouTube / YT Music playlist or album URL/ID."""
    raw = str(playlist_url_or_id or "").strip()
    if not raw:
        raise SourcesError("playlist URL or ID required")
    m = re.search(r"[?&]list=([A-Za-z0-9_-]{4,128})", raw)
    if m:
        list_id = m.group(1)
        url = f"https://www.youtube.com/playlist?list={list_id}"
    elif re.fullmatch(r"[A-Za-z0-9_-]{10,128}", raw):
        url = f"https://www.youtube.com/playlist?list={raw}"
    elif raw.startswith(("https://www.youtube.com/", "https://music.youtube.com/")):
        url = _validate_http_url(raw)
    else:
        raise SourcesError("invalid YouTube playlist URL or ID")
    capped_limit = max(1, min(250, int(limit)))
    out = _run_ytdlp(
        [
            "--flat-playlist",
            "--playlist-end",
            str(capped_limit),
            "--no-warnings",
            "-j",
            "--",
            url,
        ]
    )
    items: list[dict[str, Any]] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and item.get("id"):
            items.append(_shape(item))
    return items


def cache_audio(video_id: str) -> str:
    """Download bestaudio (opus/m4a) into the local offline cache and return its path."""
    vid = _validate_video_id(video_id)
    audio_dir = _audio_cache_dir()
    audio_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for ext in ("opus", "m4a", "webm", "ogg", "mp3"):
        existing = audio_dir / f"{vid}.{ext}"
        if existing.is_file() and not existing.is_symlink() and existing.stat().st_size > 0:
            return str(existing)
    out_tmpl = str(audio_dir / f"{vid}.%(ext)s")
    bin_path = shutil.which("yt-dlp") or YTDLP
    try:
        proc = subprocess.run(
            [
                bin_path,
                "--force-ipv4",
                "--no-playlist",
                "--no-warnings",
                "--max-filesize",
                "80M",
                "-f",
                "bestaudio[ext=opus]/bestaudio[ext=m4a]/bestaudio",
                "-o",
                out_tmpl,
                "--",
                f"https://www.youtube.com/watch?v={vid}",
            ],
            text=True,
            capture_output=True,
            timeout=90,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SourcesError("offline audio download timed out") from exc
    except OSError as exc:
        raise SourcesError(f"could not run yt-dlp for audio cache: {exc}") from exc
    if proc.returncode != 0:
        lines = (proc.stderr or proc.stdout or "audio download failed").strip().splitlines()
        raise SourcesError(lines[-1] if lines else "audio download failed")
    for p in sorted(audio_dir.glob(f"{vid}.*")):
        if (
            p.is_file()
            and not p.is_symlink()
            and p.suffix.lower() not in (".part", ".tmp", ".ytdl")
            and p.stat().st_size > 0
        ):
            try:
                os.chmod(p, 0o600)
            except OSError:
                pass
            return str(p)
    raise SourcesError("yt-dlp completed without producing cached audio file")


SPONSORBLOCK_CATEGORIES = (
    "music_offtopic",
    "sponsor",
    "selfpromo",
    "intro",
    "outro",
)


def sponsorblock_segments(video_id: str) -> list[dict[str, Any]]:
    """Query keyless SponsorBlock API for non-music segments; returns [] on miss/error."""
    try:
        vid = _validate_video_id(video_id)
    except SourcesError:
        return []
    cats_json = json.dumps(list(SPONSORBLOCK_CATEGORIES))
    params = urllib.parse.urlencode({"videoID": vid, "categories": cats_json})
    url = f"https://sponsor.ajay.app/api/skipSegments?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            if resp.status == 404:
                return []
            raw = resp.read(512 * 1024 + 1)
            if len(raw) > 512 * 1024:
                return []
            data = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    segments: list[dict[str, Any]] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        seg = entry.get("segment")
        if not isinstance(seg, list) or len(seg) != 2:
            continue
        try:
            start = float(seg[0])
            end = float(seg[1])
        except (TypeError, ValueError):
            continue
        if end > start >= 0.0 and (end - start) >= 1.0:
            segments.append(
                {
                    "start": round(start, 2),
                    "end": round(end, 2),
                    "category": _sanitize_text(entry.get("category")) or "music_offtopic",
                }
            )
    segments.sort(key=lambda s: s["start"])
    return segments

