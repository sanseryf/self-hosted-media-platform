"""Phase 2b — in-browser video playback for Hub, talking directly to Jellyfin.

    POST /api/playback/info       {item_id, device_profile, start_ticks?} -> stream URL + subs + resume
    GET  /api/playback/subtitle?item_id=&src_id=&idx=  -> same-origin VTT proxy for <track src>
    POST /api/playback/start      {item_id, play_session_id, ...}         -> tell Jellyfin playback began
    POST /api/playback/progress   {item_id, play_session_id, ...}         -> heartbeat (10s, from the client)
    POST /api/playback/stopped    {item_id, play_session_id, ...}         -> tears down a transcode
    GET  /api/playback/episodes?series_id=  -> season/episode listing for the episode picker

Every route here is session-gated (unlike library.py, whose whole contract is
"no auth, every route anonymous") — these routes mutate Jellyfin play state
and need a real per-user identity, so they live in their own router rather
than being bolted onto library.py's anonymous one. `_require_session()`
wraps auth.py's `get_session_jellyfin_token()` and 401s when there's no
session. Like every other Hub route, everything else degrades to a graceful
JSON `{"ok": false, ...}` rather than a 500 — a dead/unreachable Jellyfin, a
malformed response, or a missing MediaSource all fail soft.

As an extra local-dev safety net (not required by Jellyfin's auth model,
which uses the per-user token here rather than the admin key), every handler
also short-circuits if JELLYFIN_API_KEY is unset — the same gate
watching.py/library.py already use to keep local dev from ever reaching real
Jellyfin. Real production already has that key set for the anonymous poster
routes, so this changes nothing there; locally it's one more line of defense
alongside HUB_JELLYFIN_LOGIN_ENABLED (which already blocks minting a real
session in the first place).

── The one deliberate security bend: token-in-URL (video only) ─────────────
`<video src>` loads its URL directly and cannot attach an `Authorization`
header, so the per-user Jellyfin token this module reads via
`get_session_jellyfin_token()` MUST appear as `api_key=` in the stream URL
handed back to the browser (the `url` field of `/info`'s response). This is
the one intentional exception to Phase 2a's "the Jellyfin token never leaves
the server" rule, made deliberately rather than by omission:
  - The token is the user's own, scoped to their account — exactly how
    Jellyfin's own web client streams video.
  - The alternative (proxying multi-GB video + Range requests + HLS segment
    rewriting through Hub) turns a light FastAPI app into a streaming
    reverse-proxy and defeats the server's direct-serve/QSV transcode path.
  - The boundary that IS preserved: the token never appears in any JSON
    response *field* — only inside the fully-formed media URL this module
    constructs server-side. No endpoint here ever echoes the raw token back
    as its own value.
Subtitles are NOT part of this bend — each subtitle's `url` points at this
module's own `/subtitle` proxy (session-cookie-authenticated, same origin as
the page), not at Jellyfin directly. Two reasons, one forced and one free:
  - Forced: JF_PUBLIC_URL (Jellyfin's public host, e.g. watch.<domain>) is a
    different origin than Hub's own page. `<video>` doesn't need CORS to
    play a cross-origin stream, but `<track>` does — and with no
    `crossorigin` attribute on `<video>` (there still isn't one; not
    needed), a cross-origin `<track src>` fetch runs in "no-cors" mode and
    gets back an opaque response the browser can't parse as VTT. Cues never
    render, with no console error to point at. Proxying same-origin
    sidesteps this outright rather than betting on Jellyfin's CORS headers.
  - Free bonus: since the proxy is session-gated rather than token-gated,
    the token doesn't need to ride along in the subtitle URL at all — one
    fewer place it's visible in the page's own HTML/DOM.

── Audio track switching: full PlaybackInfo renegotiation, not an in-place
   swap ───────────────────────────────────────────────────────────────────
Unlike subtitles (plain VTT sidecar <track> elements the browser switches
between for free, no server round-trip), Jellyfin doesn't hand back an HLS
manifest with every audio track as an alternate rendition here — the
TranscodingUrl this module returns bakes in ONE selected audio stream
(server-side AAC/EAC3 passthrough or transcode), same as Jellyfin's own web
client for HLS output. Confirming otherwise would need pinning down this
install's exact Jellyfin version and testing its master.m3u8 shape live,
which local dev (no real Jellyfin) can't do — so this takes the always-
correct-if-slower path: switching audio means calling `/info` again with
`audio_stream_index` set, which forwards `AudioStreamIndex` into the
PlaybackInfo POST body Jellyfin already accepts, exactly like a fresh
Play. The frontend re-attaches HLS/video at the same position (`start_ticks`
= current playhead) rather than restarting from 0, so the switch reads as
"a brief rebuffer," not "the video restarted." If a future session confirms
this Jellyfin install's HLS output DOES carry multi-audio EXT-X-MEDIA
renditions, hls.js's native `audioTracks` API could replace this with a
seamless in-manifest switch — flagged here rather than assumed.
"""
from __future__ import annotations

