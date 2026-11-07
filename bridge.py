#!/usr/bin/env python3
"""
Pit Wall live bridge.

Connects to the free streams of the F1 live timing feed, estimates where each
car is on track from mini-sector timing, and serves the site at
http://localhost:8765.

    pip install -r requirements.txt
    python bridge.py

Circuit maps come from data/tracks (made by build_data.py) or, failing that,
from a past session via FastF1.

This is an unofficial, personal-use tool. It is not affiliated with Formula 1.
"""
from __future__ import annotations

import argparse
import asyncio
import bisect
import html
import json
import math
import os
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

try:
    import aiohttp
    from aiohttp import web
except ImportError:
    sys.exit("Missing packages. Run:  pip install -r requirements.txt")

HERE = Path(__file__).resolve().parent
SIGNALR = "https://livetiming.formula1.com/signalr"
HUB = json.dumps([{"name": "Streaming"}])
# Free topics only. Position.z and CarData.z need an F1 TV login.
TOPICS = [
    "Heartbeat", "SessionInfo", "SessionStatus", "TrackStatus", "LapCount",
    "DriverList", "TimingData", "TimingAppData", "RaceControlMessages",
    "WeatherData", "ExtrapolatedClock",
]
COMPOUND = {"SOFT": "S", "MEDIUM": "M", "HARD": "H", "INTERMEDIATE": "I", "WET": "W"}
TRACK_STATUS = {"1": "GREEN", "2": "YELLOW", "4": "SC", "5": "RED", "6": "VSC", "7": "VSC"}


def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- helpers
def merge(dst, src):
    """Deep-merge a live timing update into the stored state.

    Updates to lists arrive as dicts keyed by index ("0", "1", ...)."""
    if isinstance(src, dict):
        if isinstance(dst, list):
            for k, v in src.items():
                try:
                    i = int(k)
                except (TypeError, ValueError):
                    continue
                while len(dst) <= i:
                    dst.append({})
                dst[i] = merge(dst[i], v) if isinstance(v, dict) else v
            return dst
        if not isinstance(dst, dict):
            dst = {}
        for k, v in src.items():
            if k == "_kf":
                continue
            if isinstance(v, dict):
                dst[k] = merge(dst.get(k), v)
            else:
                dst[k] = v
        return dst
    return src


def items(x):
    """(index, value) pairs from a list or an index-keyed dict."""
    if isinstance(x, list):
        return list(enumerate(x))
    if isinstance(x, dict):
        out = []
        for k, v in x.items():
            try:
                out.append((int(k), v))
            except (TypeError, ValueError):
                pass
        return sorted(out)
    return []


def lap_seconds(s):
    if not isinstance(s, str) or not s.strip():
        return None
    try:
        if ":" in s:
            m, r = s.split(":", 1)
            return int(m) * 60 + float(r)
        return float(s)
    except ValueError:
        return None


