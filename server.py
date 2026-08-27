#!/usr/bin/env python3
"""
Draft Room — live draft assistant for the Bad News Bears.

Runs entirely on this Mac. Two ways to feed it picks:

  MANUAL   click a player to mark him gone. Always works, nothing to configure.
  ESPN     paste your league ID + cookies in Settings, hit Start Sync, and it
           pulls the real draft every 20 seconds.

Nothing is uploaded anywhere. Credentials stay in config.json next to this file.
"""

import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
import random
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import engine

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = json.load(open(os.path.join(HERE, "data.json")))
CONFIG_PATH = os.path.join(HERE, "config.json")
SEASON = 2026
POLL_SECONDS = 20
SIM_DELAY = 1.1          # seconds between simulated rival picks

# Value over replacement drives every ranking decision; precompute it once.
# (auc / implied_pick / implied_round come from the workbook and are already in data.json)
for _p in DATA["players"]:
    _p["vorp"] = round(_p["fp"] - DATA["baseline"][_p["pos"]], 1)

PLAYERS = {p["player"]: p for p in DATA["players"]}
KEEPER_SLOTS = {int(k): v for k, v in DATA["keeper_slots"].items()}
MY_PICKS = [p["overall"] for p in DATA["mypicks"]]
MY_SLOT = DATA["myslot"]
TOTAL_PICKS = DATA["teams"] * DATA["rounds"]

LOCK = threading.RLock()


# --------------------------------------------------------------------------- #
# name matching: ESPN and The Athletic disagree about suffixes and punctuation
# --------------------------------------------------------------------------- #
SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def norm(name):
    s = name.lower().replace(".", "").replace("'", "").replace("-", " ")
    s = re.sub(r"[^a-z ]", "", s)
    parts = [w for w in s.split() if w and w not in SUFFIXES]
    return " ".join(parts)


def short(name):
    """First initial + last name, for the awkward cases."""
    p = norm(name).split()
    return f"{p[0][:1]} {p[-1]}" if len(p) >= 2 else norm(name)


NORM_INDEX, SHORT_INDEX = {}, {}
for _n in PLAYERS:
    NORM_INDEX.setdefault(norm(_n), _n)
    SHORT_INDEX.setdefault(short(_n), _n)


def resolve(espn_name):
    """Map an ESPN player name onto our projection set, or None."""
    n = norm(espn_name)
    if n in NORM_INDEX:
        return NORM_INDEX[n]
    s = short(espn_name)
    if s in SHORT_INDEX:
        return SHORT_INDEX[s]
    return None