import asyncio
import os
import urllib.parse
import urllib.request
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from projects.auth import get_session_jellyfin_token
from projects.watching import JF_KEY, JF_URL, _item_to_card, _jf_get_async, _jf_post_async

log = logging.getLogger("hub.playback")

router = APIRouter()

# JF_URL is Hub's *server-side* address for Jellyfin — in production that's the
# internal LAN host (fast, private). But the stream/subtitle URLs below are
# handed to the *browser*, which is on the public origin over HTTPS: a LAN
# http:// URL fails there (mixed-content block + not routable remotely). So
# browser-facing URLs use the public address. Falls back to JF_URL when
# JELLYFIN_PUBLIC_URL isn't set (e.g. local dev pointed straight at one host).
JF_PUBLIC_URL = (os.environ.get("JELLYFIN_PUBLIC_URL", "").strip() or JF_URL).rstrip("/")

# Identifies Hub to Jellyfin on every per-user call below (session list entries,
# active-device dashboard, etc.) — cosmetic/diagnostic only, not a credential.
_CLIENT_INFO = 'Client="Hub", Device="Hub Web", DeviceId="hub-server", Version="1.2"'


def _auth_header(token: str) -> dict:
    """Per-user auth header for POSTs to Jellyfin (PlaybackInfo, Sessions/Playing*).
    Sends both forms Jellyfin accepts so this keeps working across server
    versions: X-Emby-Token (bare token) and the full MediaBrowser scheme."""
    return {
        "X-Emby-Token": token,
        "Authorization": f'MediaBrowser {_CLIENT_INFO}, Token="{token}"',
    }


def _abs(url: str | None) -> str | None:
    """Prefix a relative Jellyfin URL (DirectStreamUrl / TranscodingUrl) with
    the PUBLIC base — these are loaded by the browser. Already-absolute URLs
    pass through, but a Jellyfin-emitted absolute LAN URL is rehosted onto the
    public origin too (Jellyfin bakes its own internal host into TranscodingUrl)."""
    if not url:
        return url
    if url.startswith("http://") or url.startswith("https://"):
        # rehost Jellyfin's internal host onto the public one (keep path+query)
        try:
            parsed = urllib.parse.urlsplit(url)
            return f"{JF_PUBLIC_URL}{parsed.path}" + (f"?{parsed.query}" if parsed.query else "")
        except Exception:  # noqa: BLE001
            return url
    if not url.startswith("/"):
        url = "/" + url
    return f"{JF_PUBLIC_URL}{url}"


def _ensure_api_key(url: str, token: str) -> str:
    """Guarantee the URL the browser will load carries api_key= — Jellyfin
    doesn't always echo it back in DirectStreamUrl/TranscodingUrl depending
    on server version, so append it ourselves when missing rather than
    trusting it's already there."""
    if "api_key=" in url:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}api_key={urllib.parse.quote(token)}"


