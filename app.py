import asyncio
import base64
import json
import os
import re
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

import yt_dlp
from aiohttp import web

COOKIES_B64 = os.environ.get("YOUTUBE_COOKIES_B64", "")
URL = os.environ.get("TEST_URL", "https://www.youtube.com/watch?v=LNzCs4lgNXc")

COOKIES_PATH = os.path.join(tempfile.gettempdir(), "diag_cookies.txt")
if COOKIES_B64:
    with open(COOKIES_PATH, "wb") as fh:
        fh.write(base64.b64decode(COOKIES_B64))

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def probe(label: str, **overrides) -> dict:
    start = time.time()
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "retries": 1,
        "http_headers": {"User-Agent": UA},
        "extractor_retries": 1,
    }
    opts.update(overrides)
    if "youtube" == os.environ.get("FORCE_STYLE", ""):
        pass

    result = {
        "label": label,
        "ok": False,
        "n_formats": 0,
        "error": "",
        "elapsed": 0,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(URL, download=False)
        formats = info.get("formats") or []
        result["n_formats"] = len(formats)
        result["ok"] = len(formats) > 0
        vids = [f for f in formats if f.get("vcodec") != "none"]
        result["best_video"] = {
            "format_id": vids[-1].get("format_id"),
            "ext": vids[-1].get("ext"),
            "vcodec": vids[-1].get("vcodec"),
            "filesize": vids[-1].get("filesize") or vids[-1].get("filesize_approx"),
        } if vids else None
    except Exception as exc:
        result["error"] = str(exc).strip()[:3000]
    result["elapsed"] = round(time.time() - start, 1)
    return result


def run_all() -> list[dict]:
    results = []

    def desktop_cookie_opts():
        opts = {}
        if os.path.isfile(COOKIES_PATH):
            opts["cookiefile"] = COOKIES_PATH
        return opts

    def base_with_cookies():
        opts = desktop_cookie_opts()
        opts["http_headers"] = {"User-Agent": UA}
        return opts

    # Localización de deno (para js_runtime / po_token)
    deno = "/home/renderscripts/.deno/bin/deno"
    js = {}
    if os.path.isfile(deno):
        js = {"js_runtimes": {"deno": {"path": deno}}}
    elif os.environ.get("DENO_BIN") and os.path.isfile(os.environ["DENO_BIN"]):
        js = {"js_runtimes": {"deno": {"path": os.environ["DENO_BIN"]}}}

    configs = [
        ("1-default-no-cookies", base_with_cookies()),
        ("2-default-with-cookies", {**base_with_cookies(), **desktop_cookie_opts()}),
    ]

    # This is a bug in generation above (cookies applied twice, but fine).
    # Build explicit list:
    configs = [
        ("default-sin-cookies", {"http_headers": {"User-Agent": UA}}),
        ("default-con-cookies", {"http_headers": {"User-Agent": UA}, "cookiefile": COOKIES_PATH} if os.path.isfile(COOKIES_PATH) else {"http_headers": {"User-Agent": UA}}),
    ]

    jsset = js if js else None
    for client in ["android", "tv", "web", "mweb", "android_vr", "ios"]:
        for label, base in list(configs):
            cfg = dict(base)
            cfg["extractor_args"] = {"youtube": {"player_client": [client]}}
            configs.append((f"{client}-con-cookies", cfg))

    # Configuraciones con cookies, cliente por defecto (la del bot de música)
    if os.path.isfile(COOKIES_PATH):
        configs.append(("musica-bot", {
            "http_headers": {"User-Agent": UA},
            "cookiefile": COOKIES_PATH,
            "format": "bestaudio[ext=m4a]/bestaudio/best",
        }))
        configs.append(("musica-bot-mp3", {
            "http_headers": {"User-Agent": UA},
            "cookiefile": COOKIES_PATH,
            "format": "bestaudio/best",
        }))

    # La que usa el bot de video exactamente (con cookies, cliente default)
    if os.path.isfile(COOKIES_PATH):
        configs.append(("video-bot-exacta", {
            "http_headers": {"User-Agent": UA},
            "cookiefile": COOKIES_PATH,
            "format": "bv*[ext=mp4][vcodec^=avc1]+ba[ext=m4a]/b[ext=mp4][vcodec^=avc1]/bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b/wv*+wa/w",
        }))

    # js_runtime (po_token) con tv y web
    for client in ["tv", "web"]:
        if jsset:
            cfg = {"http_headers": {"User-Agent": UA}}
            if os.path.isfile(COOKIES_PATH):
                cfg["cookiefile"] = COOKIES_PATH
            cfg.update(jsset)
            cfg["extractor_args"] = {
                "youtube": {"player_client": [client]},
            }
            configs.append((f"{client}-con-cookies-po", cfg))

    # Salida de los intentos (cascada del bot de video): default->relax->android->web_sdk
    cascade = []
    if os.path.isfile(COOKIES_PATH):
        base = {"http_headers": {"User-Agent": UA}, "cookiefile": COOKIES_PATH}
        cascade.append(("video-cascada-1-default", dict(base)))
        relaxed = dict(base)
        relaxed["format"] = "bv*+ba/b/wv*+wa/w"
        cascade.append(("video-cascada-2-relax", relaxed))
        android = dict(relaxed)
        android["extractor_args"] = {"youtube": {"player_client": ["android", "tv", "android_vr"]}}
        cascade.append(("video-cascada-3-android", android))
        websdk = dict(relaxed)
        websdk["extractor_args"] = {"youtube": {"player_client": ["tv", "web"]}}
        cascade.append(("video-cascada-4-websdk", websdk))
    configs += cascade

    seen = set()
    for label, opts in configs:
        if label in seen:
            continue
        seen.add(label)
        configs_dedup.append((label, opts))

    CACHE = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(probe, label, **opts): label for label, opts in configs_dedup}
        for fut in futures:
            label = futures[fut]
            r = fut.result()
            CACHE[label] = r
    results = [CACHE[l] for l, _ in configs_dedup]
    return results


async def handler(request: web.Request) -> web.Response:
    global CACHED
    if CACHED is None:
        CACHED = await asyncio.to_thread(run_all)
    return web.json_response({"url": URL, "cookies": bool(os.path.isfile(COOKIES_PATH)), "results": CACHED})


CACHED = None


async def main() -> None:
    app = web.Application()
    app.router.add_get("/", handler)
    app.router.add_get("/health", handler)
    port = int(os.environ.get("PORT", "10000"))
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    print(f"listening on {port}", flush=True)
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())