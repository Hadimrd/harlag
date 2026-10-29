import os, re, time, uuid, secrets, shutil, ipaddress, subprocess, json, asyncio, threading, base64
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlparse, quote_plus
from urllib.request import Request as URLRequest, urlopen
from html import unescape
from html.parser import HTMLParser

from fastapi import FastAPI, Header, HTTPException, Request, Depends
from fastapi.responses import FileResponse
from yt_dlp import YoutubeDL
from yt_dlp.extractor import gen_extractor_classes
from shazamio import Serialize, Shazam

APP_NAME = "cf-downloader-api"
API_KEY = os.environ.get("API_KEY", "")
PORT = int(os.environ.get("PORT", "10000"))
MAX_BYTES = 100 * 1024 * 1024
MAX_DURATION = 30 * 60
FILE_TTL = 30 * 60
MEDIA_DIR = Path("/tmp/cf-downloader-media")
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
FILES = {}
RATE = {}
# Prevent concurrent yt-dlp/ffmpeg jobs from exhausting the small Render instance.
DOWNLOAD_LOCK = threading.Lock()
# Main platforms supported by yt-dlp. Support still depends on each site's
# public accessibility and any anti-bot/login controls.
ALLOWED_HOSTS = (
    "youtube.com", "youtu.be", "youtube-nocookie.com", "youtubekids.com",
    "instagram.com", "tiktok.com", "vm.tiktok.com", "vt.tiktok.com", "douyin.com",
    "facebook.com", "fb.watch", "m.facebook.com", "x.com", "twitter.com",
    "reddit.com", "redd.it", "v.redd.it", "vimeo.com", "dailymotion.com", "dai.ly",
    "twitch.tv", "clips.twitch.tv", "soundcloud.com", "on.soundcloud.com", "bandcamp.com",
    "streamable.com", "pinterest.com", "pin.it", "snapchat.com", "story.snapchat.com",
    "threads.net", "tumblr.com", "linkedin.com", "rumble.com", "bilibili.com",
    "bilibili.tv", "b23.tv", "aparat.com", "namasha.com", "archive.org",
    "mixcloud.com", "odysee.com", "vk.com", "vkvideo.ru", "ok.ru",
    "likee.video", "kwai.com", "capcut.com", "loom.com", "imgur.com",
    "giphy.com", "coub.com", "9gag.com", "medal.tv", "streamja.com",
    "kick.com", "patreon.com", "tnaflix.com", "flickr.com", "photobucket.com",
    "bitchute.com", "brighteon.com", "rumble.com", "weverse.io", "weibo.com",
    "xiaohongshu.com", "xhslink.com"
)
YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com", "youtubekids.com")
FORMATS = [
    {"id":"best","label":"🎬 بهترین کیفیت","ext":"mp4"},
    {"id":"1080","label":"🎬 1080p","ext":"mp4"},
    {"id":"720","label":"🎬 720p","ext":"mp4"},
    {"id":"480","label":"🎬 480p","ext":"mp4"},
    {"id":"mp3","label":"🎵 فقط صدا (MP3)","ext":"mp3"}
]
app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None)

def auth(x_api_key: str | None = Header(default=None, alias="X-API-Key")):
    if not API_KEY:
        raise HTTPException(503, "API_KEY is not configured")
    if not x_api_key or not secrets.compare_digest(x_api_key, API_KEY):
        raise HTTPException(401, "Unauthorized")

SPECIFIC_EXTRACTORS = None
DIRECT_MEDIA_EXTENSIONS = {
    ".mp4", ".m4v", ".mov", ".webm", ".mkv", ".mp3", ".m4a",
    ".aac", ".wav", ".ogg", ".opus", ".flac", ".avi", ".mpg", ".mpeg",
    ".m3u8", ".mpd", ".ts", ".flv", ".wmv", ".3gp"
}


def _has_specific_extractor(url: str) -> bool:
    global SPECIFIC_EXTRACTORS
    if SPECIFIC_EXTRACTORS is None:
        SPECIFIC_EXTRACTORS = tuple(
            ie for ie in gen_extractor_classes()
            if ie.__name__ not in ("GenericIE", "UnsupportedURLIE")
        )
    for ie in SPECIFIC_EXTRACTORS:
        try:
            if ie.suitable(url):
                return True
        except Exception:
            continue
    return False