def _jf_get_raw(path: str, token: str) -> bytes:
    """Blocking GET against Jellyfin's *internal* URL (JF_URL, not the public
    one), returning the raw response body rather than parsed JSON —
    _jf_get_async/_jf_post_async in watching.py always call json.loads() on
    the response, which a VTT body isn't. Used only by /subtitle below.
    Talks to JF_URL (server-to-server) precisely so it does NOT inherit the
    cross-origin problem it exists to route around."""
    url = f"{JF_URL}{path}"
    req = urllib.request.Request(url, headers=_auth_header(token))
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read()


async def _jf_get_raw_async(path: str, token: str) -> bytes:
    """asyncio.to_thread wrapper — see _jf_get_raw's docstring and
    _jf_get_async's (watching.py) for why blocking urllib needs this."""
    return await asyncio.to_thread(_jf_get_raw, path, token)


async def _require_session(request: Request):
    """Returns (jellyfin_user_id, jellyfin_token) or None. Callers 401 on
    None — that's a real auth failure, distinct from the ok:false-but-200
    degrade path used for "logged in, but Jellyfin itself is unreachable"."""
    return await get_session_jellyfin_token(request)


def _unauthorized() -> JSONResponse:
    return JSONResponse({"ok": False, "error": "Not logged in"}, status_code=401)


# Absolute ceiling for a requested stream cap — the same 120 Mbps this module
# has always used as its effective "source quality / no cap" bitrate. A client
# quality preset never exceeds this; a value of 0/None also means "no cap" and
# resolves to this. Kept as a named constant so the default and the clamp can
# never drift apart.
_MAX_STREAMING_BITRATE = 120000000
# Floor to reject a nonsense/hostile tiny value that would make every title
# unplayably over-compressed — well below the lowest real preset (the client's
# "Data saver" is 720 kbps).
_MIN_STREAMING_BITRATE = 100000


def _resolve_max_bitrate(requested: int | None) -> int:
    """Turn a client's requested cap into the MaxStreamingBitrate Jellyfin
    gets. 0/None/negative -> the 120 Mbps "no cap" default; anything else is
    clamped into [_MIN, _MAX] so a bad request can't ask for an unplayable
    stream or bypass the ceiling."""
    if not requested or requested <= 0:
        return _MAX_STREAMING_BITRATE
    return max(_MIN_STREAMING_BITRATE, min(int(requested), _MAX_STREAMING_BITRATE))


class PlaybackInfoBody(BaseModel):
    item_id: str
    device_profile: dict
    start_ticks: int | None = None
    # Set on an audio-track switch (never on the initial /info call): forces
    # Jellyfin to renegotiate PlaybackInfo with this stream selected instead
    # of its own default. See this module's docstring ("Audio track
    # switching") for why that's a full renegotiation, not an in-place change.
    audio_stream_index: int | None = None
    # Selectable streaming quality: the client's chosen max bitrate (bps) from
    # its quality menu. None/0 means "no cap" (source quality). Applied both as
    # the top-level MaxStreamingBitrate below AND onto the DeviceProfile Jellyfin
    # weighs — the frontend already mirrors it into device_profile, but this
    # re-asserts it server-side so the cap holds even if a client omits it there.
    max_bitrate: int | None = None


class ProgressBody(BaseModel):
    item_id: str
    play_session_id: str
    position_ticks: int = 0
    is_paused: bool = False
    media_source_id: str | None = None