def num(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def fmt_gap(s):
    if not isinstance(s, str) or not s:
        return "–"
    s = s.strip()
    if s.upper().startswith("LAP"):
        return "Leader"
    if s.endswith("L") and s[:-1].lstrip("+").isdigit():
        n = int(s[:-1].lstrip("+"))
        return f"+{n} lap" + ("s" if n > 1 else "")
    return s if s.startswith("+") or s.startswith("-") else "+" + s


# ---------------------------------------------------------------- track
class Track:
    def __init__(self, name, x, y, d, t, lap_time, s1, s2):
        self.name, self.x, self.y, self.d, self.t = name, x, y, d, t
        self.L = d[-1]
        self.lap_time = lap_time
        self.sec_t = [0.0, s1, s2, lap_time]  # reference times at sector starts/end

    def dist_at_time(self, tt):
        tt = max(0.0, min(tt, self.t[-1]))
        i = bisect.bisect_right(self.t, tt) - 1
        i = max(0, min(i, len(self.t) - 2))
        span = (self.t[i + 1] - self.t[i]) or 1e-9
        return self.d[i] + (self.d[i + 1] - self.d[i]) * (tt - self.t[i]) / span

    def sector_point(self, si, frac):
        a, b = self.sec_t[si], self.sec_t[si + 1]
        return self.dist_at_time(a + (b - a) * frac) % self.L

    def to_json(self):
        return {"name": self.name, "x": self.x, "y": self.y, "d": self.d, "t": self.t,
                "lap_time": self.lap_time, "s1": self.sec_t[1], "s2": self.sec_t[2]}

    @classmethod
    def from_json(cls, j):
        return cls(j["name"], j["x"], j["y"], j["d"], j["t"], j["lap_time"], j["s1"], j["s2"])

    def message(self):
        r = lambda v: [round(z, 1) for z in v]
        return {"type": "track", "name": self.name, "x": r(self.x), "y": r(self.y),
                "d": r(self.d), "t": [round(z, 3) for z in self.t], "lapTime": self.lap_time,
                "sectors": [round(self.dist_at_time(self.sec_t[1]), 1), round(self.dist_at_time(self.sec_t[2]), 1)]}


def track_slug(name):
    import re
    import unicodedata
    plain = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-")


def saved_track(event):
    """A circuit layout saved by make_tracks.py, matched by event name."""
    folder = next((f for f in (HERE / "data" / "tracks", HERE / "tracks") if f.is_dir()), None)
    if folder is None:
        return None
    want = track_slug(event)
    path = folder / f"{want}.json"
    if not path.exists():
        words = set(want.split("-")) - {"grand", "prix", "gp", "formula", "1"}
        path = next((p for p in sorted(folder.glob("*.json"))
                     if words & (set(p.stem.split("-")) - {"grand", "prix"})), None)
    if path is None:
        return None
    return Track.from_json(json.loads(path.read_text(encoding="utf-8")))


def load_track(event, year):
    """Blocking: the circuit outline, from tracks/ if saved there, otherwise built
    with FastF1 from a past session's fastest lap."""
    saved = saved_track(event)
    if saved:
        log(f"Circuit layout ready: {saved.name} (saved)")
        return saved
    if os.environ.get("PITWALL_TRACKS_ONLY"):
        raise RuntimeError(f"no saved layout for {event} in tracks/ (run build_data.py)")
    return build_track(event, year)


def track_from_session(ses, event=None):
    """Circuit outline from the fastest lap of an already loaded FastF1 session
    (loaded with laps=True, telemetry=True)."""
    import numpy as np

    lap = ses.laps.pick_fastest()
    if lap is None or getattr(lap, "empty", False):
        raise RuntimeError("no timed lap in this session")
    tel = lap.get_telemetry()
    t = tel["Time"].dt.total_seconds().to_numpy(dtype=float)
    d = tel["Distance"].to_numpy(dtype=float)
    x = tel["X"].to_numpy(dtype=float)
    yy = tel["Y"].to_numpy(dtype=float)
    ok = np.isfinite(t) & np.isfinite(d) & np.isfinite(x) & np.isfinite(yy)
    t, d, x, yy = t[ok], d[ok], x[ok], yy[ok]
    d = d - d[0]
    t = t - t[0]
    keep = np.concatenate([[True], np.diff(d) > 0.05])
    t, d, x, yy = t[keep], d[keep], x[keep], yy[keep]
    if len(d) < 50:
        raise RuntimeError("too little position data on the fastest lap")
    try:
        rot = math.radians(float(ses.get_circuit_info().rotation))
    except Exception:
        rot = 0.0
    xr = x * math.cos(rot) - yy * math.sin(rot)
    yr = x * math.sin(rot) + yy * math.cos(rot)
    idx = np.unique(np.linspace(0, len(d) - 1, 900).astype(int))
    secs = lambda v, alt: v.total_seconds() if v == v and v is not None else alt
    lap_time = secs(lap["LapTime"], float(t[-1]))
    s1 = secs(lap["Sector1Time"], lap_time / 3)
    s2v = secs(lap["Sector2Time"], lap_time / 3)
    name = str(ses.event.get("EventName", event or ""))
    track = Track(name, xr[idx].tolist(), yr[idx].tolist(), d[idx].tolist(), t[idx].tolist(),
                  float(lap_time), float(s1), float(s1 + s2v))
    track.rot = rot
    return track


def enable_fastf1_cache():
    import fastf1
    cache = HERE / "fastf1-cache"
    cache.mkdir(exist_ok=True)
    fastf1.Cache.enable_cache(str(cache))
    return fastf1


def build_track(event, year):
    fastf1 = enable_fastf1_cache()
    last_err = None
    for y, kind in [(year, "Q"), (year, "R"), (year - 1, "R"), (year - 1, "Q"), (year - 2, "R")]:
        try:
            log(f"Loading circuit layout from {y} {event} ({kind})")
            ses = fastf1.get_session(y, event, kind)
            ses.load(laps=True, telemetry=True, weather=False, messages=False)
            track = track_from_session(ses, event)
            log(f"Circuit layout ready: {track.name}, {track.L:.0f} m")
            return track
        except Exception as e:  # try the next session
            last_err = e
            log(f"  not available: {e}")
    raise RuntimeError(f"No usable layout for {event}: {last_err}")


def load_schedule():
    """Blocking: upcoming race weekends with session start times in UTC."""
    import pandas as pd

    fastf1 = enable_fastf1_cache()
    now = datetime.now(timezone.utc)
    events = []
    for yr in (now.year, now.year + 1):
        try:
            sch = fastf1.get_event_schedule(yr, include_testing=False)
        except Exception as e:
            log(f"Schedule for {yr} not available: {e}")
            continue
        for _, ev in sch.iterrows():
            sessions = []
            for i in range(1, 6):
                name, when = ev.get(f"Session{i}"), ev.get(f"Session{i}DateUtc")
                if not name or when is None or pd.isna(when):
                    continue
                ts = pd.Timestamp(when)
                ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
                sessions.append({"name": str(name), "start": ts.strftime("%Y-%m-%dT%H:%M:%SZ")})
            if sessions and pd.Timestamp(sessions[-1]["start"]) > pd.Timestamp(now) - pd.Timedelta(hours=3):
                events.append({"name": str(ev.get("EventName", "")),
                               "place": f"{ev.get('Location', '')}, {ev.get('Country', '')}".strip(", "),
                               "sessions": sessions})
    return events


# ---------------------------------------------------------------- state
class LiveState:
    def __init__(self, clock):
        self.clock = clock
        self.data = {}
        self.track = None
        self.track_key = None
        self.track_loading = False
        self.info = None
        self.anchors = {}   # racing number -> {"si", "frac", "t"}
        self.lap_hist = {}  # racing number -> [{"t", "c", "pit"}]

    # -- incoming feed
    def apply(self, topic, payload):
        if not isinstance(topic, str) or topic.endswith(".z") or payload is None:
            return
        if topic == "TimingData" and isinstance(payload, dict):
            self._timing_update(payload)
        self.data[topic] = merge(self.data.get(topic), payload)

    def _seg_count(self, num, si):
        line = ((self.data.get("TimingData") or {}).get("Lines") or {}).get(num) or {}
        for i, sec in items(line.get("Sectors")):
            if i == si and isinstance(sec, dict):
                n = len(items(sec.get("Segments")))
                if n:
                    return n
        return 8

    def _timing_update(self, payload):
        now = self.clock()
        lines = payload.get("Lines")
        if not isinstance(lines, dict):
            return
        for rn, line in lines.items():
            if not isinstance(line, dict):
                continue
            cand = None
            if "NumberOfLaps" in line:
                cand = (0, 0.0)
            for si, sec in items(line.get("Sectors")):
                if not isinstance(sec, dict) or si > 2:
                    continue
                segs = items(sec.get("Segments"))
                n = max(self._seg_count(rn, si), len(segs), 1)
                for k, seg in segs:
                    if isinstance(seg, dict) and seg.get("Status"):
                        p = (si, min(1.0, (k + 1) / n))
                        if cand is None or p > cand:
                            cand = p
                if si < 2 and sec.get("Value"):
                    p = (si + 1, 0.0)
                    if cand is None or p > cand:
                        cand = p
            if cand is not None:
                prev = self.anchors.get(rn)
                newp, oldp = cand[0] + cand[1], (prev["si"] + prev["frac"]) if prev else -1
                new_lap = oldp > 2.3 and newp < 0.8
                if prev is None or newp >= oldp or new_lap or now - prev["t"] > 120:
                    self.anchors[rn] = {"si": cand[0], "frac": cand[1], "t": now}
            last = line.get("LastLapTime")
            if isinstance(last, dict) and last.get("Value"):
                secs = lap_seconds(last["Value"])
                hist = self.lap_hist.setdefault(rn, [])
                if secs and (not hist or abs(hist[-1]["t"] - secs) > 1e-6):
                    hist.append({"t": secs, "c": self._compound(rn), "pit": False})
                    del hist[:-120]
            if line.get("PitOut") or line.get("InPit"):
                hist = self.lap_hist.get(rn)
                if hist:
                    hist[-1]["pit"] = True

    def _compound(self, num):
        stints = items((((self.data.get("TimingAppData") or {}).get("Lines") or {}).get(num) or {}).get("Stints"))
        if not stints:
            return "U"
        return COMPOUND.get(str(stints[-1][1].get("Compound", "")).upper(), "U")

    # -- circuit layout
    def session_key(self):
        si = self.data.get("SessionInfo") or {}
        meeting = si.get("Meeting") or {}
        name = meeting.get("Name") or meeting.get("Location")
        start = si.get("StartDate") or ""
        year = int(start[:4]) if start[:4].isdigit() else datetime.now().year
        return (name, year) if name else None

    # -- outgoing state
    def snapshot(self, rate):
        D = self.data
        dl = D.get("DriverList") or {}
        td = (D.get("TimingData") or {}).get("Lines") or {}
        ta = (D.get("TimingAppData") or {}).get("Lines") or {}
        si = D.get("SessionInfo") or {}
        status = TRACK_STATUS.get(str((D.get("TrackStatus") or {}).get("Status", "1")), "GREEN")
        sstat = str((D.get("SessionStatus") or {}).get("Status", ""))
        if sstat in ("Finished", "Finalised", "Ends"):
            status = "FINISHED"
        elif sstat in ("Inactive", "") and not td:
            status = "WAIT"
        T = self.track
        now = self.clock()
        drivers, best_times = [], []
        for rn, info in dl.items():
            if not isinstance(info, dict) or not info.get("Tla"):
                continue
            line = td.get(rn) or {}
            pos = int(num_or(line.get("Position"), num_or(info.get("Line"), 99)))
            gap = line.get("GapToLeader") or line.get("TimeDiffToFastest") or ""
            itv = line.get("IntervalToPositionAhead")
            itv = itv.get("Value") if isinstance(itv, dict) else (line.get("TimeDiffToPositionAhead") or "")
            last = line.get("LastLapTime") or {}
            best = line.get("BestLapTime") or {}
            sectors = []
            for i in range(3):
                sec = dict(items(line.get("Sectors"))).get(i) or {}
                f = "ob" if sec.get("OverallFastest") else "pb" if sec.get("PersonalFastest") else ("nn" if sec.get("Value") else "")
                sectors.append({"v": num(sec.get("Value")), "f": f})
            stints_raw = [s for _, s in items((ta.get(rn) or {}).get("Stints")) if isinstance(s, dict)]
            stints = [{"c": COMPOUND.get(str(s.get("Compound", "")).upper(), "U"),
                       "laps": max(0, int(num(s.get("TotalLaps"), 0)) - int(num(s.get("StartLaps"), 0)))} for s in stints_raw]
            cur = stints_raw[-1] if stints_raw else {}
            best_s = lap_seconds(best.get("Value"))
            if best_s:
                best_times.append(best_s)
            d = {
                "num": str(info.get("RacingNumber") or rn), "code": info.get("Tla"),
                "name": info.get("FullName") or info.get("BroadcastName") or info.get("Tla"),
                "team": info.get("TeamName") or "", "color": "#" + str(info.get("TeamColour") or "8EA2C1").lstrip("#"),
                "pos": pos, "grid": None,
                "gapStr": "Leader" if pos == 1 else fmt_gap(gap),
                "intStr": "Leader" if pos == 1 else fmt_gap(itv),
                "last": lap_seconds(last.get("Value")), "best": best_s,
                "lastFlag": "ob" if last.get("OverallFastest") else "pb" if last.get("PersonalFastest") else "",
                "sectors": sectors, "pits": int(num(line.get("NumberOfPitStops"), 0)),
                "inPit": bool(line.get("InPit")), "retired": bool(line.get("Retired") or line.get("Stopped")),
                "compound": COMPOUND.get(str(cur.get("Compound", "")).upper(), "U") if cur else None,
                "age": int(num(cur.get("TotalLaps"), 0)) if cur else None,
                "stints": stints, "drs": False,
                "trap": num(((line.get("Speeds") or {}).get("ST") or {}).get("Value")),
                "lapTimes": self.lap_hist.get(rn, []),
            }
            a = self.anchors.get(rn)
            if T and a:
                pace = (d["last"] / T.lap_time) if d["last"] else 1.08
                pace = min(1.6, max(0.97, pace))
                if status in ("SC", "VSC", "YELLOW"):
                    pace = max(pace, 1.4 if status != "YELLOW" else pace)
                segs = self._seg_count(rn, min(a["si"], 2))
                d["anchorD"] = round(T.sector_point(min(a["si"], 2), a["frac"]) if a["si"] < 3 else 0.0, 1)
                d["anchorT"] = round(a["t"] * 1000)
                d["pace"] = round(pace, 4)
                d["maxAhead"] = round(T.L / (3 * segs) * 2.2, 1)
            drivers.append(d)
        drivers.sort(key=lambda z: z["pos"])
        if best_times:
            ob = min(best_times)
            for d in drivers:
                d["bestIsOb"] = d["best"] is not None and abs(d["best"] - ob) < 1e-6
        rc = []
        for _, m in items((D.get("RaceControlMessages") or {}).get("Messages"))[-40:][::-1]:
            if not isinstance(m, dict):
                continue
            cat = str(m.get("Category", ""))
            rc.append({"lap": m.get("Lap"), "text": html.escape(str(m.get("Message", ""))),
                       "kind": "flag" if cat in ("Flag", "SafetyCar") else "info"})
        w = D.get("WeatherData") or {}
        weather = {"air": num(w.get("AirTemp"), 0), "track": num(w.get("TrackTemp"), 0), "hum": num(w.get("Humidity"), 0),
                   "wind": num(w.get("WindSpeed"), 0), "rain": str(w.get("Rainfall", "0")) not in ("0", "", "False")} if w else None
        lc = D.get("LapCount") or {}
        meeting = si.get("Meeting") or {}
        is_race = str(si.get("Type", "")).lower() == "race"
        clock = None if is_race else (D.get("ExtrapolatedClock") or {}).get("Remaining")
        return {"type": "state", "serverTime": round(now * 1000), "rate": rate, "sessionState": sstat,
                "event": meeting.get("Name") or "Live session", "session": si.get("Name") or "",
                "lapCur": int(num(lc.get("CurrentLap"), 0)), "lapTotal": int(num(lc.get("TotalLaps"), 0)),
                "status": status, "clock": clock, "drivers": drivers, "rc": rc, "weather": weather}


def num_or(x, default):
    v = num(x)
    return default if v is None else v


# ---------------------------------------------------------------- feed sources
async def live_feed(state, recorder):
    """Keep a connection to F1 live timing open, trying the newer SignalR Core
    endpoint first and the classic one as a fallback."""
    headers = {"User-Agent": "BestHTTP", "Accept-Encoding": "gzip, identity"}
    attempt = 0
    while True:
        method = (_core_session, _legacy_session)[attempt % 2]
        try:
            async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=None, connect=20)) as http:
                await method(http, state, recorder)
            log("Live timing connection closed, reconnecting in 5 s")
            attempt = 0 if method is _core_session else 1
        except Exception as e:
            log(f"Live timing ({method.__name__.strip('_').replace('_session', '')}) failed: {type(e).__name__}: {e}")
            attempt += 1
        await asyncio.sleep(5)