def validate_url(url: str):
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password or p.port not in (None,80,443):
        raise HTTPException(400, "INVALID_MEDIA_URL")
    host = p.hostname.lower().rstrip(".")
    if host in ("localhost",) or host.endswith((".local", ".internal", ".localhost")):
        raise HTTPException(400, "PRIVATE_HOST_NOT_ALLOWED")
    try:
        addr = ipaddress.ip_address(host)
        if not addr.is_global:
            raise HTTPException(400, "PRIVATE_ADDRESS_NOT_ALLOWED")
    except ValueError:
        pass
    known_host = any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)
    direct_media = Path(p.path.lower()).suffix in DIRECT_MEDIA_EXTENSIONS
    # Match every specific yt-dlp extractor, not only a short hard-coded list.
    # GenericIE is intentionally excluded so arbitrary website URLs are not fetched blindly.
    if not known_host and not direct_media and not _has_specific_extractor(url):
        raise HTTPException(400, "UNSUPPORTED_PLATFORM")

def rate_limit(request: Request):
    now = time.time()
    client = request.client.host if request.client else "unknown"
    recent = [x for x in RATE.get(client, []) if now-x < 60]
    if len(recent) >= 12:
        raise HTTPException(429, "Rate limit exceeded")
    recent.append(now)
    RATE[client] = recent

def opts(**extra):
    return {"quiet":True,"no_warnings":True,"noplaylist":True,"socket_timeout":20,"retries":1,"extractor_retries":1,"cachedir":False,**extra}

def _is_youtube(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return any(host == h or host.endswith("." + h) for h in YOUTUBE_HOSTS)


def _youtube_cookie_file():
    """Load optional owner-supplied Netscape-format cookies from Render env vars."""
    encoded = (os.environ.get("YOUTUBE_COOKIES_B64") or "").strip()
    raw = os.environ.get("YOUTUBE_COOKIES") or ""
    if not encoded and not raw:
        return None
    try:
        data = base64.b64decode(encoded, validate=True).decode("utf-8") if encoded else raw
        if not data.strip() or (".youtube.com" not in data and "youtube.com" not in data):
            print("youtube_cookie_config_invalid=unexpected_format")
            return None
        cookie_path = Path("/tmp/cf-downloader-youtube-cookies.txt")
        if not cookie_path.exists() or cookie_path.read_text(encoding="utf-8", errors="replace") != data:
            cookie_path.write_text(data, encoding="utf-8")
            cookie_path.chmod(0o600)
        return str(cookie_path)
    except Exception as exc:
        print(f"youtube_cookie_config_invalid={type(exc).__name__}")
        return None


def _extractor_profiles(url: str):
    # The external PO-token provider helps with token-required clients, but cannot
    # remove IP-based bot checks. Try public clients first; cookies may be supplied
    # by the service owner through a private Render environment variable if needed.
    if _is_youtube(url):
        return [
            # Try a public client without a required PO token before using mweb.
            {"youtube": {"player_client": ["android_vr"]}},
            {"youtube": {"player_client": ["mweb"]}},
            {"youtube": {"player_client": ["web_safari"]}},
            {"youtube": {"player_client": ["web_embedded"]}},
            {"youtube": {"player_client": ["tv"]}},
            None,
        ]
    return [None]


def _public_extract_error(url: str, exc: Exception) -> str:
    message = str(exc).lower()
    if _is_youtube(url) and any(
        marker in message for marker in
        ("sign in to confirm", "not a bot", "confirm you're not a bot", "po token", "proof of origin")
    ):
        return "YOUTUBE_BOT_CHECK"
    if any(marker in message for marker in ("private", "login required", "sign in to view", "members-only")):
        return "MEDIA_REQUIRES_LOGIN"
    return "MEDIA_UNAVAILABLE"


def _clean_attempt_files(folder: Path):
    for item in folder.iterdir():
        try:
            shutil.rmtree(item) if item.is_dir() else item.unlink()
        except OSError:
            pass


def _extract_download(url: str, settings: dict, folder: Path):
    last_error = None
    for extractor_args in _extractor_profiles(url):
        attempt_settings = dict(settings)
        if extractor_args:
            attempt_settings["extractor_args"] = extractor_args
        else:
            attempt_settings.pop("extractor_args", None)
        if _is_youtube(url):
            cookie_file = _youtube_cookie_file()
            if cookie_file:
                attempt_settings["cookiefile"] = cookie_file
        try:
            with YoutubeDL(attempt_settings) as ydl:
                return ydl.extract_info(url, download=True)
        except Exception as exc:
            last_error = exc
            print(f"extract_download_failed host={urlparse(url).hostname} error={type(exc).__name__}: {str(exc)[:180]}")
            _clean_attempt_files(folder)
    raise HTTPException(422, _public_extract_error(url, last_error or RuntimeError("extract failed"))) from last_error


def get_info(url: str):
    validate_url(url)
    last_error = None
    info = None
    for extractor_args in _extractor_profiles(url):
        settings = opts(skip_download=True)
        if extractor_args:
            settings["extractor_args"] = extractor_args
        if _is_youtube(url):
            cookie_file = _youtube_cookie_file()
            if cookie_file:
                settings["cookiefile"] = cookie_file
        try:
            with YoutubeDL(settings) as ydl:
                info = ydl.extract_info(url, download=False)
            if info:
                break
        except Exception as exc:
            last_error = exc
    if not info:
        if last_error:
            print(f"extract_info_failed host={urlparse(url).hostname} error={type(last_error).__name__}: {str(last_error)[:240]}")
            raise HTTPException(422, _public_extract_error(url, last_error)) from last_error
        raise HTTPException(422, "MEDIA_UNAVAILABLE")
    if info.get("is_live"):
        raise HTTPException(422, "LIVE_STREAM_UNSUPPORTED")
    if info.get("duration") and info["duration"] > MAX_DURATION:
        raise HTTPException(413, "MEDIA_TOO_LONG_MAX_30_MIN")
    return info

def clean_files():
    now = time.time()
    for key, item in list(FILES.items()):
        if item["expires"] < now:
            shutil.rmtree(item["directory"], ignore_errors=True)
            FILES.pop(key, None)

def _is_instagram_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return host == "instagram.com" or host.endswith(".instagram.com")


class _InstagramMetaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.descriptions = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "meta":
            return
        values = dict(attrs)
        key = (values.get("property") or values.get("name") or "").lower()
        if key in ("og:description", "description", "twitter:description") and values.get("content"):
            self.descriptions.append(values["content"])


def _instagram_caption_fallback(url: str) -> str:
    # Best-effort fallback for public pages whose extractor metadata omits the caption.
    try:
        request = URLRequest(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.8",
        })
        with urlopen(request, timeout=4) as response:
            page = response.read(1_500_000).decode("utf-8", "replace")
    except Exception as exc:
        print(f"instagram_caption_fallback_failed: {type(exc).__name__}")
        return ""
    parser = _InstagramMetaParser()
    parser.feed(page)
    for item in parser.descriptions:
        value = unescape(item).strip()
        if value and not value.lower().startswith(("log in", "instagram")):
            return value[:4000]
    return ""


