#!/usr/bin/env python3
"""
Builds the data/ folder the site reads:

    data/schedule.json     upcoming race weekends, session times in UTC
    data/tracks/*.json     circuit layouts for the rest of the season
    data/replay.json       the most recent race, for the replay

    pip install -r requirements.txt
    python build_data.py

The GitHub workflow runs this automatically before every deploy.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import bridge

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
TRACKS = DATA / "tracks"
COMPOUND = {"SOFT": "S", "MEDIUM": "M", "HARD": "H", "INTERMEDIATE": "I", "WET": "W"}
HZ = 2  # replay samples per second


def log(*a):
    print(*a, flush=True)


def utc(ts):
    ts = pd.Timestamp(ts)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def race_time(ev):
    for i in range(5, 0, -1):
        if str(ev.get(f"Session{i}", "")).lower() == "race":
            v = ev.get(f"Session{i}DateUtc")
            return None if v is None or pd.isna(v) else utc(v)
    return None


# ---------------------------------------------------------------- schedule
def build_schedule():
    events = bridge.load_schedule()
    (DATA / "schedule.json").write_text(json.dumps({"events": events}, ensure_ascii=False), encoding="utf-8")
    log(f"Schedule: {len(events)} upcoming events")
    return events


# ---------------------------------------------------------------- tracks
def build_tracks(events):
    TRACKS.mkdir(parents=True, exist_ok=True)
    for ev in events[:6]:
        name = ev["name"]
        path = TRACKS / f"{bridge.track_slug(name)}.json"
        if path.exists():
            continue
        year = int(ev["sessions"][0]["start"][:4])
        try:
            track = bridge.build_track(name, year)
            path.write_text(json.dumps(track.to_json()), encoding="utf-8")
            log(f"Track saved: {path.name}")
        except Exception as e:
            log(f"Track skipped for {name}: {e}")


# ---------------------------------------------------------------- replay
def project(xs, ys, tx, ty, td):
    """Distance along the track of each point (nearest track sample)."""
    out = np.empty(len(xs))
    pts = np.stack([tx, ty], axis=1)
    for i in range(0, len(xs), 2000):
        chunk = np.stack([xs[i:i + 2000], ys[i:i + 2000]], axis=1)
        d2 = ((chunk[:, None, :] - pts[None, :, :]) ** 2).sum(axis=2)
        out[i:i + 2000] = td[d2.argmin(axis=1)]
    return out


def unwrap(t, d, L):
    """Turn distance-around-the-lap into total distance travelled. The first
    sample is on the grid, behind the line, so it counts as lap -1."""
    prog = np.empty(len(d))
    prog[0] = d[0] - (L if d[0] > 0.5 * L else 0.0)
    for i in range(1, len(d)):
        step = d[i] - d[i - 1]
        if step < -0.5 * L:
            step += L
        elif step > 0.5 * L:
            step -= L
        limit = 105.0 * max(t[i] - t[i - 1], 0.05) + 40.0
        if step > limit or step < -30.0:  # projection jumped to another part of the track
            step = 0.0
        prog[i] = prog[i - 1] + step
    return prog


def find_start(raw, guess):
    """Lights out: the moment the field pulls away after the last time it sat
    still on the grid, searched for around FastF1's lap 1 start time."""
    tg = np.arange(guess - 600.0, guess + 90.0, 0.5)
    speeds = []
    for st, x, y in raw.values():
        if st[0] > tg[-1] or st[-1] < tg[0]:
            continue
        xi, yi = np.interp(tg, st, x), np.interp(tg, st, y)
        speeds.append(np.hypot(np.gradient(xi), np.gradient(yi)) / 10.0 / 0.5)  # X/Y are in 0.1 m
    if len(speeds) < 5:
        return guess
    med = np.median(np.array(speeds), axis=0)
    still = med < 1.5
    best = None
    run = 0
    for i, s_ in enumerate(still):
        run = run + 1 if s_ else 0
        if run >= 8 and i + 1 < len(still) and not still[i + 1] and tg[i] < guess + 60:
            best = tg[i]
    return float(best) if best is not None else guess