# --------------------------------------------------------------------------- #
# draft state
# --------------------------------------------------------------------------- #
class State:
    def __init__(self):
        self.reset()

    def reset(self):
        # Keepers are off the board from pick 1 — nobody can draft them.
        self.gone = {}                       # player -> owner label
        self.rosters = {s: [] for s in range(1, DATA["teams"] + 1)}
        for k in DATA["keepers"]:
            self.gone[k["player"]] = k["team"]
            if k["player"] in PLAYERS:
                self.rosters[k["slot"]].append(PLAYERS[k["player"]])
        self.log = []                        # chronological picks we were told about
        self.overall = 1
        self._skip_keeper_slots()
        self.sync_on = False
        self.sync_msg = "Manual mode"
        self.sync_ok = None
        self.last_sync = None
        self.unmatched = []
        self.sim_on = False
        self.sim_auto = True

    @property
    def my_roster(self):
        return self.rosters[MY_SLOT]

    def _skip_keeper_slots(self):
        """Keeper rounds are auto-assigned, so walk past them."""
        while self.overall in KEEPER_SLOTS and self.overall <= TOTAL_PICKS:
            self.overall += 1

    def available(self):
        """Ordered by value over replacement, not raw points — a 371-point QB is
        worth less than a 255-point tight end in this league."""
        pool = [p for p in DATA["players"] if p["player"] not in self.gone]
        pool.sort(key=lambda p: -p["vorp"])
        return pool

    def slot_on_clock(self, overall):
        rd = (overall - 1) // DATA["teams"] + 1
        pos = (overall - 1) % DATA["teams"] + 1
        return pos if rd % 2 == 1 else DATA["teams"] + 1 - pos

    def team_on_clock(self, overall):
        slot = self.slot_on_clock(overall)
        return DATA["teamnames"].get(str(slot), f"Slot {slot}")

    def take(self, name, mine=False, owner=None, source="manual", slot=None):
        if name not in PLAYERS or name in self.gone:
            return False
        # In manual mode nobody tells us who picked, but the snake order does.
        if slot is None:
            slot = MY_SLOT if mine else self.slot_on_clock(self.overall)
        mine = (slot == MY_SLOT)
        label = owner or DATA["teamnames"].get(str(slot), f"Slot {slot}")
        self.gone[name] = label
        self.rosters[slot].append(PLAYERS[name])
        self.log.append(
            dict(player=name, pos=PLAYERS[name]["pos"], tm=PLAYERS[name]["tm"],
                 overall=self.overall, mine=mine, owner=label, source=source, slot=slot)
        )
        self.overall += 1
        self._skip_keeper_slots()
        return True

    def undo(self):
        if not self.log:
            return False
        last = self.log.pop()
        self.gone.pop(last["player"], None)
        slot = last.get("slot", MY_SLOT if last["mine"] else None)
        if slot in self.rosters:
            self.rosters[slot] = [p for p in self.rosters[slot]
                                  if p["player"] != last["player"]]
        self.overall = last["overall"]
        return True

    # ---- simulation ------------------------------------------------------ #
    def sim_choose(self, slot):
        """
        How a rival picks in practice mode: mostly by average draft position,
        nudged toward positions they still need to start, with enough noise that
        no two mock drafts are identical.
        """
        r = self.rosters[slot]
        counts = {}
        for p in r:
            counts[p["pos"]] = counts.get(p["pos"], 0) + 1
        holes = engine.open_starting_slots(r)
        pool = sorted([p for p in self.available() if p.get("adp")],
                      key=lambda p: p["adp"])[:26]
        if not pool:
            pool = self.available()[:26]
        if not pool:
            return None
        best = None
        for i, p in enumerate(pool):
            cap = engine.SOFT_CAP.get(p["pos"], 8)
            if counts.get(p["pos"], 0) >= cap:
                continue
            score = -i * 1.0
            if holes.get(p["pos"], 0) > 0:
                score += 5.0
            elif p["pos"] in engine.FLEX_ELIGIBLE and holes.get("FLEX", 0) > 0:
                score += 2.0
            if p["pos"] == "QB" and counts.get("QB", 0) >= 1:
                score -= 8.0
            if p["pos"] == "TE" and counts.get("TE", 0) >= 1:
                score -= 5.0
            score += random.gauss(0, 2.6)
            if best is None or score > best[0]:
                best = (score, p)
        return best[1] if best else None

    def sim_step(self):
        """Advance exactly one rival pick. Returns False when it's your turn."""
        if self.on_the_clock() or self.overall > TOTAL_PICKS:
            return False
        slot = self.slot_on_clock(self.overall)
        p = self.sim_choose(slot)
        if not p:
            return False
        self.take(p["player"], slot=slot, source="sim")
        return True

    def sim_run_to_me(self, limit=200):
        n = 0
        while n < limit and self.sim_step():
            n += 1
        return n

    # ---- pick arithmetic ------------------------------------------------- #
    def next_my_pick(self):
        for o in MY_PICKS:
            if o >= self.overall:
                return o
        return None

    def on_the_clock(self):
        return self.overall in MY_PICKS

    def picks_until_mine(self):
        nxt = self.next_my_pick()
        if nxt is None:
            return None
        live = [o for o in range(self.overall, nxt) if o not in KEEPER_SLOTS]
        return len(live)

    def gap_after_this(self):
        """Live picks between my current pick and my following one."""
        nxt = self.next_my_pick()
        if nxt is None:
            return 0
        following = [o for o in MY_PICKS if o > nxt]
        if not following:
            return TOTAL_PICKS
        return len([o for o in range(nxt + 1, following[0]) if o not in KEEPER_SLOTS])

    def picks_left_for_me(self):
        return len([o for o in MY_PICKS if o >= self.overall])

    def round_of(self, overall):
        return (overall - 1) // DATA["teams"] + 1

    def snapshot(self):
        avail = self.available()
        on_clock = self.on_the_clock()
        until = self.picks_until_mine()
        # When on the clock, the horizon that matters is the gap to my NEXT pick.
        horizon = self.gap_after_this() if on_clock else (until or 0)
        recs = engine.recommend(
            avail, self.my_roster, horizon, self.picks_left_for_me(), top_n=3,
            current_pick=self.overall, current_round=self.round_of(self.overall),
        )
        holes = engine.open_starting_slots(self.my_roster)
        byes = {}
        for p in self.my_roster:
            byes[p["bye"]] = byes.get(p["bye"], 0) + 1
        return dict(
            league=DATA["league"], myteam=DATA["myteam"], myslot=DATA["myslot"],
            overall=self.overall, round=self.round_of(self.overall),
            total_picks=TOTAL_PICKS,
            on_clock=on_clock, picks_until_mine=until,
            next_my_pick=self.next_my_pick(), picks_left_for_me=self.picks_left_for_me(),
            my_picks=MY_PICKS, gap_after=self.gap_after_this(),
            recommendations=recs,
            sheet=engine.sheet_board(avail, top_n=3),
            compare=engine.compare(recs, engine.sheet_board(avail, 1),
                                   self.my_roster, self.round_of(self.overall)),
            available=avail[:320],
            available_count=len(avail),
            my_roster=sorted(self.my_roster, key=lambda p: (-p["fp"])),
            lineup_points=round(engine.lineup_points(self.my_roster), 1),
            holes=holes, byes=byes,
            log=list(reversed(self.log[-40:])),
            sim=dict(on=self.sim_on, auto=self.sim_auto, delay=SIM_DELAY),
            team_count=DATA["teams"],
            team_list=[dict(slot=sl, name=DATA["teamnames"].get(str(sl), f"Slot {sl}"),
                        n=len(self.rosters[sl]),
                        mine=(sl == MY_SLOT))
                   for sl in range(1, DATA["teams"] + 1)],
            sync=dict(on=self.sync_on, msg=self.sync_msg, ok=self.sync_ok,
                      last=self.last_sync, unmatched=self.unmatched[-12:],
                      interval=POLL_SECONDS),
            configured=bool(load_config().get("league_id")),
            cfg=_public_config(),
        )