@router.post("/info")
async def playback_info(body: PlaybackInfoBody, request: Request):
    """Negotiates a playable stream for one item against Jellyfin's
    PlaybackInfo endpoint, using the browser-generated DeviceProfile. Never
    returns the raw token as its own field — only baked into `url` / each
    subtitle's `url`."""
    sess = await _require_session(request)
    if sess is None:
        return _unauthorized()
    uid, token = sess

    item_id = (body.item_id or "").strip()
    if not (JF_KEY and item_id):
        return {"ok": False, "error": "Jellyfin unavailable", "mode": None, "url": None,
                "play_session_id": None, "subtitles": [], "audio_tracks": [], "selected_audio_index": None}

    # Resolve the resume position BEFORE PlaybackInfo so it can seed StartTimeTicks
    # — for HLS that makes the transcode start at the resume point instead of 0.
    # PlaybackInfo's own response does NOT carry UserData, so read the saved
    # PlaybackPositionTicks from the user's item view. A client-supplied
    # start_ticks (e.g. the episode picker) takes precedence. (This is the fix
    # for "playback always restarted from 0" — resume_ticks used to read UserData
    # off the PlaybackInfo response, which never contains it, so it was always 0.)
    resume_ticks = body.start_ticks or 0
    # Fetch the item's own metadata once, up front — it carries BOTH the resume
    # position AND the FULL source stream list. Critical: the PlaybackInfo
    # response below reflects the *transcode* and filters MediaStreams down to
    # the single picked audio/subtitle, so the track menus MUST be built from
    # this full list, not from PlaybackInfo. Best-effort; never block playback.
    item_meta = {}
    try:
        item_meta = await _jf_get_async(
            f"/Users/{uid}/Items/{urllib.parse.quote(item_id)}",
            Fields="MediaSources,MediaStreams",
        ) or {}
    except Exception:  # noqa: BLE001
        item_meta = {}
    if not resume_ticks:
        resume_ticks = (item_meta.get("UserData") or {}).get("PlaybackPositionTicks") or 0

    max_bitrate = _resolve_max_bitrate(body.max_bitrate)
    # Assert the cap on the DeviceProfile too, not just the top-level field:
    # Jellyfin's transcode decision reads MaxStreamingBitrate off the profile,
    # so a profile still advertising 120 Mbps could let a "720p" request slip
    # through as source quality. This is a shallow copy edit of the client's
    # dict; harmless if the key was already there.
    device_profile = dict(body.device_profile or {})
    device_profile["MaxStreamingBitrate"] = max_bitrate

    payload = {
        "DeviceProfile": device_profile,
        "MaxStreamingBitrate": max_bitrate,
        "StartTimeTicks": resume_ticks,
        "EnableDirectPlay": True,
        "EnableDirectStream": True,
        "EnableTranscoding": True,
        "AllowVideoStreamCopy": True,
        "AllowAudioStreamCopy": True,
        "AutoOpenLiveStream": False,
    }
    # Only sent on a switch — omitting it on the initial call lets Jellyfin
    # pick its own default (DefaultAudioStreamIndex), same as before this
    # feature existed.
    if body.audio_stream_index is not None:
        payload["AudioStreamIndex"] = body.audio_stream_index
    try:
        data = await _jf_post_async(
            f"/Items/{urllib.parse.quote(item_id)}/PlaybackInfo",
            payload, headers=_auth_header(token), params={"UserId": uid},
        )
    except Exception as e:  # noqa: BLE001 — JF down, network error, bad token, etc.
        return {"ok": False, "error": str(e), "mode": None, "url": None,
                "play_session_id": None, "subtitles": [], "audio_tracks": [], "selected_audio_index": None}

    sources = data.get("MediaSources") or []
    if not sources:
        return {"ok": False, "error": "No playable media source", "mode": None,
                "url": None, "play_session_id": None, "subtitles": [], "audio_tracks": [], "selected_audio_index": None}
    src = sources[0]
    src_id = src.get("Id") or item_id
    play_session_id = data.get("PlaySessionId") or ""

    if src.get("SupportsDirectPlay"):
        mode = "direct"
        url = (
            f"{JF_PUBLIC_URL}/Videos/{urllib.parse.quote(src_id)}/stream"
            f"?static=true&mediaSourceId={urllib.parse.quote(src_id)}"
            f"&playSessionId={urllib.parse.quote(play_session_id)}"
            f"&api_key={urllib.parse.quote(token)}"
        )
    elif src.get("SupportsDirectStream") and src.get("DirectStreamUrl"):
        mode = "direct"
        url = _ensure_api_key(_abs(src["DirectStreamUrl"]), token)
    elif src.get("SupportsTranscoding") and src.get("TranscodingUrl"):
        mode = "hls"
        url = _ensure_api_key(_abs(src["TranscodingUrl"]), token)
    else:
        return {"ok": False, "error": "Jellyfin offered no playable mode", "mode": None,
                "url": None, "play_session_id": None, "subtitles": [], "audio_tracks": [], "selected_audio_index": None}

    # Track menus come from the item's FULL source stream list (fetched above),
    # NOT src.MediaStreams — the latter is transcode-filtered to one audio/one
    # subtitle, which is exactly why dual-audio titles showed no menus. Fall
    # back to the PlaybackInfo streams only if the metadata fetch came back empty.
    meta_sources = item_meta.get("MediaSources") or []
    full_streams = (
        (meta_sources[0].get("MediaStreams") if meta_sources else None)
        or item_meta.get("MediaStreams")
        or src.get("MediaStreams")
        or []
    )

    subtitles = []
    audio_tracks = []
    default_audio_index = src.get("DefaultAudioStreamIndex")
    if default_audio_index is None and meta_sources:
        default_audio_index = meta_sources[0].get("DefaultAudioStreamIndex")
    for s in full_streams:
        stype = s.get("Type")
        idx = s.get("Index")
        lang = s.get("Language") or ""
        title = s.get("DisplayTitle") or s.get("Title") or lang or f"Track {idx}"
        if stype == "Subtitle":
            if s.get("IsTextSubtitleStream"):
                # Same-origin proxy (GET /subtitle below), NOT a direct link to
                # JF_PUBLIC_URL. Root cause this sidesteps: <track src> on a
                # <video> with no crossorigin attribute fetches in "no-cors"
                # mode, and JF_PUBLIC_URL (watch.<domain>) is a different
                # origin than Hub's own page (example.org apex) — the
                # cross-origin VTT response comes back opaque and the browser
                # silently never renders any cues. Routing the <track> through
                # Hub's own origin removes the cross-origin hop entirely,
                # rather than betting on Jellyfin sending CORS headers (which
                # this can't verify without a live Jellyfin) or bolting
                # crossorigin="anonymous" onto <video> (which would then
                # require the SAME CORS headers just to keep working — no more
                # reliable, just relocates the bet). Bonus: the token no
                # longer leaks into an HTML attribute visible in the DOM/
                # devtools for subtitles specifically — auth is the existing
                # session cookie instead.
                sub_url = (
                    f"/api/playback/subtitle?item_id={urllib.parse.quote(item_id)}"
                    f"&src_id={urllib.parse.quote(src_id)}&idx={idx}"
                )
                subtitles.append({"index": idx, "language": lang, "title": title,
                                   "url": sub_url, "note": None})
            else:
                # PGS/VobSub (image-based) — out of scope for v1 (needs server-side
                # burn-in / Method:Encode). Listed, not offered as a <track>.
                subtitles.append({"index": idx, "language": lang, "title": title,
                                   "url": None, "note": "image subtitle — will re-encode / not shown"})
        elif stype == "Audio":
            codec = (s.get("Codec") or "").upper()
            channels = s.get("Channels")
            audio_tracks.append({
                "index": idx,
                "language": lang,
                "codec": codec,
                "channels": channels,
                "title": title,
                "is_default": bool(s.get("IsDefault")) or idx == default_audio_index,
            })

    # What the browser should treat as "currently playing": the index we just
    # asked Jellyfin for (on a switch), else whatever Jellyfin defaulted to,
    # else just the first audio track listed — never None if any exist, so
    # the frontend always has something to render as selected.
    selected_audio_index = body.audio_stream_index
    if selected_audio_index is None:
        selected_audio_index = default_audio_index
    if selected_audio_index is None and audio_tracks:
        selected_audio_index = audio_tracks[0]["index"]

    run_time_ticks = src.get("RunTimeTicks") or 0  # resume_ticks resolved above, pre-PlaybackInfo

    # DIAGNOSTIC: why do the audio/caption menus not appear for some titles?
    # Logs what Jellyfin actually handed back vs. what we parsed — a raw list
    # of 2+ 'Audio' but parsed audio=1 means a parse bug; only 1 'Audio' in the
    # raw list means Jellyfin filtered the stream list (transcode/device
    # profile) and we need to fetch the full source stream list separately.
    log.warning(
        "playback/info %s mode=%s: parsed audio=%d subs=%d | full raw types=%s | meta_sources=%d",
        (item_id or "")[:8], mode, len(audio_tracks), len(subtitles),
        [s.get("Type") for s in full_streams], len(meta_sources),
    )

    return {
        "ok": True,
        "mode": mode,
        "url": url,
        "play_session_id": play_session_id,
        "item_id": item_id,
        "media_source_id": src_id,
        "run_time_ticks": run_time_ticks,
        "resume_ticks": resume_ticks,
        "subtitles": subtitles,
        "audio_tracks": audio_tracks,
        "selected_audio_index": selected_audio_index,
    }