def secs(v, t0):
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return round(v.total_seconds() - t0, 3)


def plain(v, kind=float):
    try:
        if v is None or pd.isna(v):
            return None
        return kind(v)
    except (TypeError, ValueError):
        return None


def replay_from_session(ses, total_laps=None):
    track = bridge.track_from_session(ses)
    L = track.L
    tx, ty, td = np.array(track.x), np.array(track.y), np.array(track.d)
    rot = getattr(track, "rot", 0.0)
    laps = ses.laps
    guess = laps.loc[laps["LapNumber"] == 1, "LapStartTime"].dropna().min().total_seconds()
    raw = {}
    for drv in ses.drivers:
        pos = ses.pos_data.get(drv) if hasattr(ses.pos_data, "get") else None
        if pos is None or len(pos) == 0:
            continue
        p = pos[pos["Status"].astype(str) == "OnTrack"] if "Status" in pos else pos
        st = p["SessionTime"].dt.total_seconds().to_numpy(float)
        x = p["X"].to_numpy(float)
        y = p["Y"].to_numpy(float)
        ok = np.isfinite(st) & np.isfinite(x) & np.isfinite(y) & ~((x == 0) & (y == 0))
        if ok.sum() >= 20:
            raw[drv] = (st[ok], x[ok], y[ok])
    t0 = find_start(raw, guess) - 6.0
    t_end = laps["Time"].dropna().max().total_seconds() + 25.0
    grid_t = np.arange(t0, t_end, 1.0 / HZ)
    total = int(total_laps or laps["LapNumber"].max())

    results = ses.results
    drivers, frames = [], []
    for drv, (st, x, y) in raw.items():
        keep = st >= t0 - 4
        st, x, y = st[keep], x[keep], y[keep]
        if len(st) < 20:
            continue
        xr = x * math.cos(rot) - y * math.sin(rot)
        yr = x * math.sin(rot) + y * math.cos(rot)
        prog = unwrap(st, project(xr, yr, tx, ty, td), L)
        g = np.interp(grid_t, st, prog)
        moving = np.nonzero(np.abs(np.diff(g)) > 0.6)[0]
        last_move = grid_t[moving[-1]] - t0 if len(moving) else 0.0

        r = results.loc[results["DriverNumber"].astype(str) == str(drv)]
        r = r.iloc[0] if len(r) else {}
        status = str(r.get("Status", "")) if len(r) else ""
        finished = status == "Finished" or status.startswith("+") or "Lap" in status
        color = str(r.get("TeamColor", "") or "888888") if len(r) else "888888"
        grid = plain(r.get("GridPosition"), int) if len(r) else None

        rows = []
        mine = laps.pick_drivers(drv) if hasattr(laps, "pick_drivers") else laps.pick_driver(drv)
        for _, lp in mine.iterrows():
            rows.append([
                plain(lp.get("LapNumber"), int), secs(lp.get("Time"), t0), secs(lp.get("LapTime"), 0),
                secs(lp.get("Sector1Time"), 0), secs(lp.get("Sector2Time"), 0), secs(lp.get("Sector3Time"), 0),
                COMPOUND.get(str(lp.get("Compound", "")).upper(), "U"), plain(lp.get("TyreLife"), int),
                secs(lp.get("PitInTime"), t0), secs(lp.get("PitOutTime"), t0), plain(lp.get("SpeedST")),
            ])
        q = np.round(g).astype(int)
        frames.append([int(q[0])] + np.diff(q).tolist())
        drivers.append({
            "num": str(drv), "code": str(r.get("Abbreviation", drv)) if len(r) else str(drv),
            "name": str(r.get("FullName", drv)) if len(r) else str(drv),
            "team": str(r.get("TeamName", "")) if len(r) else "",
            "color": "#" + color.lstrip("#"), "grid": grid if grid else None,
            "retiredAt": None if finished else round(float(last_move), 1),
            "laps": rows,
        })

    status = []
    ts = getattr(ses, "track_status", None)
    if ts is not None and len(ts):
        for _, row in ts.iterrows():
            status.append([secs(row["Time"], t0), str(row["Status"])])
    rc = []
    msgs = getattr(ses, "race_control_messages", None)
    t0_date = getattr(ses, "t0_date", None)
    if msgs is not None and len(msgs) and t0_date is not None:
        for _, m in msgs.iterrows():
            when = (utc(m["Time"]) - utc(t0_date)).total_seconds() - t0
            cat = str(m.get("Category", ""))
            flag = str(m.get("Flag", "") or "")
            rc.append([round(when, 1), plain(m.get("Lap"), int), str(m.get("Message", "")),
                       "flag" if cat in ("Flag", "SafetyCar") or flag not in ("", "nan", "None") else "info"])
    weather = []
    wd = getattr(ses, "weather_data", None)
    if wd is not None and len(wd):
        for _, w in wd.iloc[::5].iterrows():
            weather.append([secs(w["Time"], t0), plain(w.get("AirTemp")), plain(w.get("TrackTemp")),
                            plain(w.get("Humidity")), plain(w.get("WindSpeed")), bool(w.get("Rainfall", False))])

    ev = ses.event
    return {
        "event": str(ev.get("EventName", "")), "year": int(pd.Timestamp(ev.get("EventDate")).year),
        "place": f"{ev.get('Location', '')}, {ev.get('Country', '')}".strip(", "),
        "session": "Race", "totalLaps": total, "hz": HZ, "start": 6.0,
        "duration": round(float(grid_t[-1] - t0), 1),
        "track": track.to_json(),
        "lapFields": ["lap", "end", "time", "s1", "s2", "s3", "compound", "tyreAge", "pitIn", "pitOut", "trap"],
        "drivers": drivers, "frames": frames, "trackStatus": status, "rc": rc,
        "weatherFields": ["t", "air", "track", "hum", "wind", "rain"], "weather": weather,
    }