async def _core_session(http, state, recorder):
    core = "https://livetiming.formula1.com/signalrcore"
    async with http.post(f"{core}/negotiate?negotiateVersion=1") as r:
        if r.status >= 400:
            raise RuntimeError(f"negotiate returned HTTP {r.status}: {(await r.text())[:200]}")
        neg = await r.json(content_type=None)
    token = neg.get("connectionToken") or neg.get("connectionId")
    url = core.replace("https://", "wss://") + "?id=" + urllib.parse.quote(token)
    async with http.ws_connect(url, max_msg_size=0) as ws:
        await ws.send_str(json.dumps({"protocol": "json", "version": 1}) + "\x1e")
        await ws.send_str(json.dumps({"type": 1, "invocationId": "0", "target": "Subscribe", "arguments": [TOPICS]}) + "\x1e")
        log("Connected to F1 live timing")

        async def keepalive():
            while True:
                await asyncio.sleep(15)
                await ws.send_str('{"type":6}\x1e')
        ka = asyncio.create_task(keepalive())
        try:
            async for msg in ws:
                if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                for part in msg.data.split("\x1e"):
                    if not part.strip():
                        continue
                    try:
                        j = json.loads(part)
                    except ValueError:
                        continue
                    t = j.get("type")
                    if t == 1 and j.get("target") == "feed" and len(j.get("arguments") or []) >= 2:
                        apply_feed(state, j["arguments"][0], j["arguments"][1], recorder)
                    elif t == 3 and isinstance(j.get("result"), dict):
                        apply_snapshot(state, j["result"], recorder)
                    elif t == 3 and j.get("error"):
                        raise RuntimeError(f"subscribe refused: {j['error']}")
                    elif t == 7:
                        raise RuntimeError(f"server closed the connection: {j.get('error') or 'no reason given'}")
        finally:
            ka.cancel()