def media_caption(url: str, info: dict) -> str:
    caption = (info.get("description") or info.get("caption") or info.get("alt_title") or "").strip()
    if not caption and _is_instagram_url(url):
        caption = _instagram_caption_fallback(url)
    return caption

def safe_filename(name, ext):
    name = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', "_", name or "download").strip(" .")
    return (name[:120] or "download") + "." + ext


def _normalized_track_text(value: str) -> str:
    return re.sub(r"[\W_]+", "", (value or "").casefold(), flags=re.UNICODE)


def _deezer_preview(title: str, artist: str) -> dict:
    """Return an official Deezer 30-second preview when the catalogue match is close."""
    query = quote_plus((title + " " + artist).strip())
    request = URLRequest(
        f"https://api.deezer.com/search?q={query}&limit=5",
        headers={"User-Agent": "Mozilla/5.0 (compatible; MusicIdentifier/1.0)"},
    )
    try:
        with urlopen(request, timeout=3) as response:
            payload = json.loads(response.read(500_000).decode("utf-8", "replace"))
        data = payload.get("data") or []
    except Exception as exc:
        print(f"deezer_preview_lookup_failed error={type(exc).__name__}")
        return {}
    wanted_title = _normalized_track_text(title)
    wanted_artist = _normalized_track_text(artist)
    best = None
    for item in data:
        got_title = _normalized_track_text(str(item.get("title") or "") + " " + str(item.get("title_version") or ""))
        got_artist = _normalized_track_text((item.get("artist") or {}).get("name") or "")
        if not got_title or not got_artist:
            continue
        title_score = SequenceMatcher(None, wanted_title, got_title).ratio()
        artist_score = SequenceMatcher(None, wanted_artist, got_artist).ratio()
        if wanted_title and (wanted_title in got_title or got_title in wanted_title):
            title_score = max(title_score, 0.92)
        if wanted_artist and (wanted_artist in got_artist or got_artist in wanted_artist):
            artist_score = max(artist_score, 0.92)
        score = 0.7 * title_score + 0.3 * artist_score
        if best is None or score > best[0]:
            best = (score, item)
    if not best or best[0] < 0.72:
        return {}
    item = best[1]
    preview = item.get("preview") or ""
    if not preview.startswith("https://"):
        return {}
    return {"deezer_preview_url": preview, "deezer_url": item.get("link") or ""}