STATE = State()


# --------------------------------------------------------------------------- #
# ESPN
# --------------------------------------------------------------------------- #
def _public_config():
    """Config for the UI. Never echo the secrets back — only whether they exist."""
    c = load_config()
    return dict(league_id=c.get("league_id", ""), team_id=c.get("team_id", ""),
                has_s2=bool(c.get("espn_s2")), has_swid=bool(c.get("swid")))


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=1)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except Exception:
        pass


def _ssl_context():
    """
    This Python build ships without a populated CA store, so a plain
    create_default_context() fails to verify ESPN's certificate. certifi has the
    roots we need. We never fall back to unverified TLS.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


SSL_CTX = _ssl_context()


def espn_get(url, cfg, timeout=20):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        "Accept": "application/json",
        "X-Fantasy-Filter": "{}",
    })
    s2, swid = cfg.get("espn_s2", ""), cfg.get("swid", "")
    if s2 and swid:
        if not swid.startswith("{"):
            swid = "{" + swid.strip("{}") + "}"
        req.add_header("Cookie", f"espn_s2={s2}; SWID={swid}")
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        return json.loads(r.read().decode())


PLAYER_NAME_CACHE = {}


def espn_player_names(cfg):
    """playerId -> fullName for the whole player universe (fetched once)."""
    if PLAYER_NAME_CACHE:
        return PLAYER_NAME_CACHE
    url = (f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/"
           f"{SEASON}/players?scoringPeriodId=0&view=players_wl")
    rows = espn_get(url, cfg, timeout=45)
    for row in rows:
        pid, nm = row.get("id"), row.get("fullName")
        if pid is not None and nm:
            PLAYER_NAME_CACHE[int(pid)] = nm
    return PLAYER_NAME_CACHE


def espn_league(cfg):
    lid = cfg["league_id"]
    url = (f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/"
           f"{SEASON}/segments/0/leagues/{lid}?view=mDraftDetail&view=mTeam")
    return espn_get(url, cfg)


def sync_once():
    """Pull the real draft and apply any picks we haven't seen. Returns a message."""
    cfg = load_config()
    if not cfg.get("league_id"):
        return False, "No league ID saved yet."
    try:
        names = espn_player_names(cfg)
        league = espn_league(cfg)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, f"ESPN refused the request ({e.code}) — cookies missing or expired."
        return False, f"ESPN returned HTTP {e.code}."
    except Exception as e:
        return False, f"Could not reach ESPN: {type(e).__name__}: {e}"

    teams = {t.get("id"): (t.get("name") or f"Team {t.get('id')}")
             for t in (league.get("teams") or [])}
    # An explicit teamId from the league URL beats name matching: owners rename
    # teams mid-season and ESPN names often carry emoji the projections never see.
    my_id = None
    want = cfg.get("team_id")
    if want not in (None, ""):
        try:
            want = int(want)
            if want in teams:
                my_id = want
        except (TypeError, ValueError):
            pass
    if my_id is None:
        for tid, nm in teams.items():
            if norm(nm) == norm(DATA["myteam"]):
                my_id = tid
    picks = ((league.get("draftDetail") or {}).get("picks")) or []
    lname = (league.get("settings") or {}).get("name") or f"league {cfg['league_id']}"
    if not picks:
        found = (f"you are \u201c{teams.get(my_id)}\u201d" if my_id is not None
                 else "could NOT identify your team")
        return True, (f"Connected to \u201c{lname}\u201d · {len(teams)} teams · "
                      f"{found} · draft has not started")

    picks.sort(key=lambda p: p.get("overallPickNumber") or 0)
    applied, unmatched = 0, []
    with LOCK:
        for pk in picks:
            pid = pk.get("playerId")
            espn_name = names.get(int(pid)) if pid is not None else None
            if not espn_name:
                continue
            ours = resolve(espn_name)
            if not ours:
                if espn_name not in unmatched:
                    unmatched.append(espn_name)
                continue
            if ours in STATE.gone:
                continue
            tid = pk.get("teamId")
            mine = (my_id is not None and tid == my_id)
            STATE.take(ours, mine=mine, owner=teams.get(tid), source="espn")
            applied += 1
        if unmatched:
            for u in unmatched:
                if u not in STATE.unmatched:
                    STATE.unmatched.append(u)
    made = len(picks)
    msg = f"Live · {made} pick{'s' if made != 1 else ''} read from ESPN"
    if applied:
        msg += f" ({applied} new)"
    if my_id is None:
        msg += " · WARNING: could not identify your team (set team_id)"
    else:
        msg += f" · you are {teams.get(my_id)}"
    if unmatched:
        msg += f" · {len(unmatched)} name(s) unmatched"
    return True, msg