@router.get("/subtitle")
async def playback_subtitle(request: Request, item_id: str = "", src_id: str = "", idx: int = -1):
    """Same-origin VTT proxy for the player's <track src>. Exists solely to
    keep subtitle loading same-origin — see the long comment at the sub_url
    call site in /info above for why a direct cross-origin link to Jellyfin's
    public host silently renders no cues. Session-gated like every other
    playback route (the browser's native <track> fetch sends the hub_session
    cookie automatically on a same-origin request, no extra wiring needed).
    Streams Jellyfin's own response back as-is; never caches, never a 500 —
    a bad/missing subtitle degrades to an empty response, not a broken page.
    """
    sess = await _require_session(request)
    if sess is None:
        return _unauthorized()
    _uid, token = sess

    item_id = (item_id or "").strip()
    src_id = (src_id or "").strip()
    if not (JF_KEY and item_id and src_id and idx is not None and idx >= 0):
        return Response(status_code=404)

    try:
        raw = await _jf_get_raw_async(
            f"/Videos/{urllib.parse.quote(item_id)}/{urllib.parse.quote(src_id)}"
            f"/Subtitles/{idx}/Stream.vtt",
            token,
        )
    except Exception as e:  # noqa: BLE001 — JF down, bad token, 404 from JF, etc.
        log.warning("playback/subtitle %s/%s idx=%s failed: %s", (item_id or "")[:8], (src_id or "")[:8], idx, e)
        return Response(status_code=502)

    return Response(content=raw, media_type="text/vtt", headers={"Cache-Control": "no-store"})