@app.get("/")
def root():
    return {"service":APP_NAME,"ok":True,"endpoints":["/health","/probe?url=...","/download?url=...&spec=best","/identify?url=..."]}

@app.get("/health")
def health():
    provider = {"ok": False, "version": None}
    try:
        with urlopen("http://127.0.0.1:4416/ping", timeout=1) as response:
            if response.status == 200:
                data = json.loads(response.read(4096).decode("utf-8", "replace"))
                provider = {"ok": True, "version": data.get("version")}
    except Exception as exc:
        provider = {"ok": False, "error": type(exc).__name__}
    return {
        "ok": True,
        "service": APP_NAME,
        "api_key_configured": bool(API_KEY),
        "youtube_cookies_configured": bool(os.environ.get("YOUTUBE_COOKIES_B64") or os.environ.get("YOUTUBE_COOKIES")),
        "yt_dlp": "enabled",
        "music_recognition": "shazamio",
        "youtube_po_token_provider": provider,
    }

@app.get("/probe", dependencies=[Depends(auth)])
def probe(url: str, request: Request):
    rate_limit(request)
    info = get_info(url)
    return {"url":url,"title":info.get("title") or "بدون عنوان","caption":media_caption(url, info),"duration":info.get("duration") or 0,
            "thumbnail":info.get("thumbnail") or "","filesize":info.get("filesize") or info.get("filesize_approx") or 0,
            "artist":info.get("artist") or info.get("creator"),"song":info.get("track"),"formats":FORMATS}

@app.get("/recognize", dependencies=[Depends(auth)])
def recognize(url: str, request: Request):
    rate_limit(request)
    info = get_info(url)
    title = info.get("track") or info.get("title")
    artist = info.get("artist") or info.get("creator")
    if not title and not artist:
        return {"title":None,"artist":None}
    return {"title":title or "نامشخص","artist":artist or "نامشخص","cover":info.get("thumbnail"),"duration":info.get("duration"),"source":"metadata"}