def sim_loop():
    """Rivals pick on a timer so a practice draft feels like the real room."""
    while True:
        time.sleep(0.25)
        with LOCK:
            go = STATE.sim_on and STATE.sim_auto and not STATE.on_the_clock()
        if not go:
            continue
        time.sleep(SIM_DELAY)
        with LOCK:
            if STATE.sim_on and STATE.sim_auto and not STATE.on_the_clock():
                STATE.sim_step()


def sync_loop():
    while True:
        time.sleep(2)
        if not STATE.sync_on:
            continue
        ok, msg = sync_once()
        with LOCK:
            STATE.sync_ok = ok
            STATE.sync_msg = msg
            STATE.last_sync = time.strftime("%-I:%M:%S %p")
            # 20s is fine mid-round, but tighten up as your turn approaches so the
            # board is current the moment you're on the clock.
            until = STATE.picks_until_mine()
            wait = 6 if (until is not None and until <= 2) else POLL_SECONDS
        for _ in range(wait):
            if not STATE.sync_on:
                break
            time.sleep(1)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        raw = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if self.path.startswith("/api/state"):
            with LOCK:
                return self._send(200, json.dumps(STATE.snapshot()))
        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}
        p = self.path

        with LOCK:
            if p == "/api/pick":
                ok = STATE.take(body.get("player", ""), mine=bool(body.get("mine")))
                return self._send(200, json.dumps({"ok": ok, **STATE.snapshot()}))
            if p == "/api/undo":
                ok = STATE.undo()
                return self._send(200, json.dumps({"ok": ok, **STATE.snapshot()}))
            if p == "/api/reset":
                STATE.reset()
                return self._send(200, json.dumps({"ok": True, **STATE.snapshot()}))
            if p == "/api/config":
                cfg = load_config()
                for k in ("league_id", "team_id", "espn_s2", "swid"):
                    if body.get(k) is not None and str(body[k]).strip():
                        cfg[k] = str(body[k]).strip()
                save_config(cfg)

        if p == "/api/config":
            ok, msg = sync_once()
            with LOCK:
                STATE.sync_ok, STATE.sync_msg = ok, msg
                if ok:
                    STATE.last_sync = time.strftime("%-I:%M:%S %p")
                return self._send(200, json.dumps({"ok": ok, "msg": msg, **STATE.snapshot()}))

        if p == "/api/sim":
            with LOCK:
                want = bool(body.get("on"))
                STATE.sim_on = want
                if "auto" in body:
                    STATE.sim_auto = bool(body["auto"])
                if want:
                    STATE.sync_on = False      # never mix live ESPN with practice
                    STATE.sync_msg = "SIMULATION — practice draft, not the real thing"
                elif not STATE.sync_on:
                    STATE.sync_msg = "Manual mode"
                return self._send(200, json.dumps(STATE.snapshot()))

        if p == "/api/sim/step":
            with LOCK:
                n = STATE.sim_run_to_me() if body.get("to_me") else int(STATE.sim_step())
                return self._send(200, json.dumps({"advanced": n, **STATE.snapshot()}))

        if p == "/api/sync":
            want = bool(body.get("on"))
            with LOCK:
                STATE.sync_on = want
                STATE.sync_msg = "Sync starting…" if want else "Manual mode"
            if want:
                ok, msg = sync_once()
                with LOCK:
                    STATE.sync_ok, STATE.sync_msg = ok, msg
                    STATE.last_sync = time.strftime("%-I:%M:%S %p")
            with LOCK:
                return self._send(200, json.dumps(STATE.snapshot()))

        return self._send(404, json.dumps({"error": "not found"}))


def main():
    port = int(os.environ.get("PORT", "8777"))
    threading.Thread(target=sync_loop, daemon=True).start()
    threading.Thread(target=sim_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print("\n  ┌────────────────────────────────────────────┐")
    print("  │   DRAFT ROOM · Bad News Bears              │")
    print("  └────────────────────────────────────────────┘")
    print(f"\n  Open:  {url}\n")
    print("  Manual mode works out of the box.")
    print("  For live ESPN sync, click Settings in the app.\n")
    print("  Press Ctrl+C to stop.\n")
    if os.environ.get("NO_OPEN") != "1":
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.\n")


if __name__ == "__main__":
    main()
