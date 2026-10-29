import os, re, time, uuid, secrets, shutil, ipaddress
from pathlib import Path
from urllib.parse import urlparse, quote_plus

from fastapi import FastAPI, Header, HTTPException, Request, Depends
from fastapi.responses import FileResponse
from yt_dlp import YoutubeDL
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
ALLOWED_HOSTS = (
    "youtube.com", "youtu.be", "youtube-nocookie.com", "instagram.com",
    "tiktok.com", "vm.tiktok.com", "vt.tiktok.com", "facebook.com",
    "fb.watch", "x.com", "twitter.com"
)
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

def validate_url(url: str):
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password or p.port not in (None,80,443):
        raise HTTPException(400, "Invalid media URL")
    host = p.hostname.lower().rstrip(".")
    if not any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS):
        raise HTTPException(400, "Unsupported platform")
    try:
        addr = ipaddress.ip_address(host)
        if not addr.is_global:
            raise HTTPException(400, "Private addresses are not allowed")
    except ValueError:
        pass

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

def get_info(url: str):
    validate_url(url)
    try:
        with YoutubeDL(opts(skip_download=True)) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        raise HTTPException(422, "Could not read this media URL; it may be private, unsupported or blocked") from e
    if not info:
        raise HTTPException(422, "No media found")
    if info.get("is_live"):
        raise HTTPException(422, "Live streams are not supported")
    if info.get("duration") and info["duration"] > MAX_DURATION:
        raise HTTPException(413, "Media must be 30 minutes or shorter")
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
    return {"service":APP_NAME,"ok":True,"endpoints":["/health","/probe?url=...","/download?url=...&spec=best"]}

@app.get("/health")
def health():
    return {"ok":True,"service":APP_NAME,"api_key_configured":bool(API_KEY)}

@app.get("/probe", dependencies=[Depends(auth)])
def probe(url: str, request: Request):
    rate_limit(request)
    info = get_info(url)
    return {"url":url,"title":info.get("title") or "بدون عنوان","caption":info.get("description") or info.get("title") or "","duration":info.get("duration") or 0,
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
    """Identify music in a public clip using Shazam's current recognition service."""
    rate_limit(request)
    clean_files()
    validate_url(url)
    token = uuid.uuid4().hex
    folder = MEDIA_DIR / ("identify-" + token)
    folder.mkdir(parents=True, exist_ok=False)
    try:
        settings = opts(
            format="bestaudio/best",
            outtmpl=str(folder / "%(id)s.%(ext)s"),
            max_filesize=MAX_BYTES,
            restrictfilenames=True,
            continuedl=False,
            nopart=True,
        )
        with YoutubeDL(settings) as ydl:
            ydl.extract_info(url, download=True)
        files = [
            p for p in folder.iterdir()
            if p.is_file() and not p.name.endswith((".part", ".ytdl", ".tmp"))
        ]
        if not files:
            raise HTTPException(502, "Audio extraction produced no file")
        audio_path = max(files, key=lambda p: p.stat().st_size)
        async with Shazam() as shazam:
            raw = await shazam.recognize(str(audio_path), max_skew=1e-3)
        if not raw or not raw.get("track"):
            return {"recognized": False, "message": "No confident song match"}
        track = Serialize.full_track(raw).track
        title = track.title or "نامشخص"
        artist = track.subtitle or "نامشخص"
        query = quote_plus(f"{title} {artist}")
        return {
            "recognized": True,
            "title": title,
            "artist": artist,
            "cover": track.photo_url,
            "shazam_url": track.shazam_url,
            "apple_music_url": track.apple_music_url,
            "spotify_url": f"https://open.spotify.com/search/{query}",
            "youtube_url": track.youtube_link or f"https://www.youtube.com/results?search_query={query}",
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(422, "Song identification failed; try another public clip") from e
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
    selectors = {"best":"bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b","1080":"bv*[height<=1080]+ba/b[height<=1080]",
                 "720":"bv*[height<=720]+ba/b[height<=720]","480":"bv*[height<=480]+ba/b[height<=480]","mp3":"bestaudio/best"}
    settings = opts(format=selectors[spec],outtmpl=str(folder/"%(id)s.%(ext)s"),max_filesize=MAX_BYTES,
                    restrictfilenames=True,merge_output_format="mp4",continuedl=False,nopart=True)
    if spec == "mp3":
        settings["postprocessors"]=[{"key":"FFmpegExtractAudio","preferredcodec":"mp3","preferredquality":"192"}]
    try:
        with YoutubeDL(settings) as ydl:
            info = ydl.extract_info(url, download=True)
        files = [p for p in folder.iterdir() if p.is_file() and not p.name.endswith((".part",".ytdl",".tmp"))]
        if not files:
            raise HTTPException(502,"Downloader produced no file")
        path = max(files,key=lambda p:p.stat().st_size)
        size = path.stat().st_size
        if size > MAX_BYTES:
            raise HTTPException(413,"File exceeds 100 MB limit")
        filename = safe_filename(info.get("title") or path.stem,path.suffix.lstrip("."))
        FILES[token]={"path":path,"directory":folder,"expires":time.time()+FILE_TTL,"filename":filename,"ext":path.suffix.lower()}
        base = str(request.base_url).rstrip("/") if request else ""
        return {"file_url":base+"/files/"+token,"filename":filename,"size":size,"expires_in":FILE_TTL}
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