async def _legacy_session(http, state, recorder):
    q = urllib.parse.urlencode({"connectionData": HUB, "clientProtocol": "1.5"})
    async with http.get(f"{SIGNALR}/negotiate?{q}") as r:
        if r.status >= 400:
            raise RuntimeError(f"negotiate returned HTTP {r.status}: {(await r.text())[:200]}")
        token = (await r.json(content_type=None))["ConnectionToken"]
    q = urllib.parse.urlencode({"transport": "webSockets", "connectionToken": token,
                                "connectionData": HUB, "clientProtocol": "1.5"})
    url = SIGNALR.replace("https://", "wss://") + f"/connect?{q}"
    async with http.ws_connect(url, max_msg_size=0) as ws:
        await ws.send_str(json.dumps({"H": "Streaming", "M": "Subscribe", "A": [TOPICS], "I": 1}))
        log("Connected to F1 live timing (classic endpoint)")
        async for msg in ws:
            if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break
            if msg.type == aiohttp.WSMsgType.TEXT:
                handle_raw(state, msg.data, recorder)


def apply_snapshot(state, snap, recorder):
    for topic, data in sorted(snap.items(), key=lambda kv: kv[0] == "TimingData"):
        state.apply(topic, data)
    if recorder:
        recorder.write(json.dumps({"t": time.time(), "R": snap}) + "\n")
        recorder.flush()


