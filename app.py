import os, re, time, uuid, secrets, shutil, ipaddress, subprocess
from pathlib import Path
from urllib.parse import urlparse, quote_plus

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


def _extractor_profiles(url: str):
    # YouTube increasingly challenges datacenter IPs. Try the TV/Safari clients
    # first, then the default extractor profile; all other sites use the default.
    if _is_youtube(url):
        return [
            {"youtube": {"player_client": ["tv", "web_safari"]}},
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
        try:
            with YoutubeDL(attempt_settings) as ydl:
                return ydl.extract_info(url, download=True)
        except Exception as exc:
            last_error = exc
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

def safe_filename(name, ext):
    name = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', "_", name or "download").strip(" .")
    return (name[:120] or "download") + "." + ext

@app.get("/")
def root():
    return {"service":APP_NAME,"ok":True,"endpoints":["/health","/probe?url=...","/download?url=...&spec=best","/identify?url=..."]}

@app.get("/health")
def health():
    return {"ok":True,"service":APP_NAME,"api_key_configured":bool(API_KEY),"yt_dlp":"enabled","music_recognition":"shazamio"}

@app.get("/probe", dependencies=[Depends(auth)])
def probe(url: str, request: Request):
    rate_limit(request)
    info = get_info(url)
    return {"url":url,"title":info.get("title") or "بدون عنوان","caption":(info.get("description") or info.get("caption") or info.get("alt_title") or "").strip(),"duration":info.get("duration") or 0,
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
    """Identify a track from several 12-second windows and return a short clip-derived audio sample."""
    rate_limit(request)
    clean_files()
    validate_url(url)
    folder = MEDIA_DIR / ("identify-" + uuid.uuid4().hex)
    folder.mkdir(parents=True, exist_ok=False)
    preview_folder = None
    preview_token = None
    try:
        # Reuse the already-downloaded media where possible to avoid a second full download.
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
            info = _extract_download(url, settings, folder)
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
                probe_result = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", str(source_path)],
                    check=True, capture_output=True, text=True, timeout=10,
                )
                media_duration = max(0.0, float(probe_result.stdout.strip() or 0))
            except Exception:
                media_duration = 0.0

        # A song may begin after speech or an intro. Check up to three distinct 12 s windows.
        offsets = [0.0]
        if media_duration > 12:
            offsets.extend([max(0.0, media_duration / 2 - 6), max(0.0, media_duration - 12)])
        offsets = list(dict.fromkeys(round(x, 2) for x in offsets))[:3]

        selected_offset = offsets[0]
        selected_wav = None
        raw = None
        async with Shazam() as shazam:
            for index, offset in enumerate(offsets):
                sample_wav = folder / f"sample-{index}.wav"
                subprocess.run(
                    [
                        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-ss", str(offset), "-i", str(source_path), "-t", "12",
                        "-vn", "-ac", "2", "-ar", "44100", "-f", "wav", str(sample_wav),
                    ],
                    check=True, capture_output=True, text=True, timeout=30,
                )
                if not sample_wav.exists() or sample_wav.stat().st_size < 8000:
                    continue
                try:
                    candidate = await shazam.recognize(str(sample_wav))
                except Exception as exc:
                    print(f"shazam_window_failed offset={offset} error={type(exc).__name__}: {str(exc)[:160]}")
                    continue
                if candidate and candidate.get("track"):
                    raw = candidate
                    selected_offset = offset
                    selected_wav = sample_wav
                    break

        if not raw or not raw.get("track"):
            return {"recognized": False, "message": "NO_CONFIDENT_MATCH"}
        track = Serialize.full_track(raw).track
        title = track.title or "نامشخص"
        artist = track.subtitle or "نامشخص"
        query = quote_plus(f"{title} {artist}")

        # The bot can send the identified segment itself as a short audio preview,
        # extracted from the source Reel. It does not fetch or redistribute the full song.
        preview_folder = MEDIA_DIR / ("preview-" + uuid.uuid4().hex)
        preview_folder.mkdir(parents=True, exist_ok=False)
        preview_path = preview_folder / "song-preview.mp3"
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-ss", str(selected_offset), "-i", str(source_path), "-t", "12",
                "-vn", "-codec:a", "libmp3lame", "-b:a", "128k", str(preview_path),
            ],
            check=True, capture_output=True, text=True, timeout=30,
        )
        preview_token = secrets.token_hex(24)
        preview_name = safe_filename(f"{title} - {artist} (preview)", "mp3")
        FILES[preview_token] = {
            "path": preview_path,
            "directory": preview_folder,
            "expires": time.time() + FILE_TTL,
            "filename": preview_name,
            "ext": ".mp3",
            "source_url": url,
            "is_preview": True,
        }
        base = str(request.base_url).rstrip("/")
        return {
            "recognized": True,
            "title": title,
            "artist": artist,
            "cover": track.photo_url,
            "shazam_url": track.shazam_url,
            "apple_music_url": track.apple_music_url,
            "spotify_url": f"https://open.spotify.com/search/{query}",
            "youtube_url": track.youtube_link or f"https://www.youtube.com/results?search_query={query}",
            "preview_url": base + "/files/" + preview_token,
            "preview_duration_seconds": 12,
            "preview_source": "clip",
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"song identification failed ({type(e).__name__}): {str(e)[:300]}")
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
    folder.mkdir(parents=True, exist_ok=False)
    selectors = {
        "best": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/best",
        "1080": "bv*[height<=1080]+ba/b[height<=1080]/best[height<=1080]",
        "720": "bv*[height<=720]+ba/b[height<=720]/best[height<=720]",
        "480": "bv*[height<=480]+ba/b[height<=480]/best[height<=480]",
        "mp3": "bestaudio/best",
    }
    settings = opts(format=selectors[spec],outtmpl=str(folder/"%(id)s.%(ext)s"),max_filesize=MAX_BYTES,
                    restrictfilenames=True,merge_output_format="mp4",continuedl=False,nopart=True)
    if spec == "mp3":
        settings["postprocessors"]=[{"key":"FFmpegExtractAudio","preferredcodec":"mp3","preferredquality":"192"}]
    try:
        info = _extract_download(url, settings, folder)
        files = [p for p in folder.iterdir() if p.is_file() and not p.name.endswith((".part",".ytdl",".tmp"))]
        if not files:
            raise HTTPException(502,"Downloader produced no file")
        path = max(files,key=lambda p:p.stat().st_size)
        size = path.stat().st_size
        if size > MAX_BYTES:
            raise HTTPException(413,"File exceeds 100 MB service limit")
        filename = safe_filename(info.get("title") or path.stem,path.suffix.lstrip("."))
        FILES[token]={"path":path,"directory":folder,"expires":time.time()+FILE_TTL,"filename":filename,"ext":path.suffix.lower(),"source_url":url}
        base = str(request.base_url).rstrip("/") if request else ""
        caption = (info.get("description") or info.get("caption") or info.get("alt_title") or "").strip()
        return {
            "file_url": base + "/files/" + token,
            "filename": filename,
            "size": size,
            "title": info.get("title") or info.get("fulltitle") or filename,
            "caption": caption,
            "description": caption,
            "duration": info.get("duration") or 0,
            "expires_in": FILE_TTL,
        }
    except HTTPException:
        shutil.rmtree(folder,ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(folder,ignore_errors=True)
        raise HTTPException(422,"Download failed; this media may require login or be blocked by the platform") from e

@app.get("/files/{token}")
def get_file(token: str):
    clean_files()
    item = FILES.get(token)
    if not item or item["expires"] < time.time() or not item["path"].exists():
        raise HTTPException(404,"File link expired or unavailable")
    media_type = "audio/mpeg" if item["ext"] == ".mp3" else "video/mp4"
    return FileResponse(item["path"],media_type=media_type,filename=item["filename"],
                        headers={"Cache-Control":"no-store","X-Content-Type-Options":"nosniff"})