@app.get("/identify", dependencies=[Depends(auth)])
async def identify(url: str, request: Request):
    """Identify music from multiple sections of a clip, including instrumental passages."""
    rate_limit(request)
    clean_files()
    validate_url(url)
    folder = MEDIA_DIR / ("identify-" + uuid.uuid4().hex)
    folder.mkdir(parents=True, exist_ok=False)
    preview_folders = []
    try:
        # Reuse media already fetched for this Telegram job so identification is faster.
        cached = next(
            (
                item for item in FILES.values()
                if item.get("source_url") == url
                and item["expires"] > time.time()
                and item["path"].exists()
                and not item.get("is_preview")
            ),
            None,
        )
        if cached:
            source_path = cached["path"]
            media_duration = 0.0
        else:
            settings = opts(
                format="bestaudio/best",
                outtmpl=str(folder / "%(id)s.%(ext)s"),
                max_filesize=MAX_BYTES,
                restrictfilenames=True,
                continuedl=False,
                nopart=True,
            )
            info = await asyncio.to_thread(_extract_download, url, settings, folder)
            audio_files = [
                p for p in folder.iterdir()
                if p.is_file() and not p.name.endswith((".part", ".ytdl", ".tmp", ".wav"))
            ]
            if not audio_files:
                raise HTTPException(502, "AUDIO_EXTRACTION_FAILED")
            source_path = max(audio_files, key=lambda p: p.stat().st_size)
            media_duration = float(info.get("duration") or 0)

        if not media_duration:
            try:
                probe_result = await asyncio.to_thread(
                    subprocess.run,
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", str(source_path)],
                    check=True, capture_output=True, text=True, timeout=10,
                )
                media_duration = max(0.0, float(probe_result.stdout.strip() or 0))
            except Exception:
                media_duration = 0.0

        base = str(request.base_url).rstrip("/")
        # Make the complete audio track of the user-supplied clip available as MP3.
        # This is the clip's audio, not a download of the full commercial song.
        clip_audio_url = None
        clip_audio_size = 0
        clip_audio_filename = "clip-audio.mp3"
        try:
            clip_audio_folder = MEDIA_DIR / ("clip-audio-" + uuid.uuid4().hex)
            clip_audio_folder.mkdir(parents=True, exist_ok=False)
            clip_audio_path = clip_audio_folder / "clip-audio.mp3"
            await asyncio.to_thread(
                subprocess.run,
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-threads", "1",
                 "-i", str(source_path), "-t", "30",
                 "-vn", "-codec:a", "libmp3lame", "-b:a", "128k", str(clip_audio_path)],
                check=True, capture_output=True, text=True, timeout=120,
            )
            if clip_audio_path.exists() and 1000 < clip_audio_path.stat().st_size <= MAX_BYTES:
                clip_token = secrets.token_hex(24)
                clip_audio_size = clip_audio_path.stat().st_size
                clip_audio_filename = safe_filename("Audio preview from supplied clip", "mp3")
                FILES[clip_token] = {
                    "path": clip_audio_path,
                    "directory": clip_audio_folder,
                    "expires": time.time() + FILE_TTL,
                    "filename": clip_audio_filename,
                    "ext": ".mp3",
                    "source_url": url,
                    "is_preview": True,
                }
                clip_audio_url = base + "/files/" + clip_token
            else:
                shutil.rmtree(clip_audio_folder, ignore_errors=True)
        except Exception as exc:
            print(f"full_clip_audio_failed error={type(exc).__name__}: {str(exc)[:120]}")
            try:
                shutil.rmtree(clip_audio_folder, ignore_errors=True)
            except Exception:
                pass
        # Listen across the whole clip. Songs often start after speech; a remix or
        # an instrumental section may be audible only in the middle/end.
        offsets = [0.0]
        if media_duration > 15:
            offsets.extend([
                max(0.0, media_duration * 0.33 - 6),
                max(0.0, media_duration * 0.66 - 6),
                max(0.0, media_duration - 12),
            ])
        offsets = list(dict.fromkeys(round(x, 2) for x in offsets))[:4]

        recognized_tracks = {}
        async with Shazam() as shazam:
            for index, offset in enumerate(offsets):
                sample_wav = folder / f"sample-{index}.wav"
                try:
                    await asyncio.to_thread(
                        subprocess.run,
                        [
                            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                            "-ss", str(offset), "-i", str(source_path), "-t", "12",
                            "-vn", "-ac", "2", "-ar", "44100", "-af",
                            "highpass=f=90,lowpass=f=9000", "-f", "wav", str(sample_wav),
                        ],
                        check=True, capture_output=True, text=True, timeout=30,
                    )
                except Exception as exc:
                    print(f"audio_sample_failed offset={offset} error={type(exc).__name__}")
                    continue
                if not sample_wav.exists() or sample_wav.stat().st_size < 8000:
                    continue
                try:
                    candidate = await shazam.recognize(str(sample_wav))
                except Exception as exc:
                    print(f"shazam_window_failed offset={offset} error={type(exc).__name__}: {str(exc)[:120]}")
                    continue
                if not candidate or not candidate.get("track"):
                    continue
                try:
                    track = Serialize.full_track(candidate).track
                except Exception:
                    continue
                title = (getattr(track, "title", None) or "").strip()
                artist = (getattr(track, "subtitle", None) or "").strip()
                if not title:
                    continue
                key = (title.casefold(), artist.casefold())
                if key in recognized_tracks:
                    recognized_tracks[key]["hits"] += 1
                else:
                    recognized_tracks[key] = {
                        "title": title,
                        "artist": artist or "نامشخص",
                        "track": track,
                        "offset": offset,
                        "hits": 1,
                    }

        if not recognized_tracks:
            print(f"song_identification_no_match host={urlparse(url).hostname} windows={len(offsets)}")
            return {"recognized": False, "matches": [], "message": "NO_CONFIDENT_MATCH", "clip_audio_url": clip_audio_url, "clip_audio_size": clip_audio_size, "clip_audio_filename": clip_audio_filename, "clip_audio_duration_seconds": min(media_duration or 0, 30)}

        candidates = sorted(
            recognized_tracks.values(),
            key=lambda item: (item["hits"], -item["offset"]),
            reverse=True,
        )[:4]
        matches = []
        for index, item in enumerate(candidates):
            track = item["track"]
            title = item["title"]
            artist = item["artist"]
            query = quote_plus(f"{title} {artist}")
            deezer_preview = await asyncio.to_thread(_deezer_preview, title, artist)
            preview_url = None
            preview_folder = MEDIA_DIR / ("preview-" + uuid.uuid4().hex)
            preview_folder.mkdir(parents=True, exist_ok=False)
            preview_folders.append(preview_folder)
            preview_path = preview_folder / "clip-audio-preview.mp3"
            try:
                await asyncio.to_thread(
                    subprocess.run,
                    [
                        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-ss", str(item["offset"]), "-i", str(source_path), "-t", "12",
                        "-vn", "-codec:a", "libmp3lame", "-b:a", "128k", str(preview_path),
                    ],
                    check=True, capture_output=True, text=True, timeout=30,
                )
                if preview_path.exists() and preview_path.stat().st_size > 1000:
                    token = secrets.token_hex(24)
                    FILES[token] = {
                        "path": preview_path,
                        "directory": preview_folder,
                        "expires": time.time() + FILE_TTL,
                        "filename": safe_filename(f"{title} - {artist} preview", "mp3"),
                        "ext": ".mp3",
                        "source_url": url,
                        "is_preview": True,
                    }
                    preview_url = base + "/files/" + token
            except Exception as exc:
                print(f"audio_preview_failed index={index} error={type(exc).__name__}")

            matches.append({
                "id": index,
                "title": title,
                "artist": artist,
                "cover": getattr(track, "photo_url", None),
                "shazam_url": getattr(track, "shazam_url", None),
                "apple_music_url": getattr(track, "apple_music_url", None),
                "spotify_url": f"https://open.spotify.com/search/{query}",
                "youtube_url": getattr(track, "youtube_link", None) or f"https://www.youtube.com/results?search_query={query}",
                "lyrics_url": f"https://genius.com/search?q={query}",
                "deezer_preview_url": deezer_preview.get("deezer_preview_url"),
                "deezer_url": deezer_preview.get("deezer_url"),
                "preview_url": preview_url,
                "preview_duration_seconds": 12,
                "preview_source": "clip",
                "confidence_hits": item["hits"],
            })

        first = matches[0]
        return {
            "recognized": True,
            "title": first["title"],
            "artist": first["artist"],
            "cover": first["cover"],
            "shazam_url": first["shazam_url"],
            "apple_music_url": first["apple_music_url"],
            "spotify_url": first["spotify_url"],
            "youtube_url": first["youtube_url"],
            "lyrics_url": first["lyrics_url"],
            "preview_url": first["preview_url"],
            "clip_audio_url": clip_audio_url,
            "clip_audio_size": clip_audio_size,
            "clip_audio_filename": clip_audio_filename,
            "clip_audio_duration_seconds": min(media_duration or 0, 30),
            "matches": matches,
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"song identification failed ({type(e).__name__}): {str(e)[:240]}")
        raise HTTPException(422, "SONG_IDENTIFICATION_FAILED") from e
    finally:
        shutil.rmtree(folder, ignore_errors=True)