def apply_feed(state, topic, data, recorder):
    state.apply(topic, data)
    if recorder:
        recorder.write(json.dumps({"t": time.time(), "topic": topic, "data": data}) + "\n")
        recorder.flush()


def handle_raw(state, text, recorder):
    try:
        j = json.loads(text)
    except ValueError:
        return
    if isinstance(j.get("R"), dict):
        apply_snapshot(state, j["R"], recorder)
    for m in j.get("M") or []:
        if isinstance(m, dict) and m.get("M") == "feed" and len(m.get("A") or []) >= 2:
            apply_feed(state, m["A"][0], m["A"][1], recorder)


async def replay_feed(state, path, speed, clock_ref):
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    rows = []
    for ln in lines:
        try:
            rows.append(json.loads(ln))
        except ValueError:
            pass
    if not rows:
        log("Replay file is empty")
        return
    t0, start = rows[0]["t"], time.time()
    clock_ref["fn"] = lambda: t0 + (time.time() - start) * speed
    log(f"Replaying {len(rows)} messages at {speed}x")
    for row in rows:
        wait = (row["t"] - t0) / speed - (time.time() - start)
        if wait > 0:
            await asyncio.sleep(wait)
        if "R" in row:
            for topic, data in row["R"].items():
                state.apply(topic, data)
        else:
            state.apply(row.get("topic"), row.get("data"))
    state.info = "The replay has finished."
    log("Replay finished")