def build_replay():
    fastf1 = bridge.enable_fastf1_cache()
    now = pd.Timestamp.now(tz="UTC")
    candidates = []
    for year in (now.year, now.year - 1):
        try:
            sch = fastf1.get_event_schedule(year, include_testing=False)
        except Exception as e:
            log(f"No schedule for {year}: {e}")
            continue
        for _, ev in sch.iterrows():
            rt = race_time(ev)
            if rt is not None and rt + pd.Timedelta(hours=4) < now:
                candidates.append((rt, year, str(ev["EventName"])))
    for _, year, name in sorted(candidates, reverse=True)[:3]:
        try:
            log(f"Building replay: {year} {name}")
            ses = fastf1.get_session(year, name, "R")
            ses.load(laps=True, telemetry=True, weather=True, messages=True)
            data = replay_from_session(ses)
            if len(data["drivers"]) < 10:
                raise RuntimeError("position data incomplete")
            (DATA / "replay.json").write_text(json.dumps(data, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
            (DATA / "replay-meta.json").write_text(json.dumps({"event": data["event"], "year": data["year"]}, ensure_ascii=False), encoding="utf-8")
            TRACKS.mkdir(parents=True, exist_ok=True)
            (TRACKS / f"{bridge.track_slug(data['event'])}.json").write_text(json.dumps(data["track"]), encoding="utf-8")
            log(f"Replay saved: {year} {name}, {len(data['drivers'])} drivers, "
                f"{(DATA / 'replay.json').stat().st_size / 1e6:.1f} MB")
            return
        except Exception as e:
            log(f"Replay of {name} not possible: {type(e).__name__}: {e}")
    log("No replay built")


def main():
    ap = argparse.ArgumentParser(description="Build the site's data folder")
    ap.add_argument("--skip-replay", action="store_true")
    args = ap.parse_args()
    DATA.mkdir(exist_ok=True)
    ok = True
    try:
        events = build_schedule()
        build_tracks(events)
    except Exception as e:
        ok = False
        log(f"Schedule/tracks failed: {type(e).__name__}: {e}")
    if not args.skip_replay:
        try:
            build_replay()
        except Exception as e:
            ok = False
            log(f"Replay failed: {type(e).__name__}: {e}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