async def _playing_forward(request: Request, jf_path: str, body: ProgressBody, event_name: str | None = None):
    """Shared body for /start, /progress, /stopped — thin authenticated
    forwarders to Jellyfin's Sessions/Playing* trio. A failed ping here must
    never break playback client-side, so this always degrades to ok:false
    rather than raising."""
    sess = await _require_session(request)
    if sess is None:
        return _unauthorized()
    _uid, token = sess
    if not JF_KEY:
        return {"ok": False}

    payload = {
        "ItemId": body.item_id,
        "PlaySessionId": body.play_session_id,
        "PositionTicks": body.position_ticks,
        "IsPaused": body.is_paused,
        "CanSeek": True,
    }
    if body.media_source_id:
        payload["MediaSourceId"] = body.media_source_id
    if event_name:
        payload["EventName"] = event_name

    # No UserId param here (unlike /info and /episodes) — Jellyfin infers the
    # session/user from the per-user auth header on Sessions/Playing* calls.
    try:
        await _jf_post_async(jf_path, payload, headers=_auth_header(token))
    except Exception:  # noqa: BLE001 — never let a failed progress ping break playback
        return {"ok": False}
    return {"ok": True}


@router.post("/start")
async def playback_start(body: ProgressBody, request: Request):
    return await _playing_forward(request, "/Sessions/Playing", body)


@router.post("/progress")
async def playback_progress(body: ProgressBody, request: Request):
    return await _playing_forward(request, "/Sessions/Playing/Progress", body, event_name="timeupdate")


@router.post("/stopped")
async def playback_stopped(body: ProgressBody, request: Request):
    """The transcode-teardown call — critical. Also the target of the
    pagehide/beforeunload sendBeacon on tab close, which is why this must
    never require anything the beacon can't provide (no custom headers)."""
    return await _playing_forward(request, "/Sessions/Playing/Stopped", body)