# ---------------------------------------------------------------- server
def main():
    ap = argparse.ArgumentParser(description="Pit Wall live bridge")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8765)))
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--replay", help="replay a recorded .jsonl session instead of going live")
    ap.add_argument("--speed", type=float, default=1.0, help="replay speed multiplier")
    ap.add_argument("--no-record", action="store_true", help="don't record the live session")
    ap.add_argument("--event", help="circuit to draw, e.g. 'Brazil' (normally detected automatically)")
    ap.add_argument("--year", type=int, help="season for --event")
    args = ap.parse_args()

    clock_ref = {"fn": time.time}
    state = LiveState(lambda: clock_ref["fn"]())
    clients = set()
    rate = args.speed if args.replay else 1.0

    async def broadcast(obj):
        data = json.dumps(obj, separators=(",", ":"))
        for ws in list(clients):
            try:
                await ws.send_str(data)
            except Exception:
                clients.discard(ws)

    async def load_track_task(event, year):
        state.track_loading = True
        state.info = f"Loading the {event} circuit layout. The first load can take a minute."
        try:
            state.track = await asyncio.to_thread(load_track, event, year)
            state.info = None
            await broadcast(state.track.message())
        except ImportError:
            state.info = "Install FastF1 to draw the circuit map:  pip install fastf1"
            log(state.info)
        except Exception as e:
            state.info = f"Couldn't load the circuit layout ({e}). Timing still works. Try --event and --year."
            log(state.info)
        finally:
            state.track_loading = False

    schedule = {"msg": None}

    async def schedule_task():
        while True:
            try:
                events = await asyncio.to_thread(load_schedule)
                if events:
                    schedule["msg"] = {"type": "schedule", "events": events}
                    await broadcast(schedule["msg"])
                    log(f"Schedule loaded: next is {events[0]['name']}")
            except ImportError:
                log("Install FastF1 for an always up-to-date schedule:  pip install fastf1")
                return
            except Exception as e:
                log(f"Couldn't load the schedule ({e})")
            await asyncio.sleep(6 * 3600)

    async def ticker(app):
        if args.event:
            state.track_key = (args.event, args.year or datetime.now().year)
            asyncio.create_task(load_track_task(*state.track_key))
        interval = float(os.environ.get("PITWALL_INTERVAL", "1.0"))
        while True:
            await asyncio.sleep(interval)
            key = state.session_key()
            if key and key != state.track_key and not state.track_loading and not args.event:
                state.track_key = key
                state.track = None
                asyncio.create_task(load_track_task(*key))
            if not clients:
                continue
            if state.data.get("DriverList"):
                await broadcast(state.snapshot(rate))
                if state.info:
                    await broadcast({"type": "info", "text": state.info})
            else:
                await broadcast({"type": "info", "text": state.info or
                                 "Connected to F1 live timing. No session is live right now. "
                                 "Timing appears here as soon as one starts."})
                await broadcast({**state.snapshot(rate), "drivers": []})

    async def on_startup(app):
        if args.replay:
            app["feed"] = asyncio.create_task(replay_feed(state, args.replay, args.speed, clock_ref))
        else:
            rec = None
            if not args.no_record:
                (HERE / "recordings").mkdir(exist_ok=True)
                path = HERE / "recordings" / f"live-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.jsonl"
                rec = open(path, "a", encoding="utf-8")
                log(f"Recording to {path}")
            app["feed"] = asyncio.create_task(live_feed(state, rec))
        app["ticker"] = asyncio.create_task(ticker(app))
        app["schedule"] = asyncio.create_task(schedule_task())

    async def index(request):
        return web.FileResponse(HERE / "index.html")

    async def healthz(request):
        # Tiny endpoint for a free uptime pinger, so the server never sleeps.
        return web.Response(text="ok")

    async def ws_handler(request):
        ws = web.WebSocketResponse(heartbeat=20, compress=True)
        await ws.prepare(request)
        clients.add(ws)
        if schedule["msg"]:
            await ws.send_str(json.dumps(schedule["msg"]))
        if state.track:
            await ws.send_str(json.dumps(state.track.message()))
        try:
            async for _ in ws:
                pass
        finally:
            clients.discard(ws)
        return ws

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/healthz", healthz)
    if (HERE / "data").is_dir():
        app.router.add_static("/data", HERE / "data")
    app.on_startup.append(on_startup)
    print(f"\n  Pit Wall bridge running. Open http://localhost:{args.port}\n")
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