@app.get("/download", dependencies=[Depends(auth)])
def download(url: str, spec: str = "best", request: Request = None):
    rate_limit(request) if request else None
    clean_files()
    validate_url(url)
    if spec not in ("best","1080","720","480","mp3"):
        raise HTTPException(400, "Unsupported format")
    token = uuid.uuid4().hex + secrets.token_hex(12)
    folder = MEDIA_DIR / token
    best_video = "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/best"
    selectors = {
        # Pick a source that actually exists; the requested resolution is applied
        # with ffmpeg below. This avoids Instagram's "Requested format is not available".
        "best": best_video,
        "1080": best_video,
        "720": best_video,
        "480": best_video,
        "mp3": "bestaudio/best",
    }
    settings = opts(format=selectors[spec],outtmpl=str(folder/"%(id)s.%(ext)s"),max_filesize=MAX_BYTES,
                    restrictfilenames=True,merge_output_format="mp4",continuedl=False,nopart=True)
    if spec == "mp3":
        settings["postprocessors"]=[{"key":"FFmpegExtractAudio","preferredcodec":"mp3","preferredquality":"192"}]
    if not DOWNLOAD_LOCK.acquire(blocking=False):
        raise HTTPException(429, "DOWNLOAD_BUSY")
    try:
        folder.mkdir(parents=True, exist_ok=False)
        print(f"download_begin host={urlparse(url).hostname} spec={spec}")
        info = _extract_download(url, settings, folder)
        files = [p for p in folder.iterdir() if p.is_file() and not p.name.endswith((".part",".ytdl",".tmp"))]
        if not files:
            raise HTTPException(502,"Downloader produced no file")
        path = max(files,key=lambda p:p.stat().st_size)
        output_width = None
        output_height = None
        if spec in ("720", "480", "1080") and path.suffix.lower() not in (".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac"):
            try:
                probe_result = subprocess.run(
                    ["ffprobe", "-v", "error", "-select_streams", "v:0",
                     "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", str(path)],
                    check=True, capture_output=True, text=True, timeout=10,
                )
                dimensions = (probe_result.stdout.strip().splitlines() or [""])[0].split("x")
                if len(dimensions) >= 2:
                    source_width, source_height = int(dimensions[0]), int(dimensions[1])
                    target_short, target_long = {
                        "480": (480, 854), "720": (720, 1280), "1080": (1080, 1920)
                    }[spec]
                    scale = min(1.0, target_short / min(source_width, source_height),
                                target_long / max(source_width, source_height))
                    output_width = max(2, (int(source_width * scale) // 2) * 2)
                    output_height = max(2, (int(source_height * scale) // 2) * 2)
                    needs_resize = output_width < source_width or output_height < source_height
                    needs_mp4 = path.suffix.lower() != ".mp4"
                    if needs_resize or needs_mp4:
                        scaled_path = folder / (path.stem + "-" + spec + "p.mp4")
                        command = [
                            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                            "-threads", "1", "-filter_threads", "1", "-filter_complex_threads", "1",
                            "-i", str(path), "-threads", "1",
                        ]
                        if needs_resize:
                            command.extend(["-vf", f"scale={output_width}:{output_height}"])
                        command.extend([
                            "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-threads", "1",
                            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
                            str(scaled_path),
                        ])
                        subprocess.run(command, check=True, capture_output=True, text=True, timeout=120)
                        if scaled_path.exists() and scaled_path.stat().st_size > 0:
                            try:
                                path.unlink()
                            except OSError:
                                pass
                            path = scaled_path
            except Exception as exc:
                print(f"resolution_normalize_failed host={urlparse(url).hostname} spec={spec} error={type(exc).__name__}: {str(exc)[:120]}")
                if spec == "mp3":
                    raise
        size = path.stat().st_size
        if size > MAX_BYTES:
            raise HTTPException(413,"File exceeds 100 MB service limit")
        filename = safe_filename(info.get("title") or path.stem,path.suffix.lstrip("."))
        FILES[token]={"path":path,"directory":folder,"expires":time.time()+FILE_TTL,"filename":filename,"ext":path.suffix.lower(),"source_url":url}
        base = str(request.base_url).rstrip("/") if request else ""
        caption = media_caption(url, info)
        print(f"download_ok host={urlparse(url).hostname} size={size} caption_chars={len(caption)}")
        return {
            "file_url": base + "/files/" + token,
            "filename": filename,
            "size": size,
            "title": info.get("title") or info.get("fulltitle") or filename,
            "caption": caption,
            "description": caption,
            "duration": info.get("duration") or 0,
            "width": output_width,
            "height": output_height,
            "expires_in": FILE_TTL,
        }
    except HTTPException:
        shutil.rmtree(folder,ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(folder,ignore_errors=True)
        print(f"download_failed host={urlparse(url).hostname} spec={spec} error={type(e).__name__}: {str(e)[:220]}")
        raise HTTPException(422,"Download failed; this media may require login or be blocked by the platform") from e
    finally:
        DOWNLOAD_LOCK.release()

@app.get("/files/{token}")
def get_file(token: str):
    clean_files()
    item = FILES.get(token)
    if not item or item["expires"] < time.time() or not item["path"].exists():
        raise HTTPException(404,"File link expired or unavailable")
    media_type = "audio/mpeg" if item["ext"] == ".mp3" else "video/mp4"
    return FileResponse(item["path"],media_type=media_type,filename=item["filename"],
                        headers={"Cache-Control":"no-store","X-Content-Type-Options":"nosniff"})