@router.get("/episodes")
async def episodes(request: Request, series_id: str = ""):
    """Season/episode listing for the series episode picker. Uses the
    existing admin-key GET helper (like library.py) rather than the
    per-user token — Jellyfin's admin key can read any user's UserData via
    ?UserId=, and this is metadata-only, no play-state mutation — but still
    session-gated so the picker itself is a logged-in-only feature."""
    sess = await _require_session(request)
    if sess is None:
        return _unauthorized()
    uid, _token = sess

    series_id = (series_id or "").strip()
    if not (JF_KEY and series_id):
        return {"ok": False, "seasons": []}

    try:
        data = await _jf_get_async(
            f"/Shows/{urllib.parse.quote(series_id)}/Episodes",
            UserId=uid, Fields="Overview,RunTimeTicks,UserData", EnableImages="true",
        )
    except Exception:  # noqa: BLE001
        return {"ok": False, "seasons": []}

    by_season: dict = {}
    for ep in (data.get("Items") or []):
        season_no = ep.get("ParentIndexNumber")
        by_season.setdefault(season_no, []).append(ep)

    seasons = []
    for season_no in sorted(by_season.keys(), key=lambda x: (x is None, x)):
        eps = sorted(by_season[season_no], key=lambda e: (e.get("IndexNumber") is None, e.get("IndexNumber")))
        out_eps = []
        for ep in eps:
            ticks = ep.get("RunTimeTicks") or 0
            user_data = ep.get("UserData") or {}
            tag = (ep.get("ImageTags") or {}).get("Primary")
            out_eps.append({
                "id": ep.get("Id"),
                "title": ep.get("Name"),
                "episode_no": ep.get("IndexNumber"),
                "overview": (ep.get("Overview") or "").strip(),
                "runtime_min": round(ticks / 600000000) if ticks else None,
                "run_time_ticks": ticks,
                "resume_ticks": user_data.get("PlaybackPositionTicks") or 0,
                "tag": tag,
            })
        seasons.append({"season": season_no, "episodes": out_eps})

    return {"ok": True, "seasons": seasons}


_RESUME_FIELDS = (
    "Overview,Genres,ProductionYear,CommunityRating,CriticRating,OfficialRating,"
    "ProviderIds,People,RunTimeTicks,SeriesPrimaryImageTag"
)


def _resume_card(it: dict) -> dict:
    """_item_to_card, plus the resume-position/runtime the progress bar needs
    and, for episodes, the series context (name/season/episode + falling back
    to the series' own poster art when the episode has none of its own)."""
    card = _item_to_card(it)
    user_data = it.get("UserData") or {}
    card["position_ticks"] = user_data.get("PlaybackPositionTicks") or 0
    card["runtime_ticks"] = it.get("RunTimeTicks") or 0
    card["poster_id"] = card["id"]
    card["poster_tag"] = card["tag"]
    if it.get("Type") == "Episode":
        card["series"] = it.get("SeriesName")
        card["season"] = it.get("ParentIndexNumber")
        card["episode"] = it.get("IndexNumber")
        if not card["poster_tag"] and it.get("SeriesPrimaryImageTag"):
            card["poster_id"] = it.get("SeriesId")
            card["poster_tag"] = it.get("SeriesPrimaryImageTag")
    return card


@router.get("/resume")
async def resume(request: Request):
    """Per-user "Continue Watching" — in-progress movies/episodes from
    Jellyfin's own Resume list. Session-gated: 401 with no session, graceful
    {ok:false, items:[]} if Jellyfin itself is unreachable."""
    sess = await _require_session(request)
    if sess is None:
        return _unauthorized()
    uid, _token = sess

    if not JF_KEY:
        return {"ok": False, "items": []}

    try:
        data = await _jf_get_async(
            f"/Users/{uid}/Items/Resume",
            Limit=20, MediaTypes="Video", Recursive="true",
            Fields=_RESUME_FIELDS, ImageTypeLimit=1, EnableImageTypes="Primary",
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "items": []}

    items = [_resume_card(it) for it in (data.get("Items") or [])]
    return {"ok": True, "items": items}
