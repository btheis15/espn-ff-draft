"""
Draft recommendation engine — Bad News Bears, Unfortunate Association.

Core idea: a player is worth taking now only if taking him beats taking the best
player you could still get at that position at your NEXT pick. That is the real
opportunity cost in a snake draft, and it is what "value over next available"
(VONA) measures. Raw projected points alone will happily tell you to draft a
fifth receiver; VONA will not.
"""

STARTERS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1}
FLEX_SLOTS = 2
FLEX_ELIGIBLE = ("RB", "WR", "TE")
ROSTER_MAX = 15
# Soft caps: how many of each position a sane 15-man roster wants.
SOFT_CAP = {"QB": 2, "RB": 6, "WR": 8, "TE": 2}

# Measured season-long cost of taking your QB1 in each round, from 400-draft
# Monte Carlo runs of this exact league. Waiting is close to free after round 6:
# Purdy, Prescott, C. Williams and Lawrence all project within a few points of
# the QB replacement level of 308.
QB_EARLY_COST = {1: 25.0, 2: 22.0, 3: 20.0, 4: 16.0, 5: 10.0, 6: 2.0}

# Replacement-level points per position, filled in from the data at load time.
# Used to project what a finished roster looks like when costing a bye stack.
REPLACEMENT = {}

# Bye-week tie-breakers.
#
# These are a preference, not a measurement, and the distinction matters. Testing
# showed that stacking byes costs almost nothing in season points: when a starter
# is off you promote a bench player, and that swap costs (starter − bench)
# whether the absences clump together or fall in different weeks. The loss is
# linear in how many are out, not in how they cluster — it only turns superlinear
# once more players are out than the bench can cover, which on a 15-man roster
# with 8 starters essentially never happens.
#
# What a clash does cost is harder to price: less room to manoeuvre in that week,
# and worse exposure if an injury lands on top of it. So these are deliberately
# small — sized against a median gap of ~16 points between the top two
# candidates, they decide a close call and cannot outvote a materially better
# player. The genuinely severe cases are still caught by the measured
# lineup-shortfall term, which is added on top.
SAME_POS_BYE_TIEBREAK = 4.0
OTHER_POS_BYE_TIEBREAK = 1.0

# Once there is a bench to measure, bye_impact prices the real cascade instead:
# who of yours is off that week, who slides up, which bench body actually enters
# the lineup, and what that costs. Those two never both apply.


def lineup_points(roster):
    """Best possible starting lineup from a roster: 1QB 2RB 2WR 1TE + 2 FLEX."""
    by_pos = {}
    for p in roster:
        by_pos.setdefault(p["pos"], []).append(p["fp"])
    for v in by_pos.values():
        v.sort(reverse=True)

    total = 0.0
    used = {}
    for pos, n in STARTERS.items():
        take = by_pos.get(pos, [])[:n]
        total += sum(take)
        used[pos] = len(take)

    bench = []
    for pos in FLEX_ELIGIBLE:
        bench += by_pos.get(pos, [])[used.get(pos, 0):]
    bench.sort(reverse=True)
    return total + sum(bench[:FLEX_SLOTS])


def best_lineup(roster):
    """
    The actual eight players you would start, not just the total.

    Needed because the cost of a bye is a cascade, not a substitution: lose a
    starting receiver and your flex receiver slides up, and the body that really
    enters the lineup is the best man on your bench. What that costs depends
    entirely on who you already have.
    """
    picked, used = [], set()
    by_pos = {}
    for p in roster:
        by_pos.setdefault(p["pos"], []).append(p)
    for v in by_pos.values():
        v.sort(key=lambda p: -p["fp"])
    for pos, n in STARTERS.items():
        for p in by_pos.get(pos, [])[:n]:
            picked.append((pos, p)); used.add(id(p))
    flex = sorted([p for p in roster
                   if p["pos"] in FLEX_ELIGIBLE and id(p) not in used],
                  key=lambda p: -p["fp"])[:FLEX_SLOTS]
    for p in flex:
        picked.append(("FLEX", p)); used.add(id(p))
    return picked


def bye_impact(player, roster):
    """
    What this player's bye week actually does to your lineup, given your roster.

    Returns who is out that week, who slides up to cover, the body that enters
    from the bench, and the points that week costs against a full-strength week.
    """
    full = list(roster) + [player]
    week = player.get("bye")
    if not week:
        return None
    normal = best_lineup(full)
    normal_ids = {id(p) for _, p in normal}
    avail = [p for p in full if p.get("bye") != week]
    reduced = best_lineup(avail)
    out = [p for p in full if p.get("bye") == week and id(p) in normal_ids]
    entering = [p for _, p in reduced if id(p) not in normal_ids]
    cost = (sum(p["fp"] for _, p in normal) - sum(p["fp"] for _, p in reduced)) / GAMES
    return dict(week=week, out=out, entering=entering, cost=round(max(0.0, cost), 1),
                starters_lost=len(out))


def open_starting_slots(roster):
    """Which starting slots are still unfilled, and how many flex spots remain."""
    counts = {}
    for p in roster:
        counts[p["pos"]] = counts.get(p["pos"], 0) + 1
    holes = {pos: max(0, n - counts.get(pos, 0)) for pos, n in STARTERS.items()}
    surplus = sum(max(0, counts.get(pos, 0) - STARTERS[pos]) for pos in FLEX_ELIGIBLE)
    holes["FLEX"] = max(0, FLEX_SLOTS - surplus)
    return holes


GAMES = 17          # projections are season totals across a 17-game schedule


def weekly_lineup(roster, week):
    """Best lineup you could actually field in a given week, per game."""
    return lineup_points([p for p in roster if p.get("bye") != week]) / GAMES


def bye_damage(roster):
    """
    Points lost across the season to bye weeks.

    A bye only hurts when it empties a slot you have to start. Comparing the
    lineup you can field that week against the one you would field with everyone
    healthy captures that automatically, and it is position-aware for free: two
    running backs sharing a bye costs far more than two players sharing one when
    the second is a bench receiver, because only the first forces a replacement
    into your starting lineup.
    """
    if not roster:
        return 0.0
    ideal = lineup_points(roster) / GAMES
    weeks = {p.get("bye") for p in roster if p.get("bye")}
    return sum(max(0.0, ideal - weekly_lineup(roster, w)) for w in weeks)


def _projected_roster(roster, extra):
    """
    Your roster as it will look on kickoff, not as it looks mid-draft.

    A bye stack taken in round 4 does its damage in week 6, by which point the
    roster is full. Two details matter, and both were wrong in earlier versions:

    1. Fill to the whole 15-man roster, not just the eight starting slots. With
       no bench, clustering byes is mathematically free — two players out in one
       week costs exactly what two players out in separate weeks costs, because
       every absence is a straight subtraction. Stacking only hurts when there is
       a bench to absorb a single-week hole, and it is overwhelmed.
    2. Spread the filler across distinct bye weeks, so the filler never invents a
       collision of its own.
    """
    out = list(roster) + [extra]
    used = {p.get("bye") for p in out}
    free = [w for w in range(5, 15) if w not in used] or list(range(5, 15))
    i = 0

    def add(pos):
        nonlocal i
        out.append(dict(player=f"replacement {pos}", pos=pos,
                        fp=REPLACEMENT.get(pos, 150.0), bye=free[i % len(free)]))
        i += 1

    holes = open_starting_slots(out)
    for pos in ("QB", "RB", "WR", "TE"):
        for _ in range(holes.get(pos, 0)):
            add(pos)
    for _ in range(holes.get("FLEX", 0)):
        add("WR")
    # bench: whatever is left of the 15, weighted the way a real bench skews
    bench_mix = ("RB", "WR", "WR", "RB", "WR", "TE", "QB")
    b = 0
    while len(out) < ROSTER_MAX:
        add(bench_mix[b % len(bench_mix)])
        b += 1
    return out


def bye_cost(player, roster):
    """
    What this player's bye colliding with byes you already own actually costs,
    in season points.

    Every player misses one week, so the raw damage of adding anyone is much the
    same and tells you nothing. The number that matters is the excess: the same
    player, on a week nobody else has, versus on the week he really has. The
    difference is pure collision — measured against the roster you will finish
    with, so a stack created in round 4 is costed at what it does in week 6.
    """
    if not roster:
        return 0.0
    used = {x.get("bye") for x in roster}
    free = next((w for w in range(1, 19) if w not in used and w != player.get("bye")), None)
    if free is None:
        return 0.0
    # Priced off the real cascade on the roster you actually hold, not a
    # synthetic one: filling the bench with generic replacement-level bodies
    # washed out the very thing that matters, which is how good YOUR next man up
    # happens to be. Every player misses one week, so the baseline is his cost on
    # a week nobody else has; the excess over that is what stacking really costs.
    here = bye_impact(player, roster)
    clean = bye_impact(dict(player, bye=free), roster)
    if not here or not clean:
        return 0.0
    return max(0.0, here["cost"] - clean["cost"])


def marginal_gain(player, roster):
    """Points this player adds to your optimal starting lineup."""
    return lineup_points(roster + [player]) - lineup_points(roster)


def draft_order_key(p):
    """
    Expected draft order. ESPN publishes a real average draft position for every
    owned player, which beats any model of how rivals behave — it is measured
    behaviour rather than an assumption. Players with no ADP go undrafted in
    practice, so they sort last.

    Sorting by raw points here would be a mistake: quarterbacks sit atop the raw
    board all draft long (a 325-point QB is nearly worthless when the tenth-best
    throws for 308), which makes every QB look permanently scarce. VORP is the
    fallback when ADP is missing.
    """
    adp = p.get("adp")
    return adp if adp else 900 - min(p.get("vorp", 0), 300)


def future_pool(available, picks_until_my_turn, exclude=None):
    """Who is plausibly still on the board at your next pick."""
    pool = [p for p in available if p["player"] != (exclude or "")]
    pool.sort(key=draft_order_key)
    return pool[picks_until_my_turn:]


def _best_at(pool, pos, exclude=None):
    cands = [p for p in pool if p["pos"] == pos and p["player"] != (exclude or "")]
    return max(cands, key=lambda p: p["fp"]) if cands else None


def market_slip(player, current_pick):
    """
    How far a player has fallen past where the market drafts him.

    Prefers ESPN's real average draft position; falls back to the implied pick
    derived from the workbook's auction prices. Positive means the room has let
    him slide past his usual cost — that is what "value in this round" means.
    """
    if not current_pick:
        return 0
    ref = player.get("adp") or player.get("implied_pick")
    if not ref:
        return 0
    return round(ref - current_pick, 1)


def sheet_board(available, top_n=3):
    """
    What the original workbook would take: best available by its own overall
    rank, with nothing added. The workbook ranks on value over replacement too,
    but against a far looser baseline — it measures a player against the last
    *rosterable* player rather than the last *startable* one, which is why it
    rates Josh Allen 13th overall (+148) where this engine has him at +63.

    Shown unmodified so the two boards can be compared honestly.
    """
    pool = [p for p in available if p.get("wb_rank")]
    pool.sort(key=lambda p: p["wb_rank"])
    return [
        dict(player=p["player"], pos=p["pos"], tm=p["tm"], bye=p["bye"], fp=p["fp"],
             wb_rank=p["wb_rank"], wb_vorp=p["wb_vorp"], wb_posrk=p.get("wb_posrk"),
             vorp=p.get("vorp"), adp=p.get("adp"), auc=p.get("auc"))
        for p in pool[:top_n]
    ]


def compare(mine, sheet, roster, current_round=None):
    """
    Explain, in specifics, why this engine's top pick differs from the sheet's —
    or confirm that it doesn't.
    """
    if not mine or not sheet:
        return None
    a, b = mine[0], sheet[0]
    if a["player"] == b["player"]:
        return dict(agree=True, text=(
            f"The workbook agrees — {a['player']} is its top available player "
            f"(its overall rank {b['wb_rank']})."))

    counts = {}
    for p in roster:
        counts[p["pos"]] = counts.get(p["pos"], 0) + 1
    holes = open_starting_slots(roster)
    bits = []

    if b["pos"] == "QB" and current_round and current_round <= 6:
        bits.append(
            f"the sheet rates {b['player']} on raw value ({b['wb_vorp']:+.0f}) against a "
            f"loose baseline, but a startable quarterback is worth only "
            f"{b.get('vorp') or 0:+.0f} here, and simulations put the cost of a "
            f"round-{current_round} QB at {QB_EARLY_COST.get(current_round, 0):.0f} points")
    if holes.get(a["pos"], 0) > 0 and holes.get(b["pos"], 0) == 0:
        bits.append(f"he fills your open {a['pos']} slot while {b['pos']} is already covered")
    if counts.get(b["pos"], 0) >= SOFT_CAP.get(b["pos"], 9) - 1:
        bits.append(f"you already hold {counts.get(b['pos'])} at {b['pos']}")
    if a.get("slip", 0) >= 12:
        bits.append(f"he has slid {a['slip']:.0f} picks past his usual draft cost")
    if a.get("cliff") and a["cliff"] >= 25:
        bits.append(f"the drop to the next {a['pos']} is {a['cliff']:.0f} points")
    if not bits:
        bits.append("the sheet ignores roster need and what you can still get at your next pick")

    why = "; ".join(bits[:3])
    return dict(agree=False, text=(
        f"The sheet would take {b['player']} ({b['pos']}, its rank {b['wb_rank']}). "
        f"This engine prefers {a['player']} — {why}."))


LENSES = ("VONA", "EDGE", "SHEET", "MARKET")


def lens_ranks(available, needed):
    """
    Rank the pool four independent ways so agreement between them can be seen.

    VONA   this engine — value over next available, adjusted for your roster
    EDGE   raw value over replacement, ignoring your roster and your pick gap
    SHEET  The Athletic's own overall board, untouched
    MARKET where ESPN's average draft position says he goes

    They are genuinely independent: EDGE knows nothing about your schedule, SHEET
    knows nothing about your league's keepers, and MARKET is other people's
    behaviour rather than any projection at all.
    """
    pool = [p for p in available if p["pos"] in needed]
    out = {}
    for key, sort in (
        ("EDGE", lambda p: -p.get("vorp", 0)),
        ("SHEET", lambda p: p.get("wb_rank") or 9999),
        ("MARKET", lambda p: p.get("adp") or 9999),
    ):
        for i, p in enumerate(sorted(pool, key=sort), 1):
            out.setdefault(p["player"], {})[key] = i
    return out


def consensus_of(n):
    """How many of the four methods would take this player out of the three shown."""
    txt = {
        4: ("ALL 4 AGREE", "every method picks him out of these three — your most confident pick"),
        3: ("3 OF 4", "three of the four methods pick him out of these three"),
        2: ("2 OF 4", "two of the four pick him — the methods are split"),
        1: ("1 OF 4", "only one method picks him; if that is this engine, it is the only one "
                      "that knows your roster, your keepers and your pick gap"),
    }.get(n, ("0 OF 4", "no method rates him first of the three"))
    return dict(verdict=txt[0], agree=n, note=txt[1])


def recommend(available, roster, picks_until_my_turn, picks_left_for_me,
              top_n=3, current_pick=None, current_round=None):
    """
    Rank available players by value-over-next-available, then explain each pick
    in plain language backed by the numbers that drove it.
    """
    if not available or picks_left_for_me <= 0:
        return []

    counts = {}
    for p in roster:
        counts[p["pos"]] = counts.get(p["pos"], 0) + 1
    holes = open_starting_slots(roster)
    my_byes = {}
    for p in roster:
        my_byes[p["bye"]] = my_byes.get(p["bye"], 0) + 1

    # Positions we still genuinely need bodies at.
    needed = set()
    for pos in STARTERS:
        if counts.get(pos, 0) < SOFT_CAP[pos]:
            needed.add(pos)

    # Only the plausible candidates need full evaluation. Nobody's best pick is
    # the 200th-best player left, and scoring all of them made each call O(n^2).
    shortlist = sorted(
        (p for p in available if p["pos"] in needed),
        key=lambda p: -p.get("vorp", p["fp"]),
    )[:45]
    # Keep the market's favourites in contention even if our value board is cool
    # on them, so a genuine ADP bargain is never silently skipped.
    by_adp = sorted((p for p in available if p["pos"] in needed and p.get("adp")),
                    key=lambda p: p["adp"])[:25]
    seen = {p["player"] for p in shortlist}
    shortlist += [p for p in by_adp if p["player"] not in seen]

    scored = []
    for p in shortlist:
        pos = p["pos"]

        gain = marginal_gain(p, roster)

        # Opportunity cost: the best OTHER player at this position you could still
        # get next time up. He must be excluded from his own comparison, or every
        # player scores zero against himself.
        later = future_pool(available, picks_until_my_turn, exclude=p["player"])
        alt = _best_at(later, pos, exclude=p["player"])
        alt_gain = marginal_gain(alt, roster) if alt else 0.0
        vona = gain - alt_gain

        # A player who fills nothing you start is worth less than his raw points.
        starts_now = holes.get(pos, 0) > 0 or (pos in FLEX_ELIGIBLE and holes["FLEX"] > 0)
        if not starts_now:
            vona *= 0.55

        # Don't hoard a position you've already solved.
        if counts.get(pos, 0) >= SOFT_CAP[pos] - 1:
            vona *= 0.7

        # Bye weeks. Two parts, because they answer different questions.
        #
        # The first is a plain preference: given two similar players, take the one
        # who is not on a week you already have covered — and weigh a clash at the
        # SAME position much harder, since that is the one that leaves a starting
        # slot empty rather than merely thinning the bench.
        #
        # The second is the measured lineup shortfall, which only bites once the
        # roster is deep enough for a bye to actually cost you a starter. Early on
        # it is correctly zero; the preference term still applies.
        #
        # Both are deliberately small next to real value gaps: they should decide
        # a close call, never talk you out of a materially better player.
        bye_clash = my_byes.get(p["bye"], 0)
        same_pos_bye = sum(1 for x in roster
                           if x.get("bye") == p["bye"] and x["pos"] == pos)
        # The cascade is the real number; the flat tie-break is only a stand-in
        # for the early rounds when there is no bench yet for it to measure.
        # Applying both would double-count.
        bcost = bye_cost(p, roster)
        tie = 0.0 if bcost >= 1.0 else min(
            9.0, SAME_POS_BYE_TIEBREAK * same_pos_bye
                 + OTHER_POS_BYE_TIEBREAK * (bye_clash - same_pos_bye))
        vona_before_bye = vona
        vona -= (tie + bcost)

        # Quarterback timing. A per-pick view cannot see that spending an early
        # pick on a QB quietly costs you a starter at a scarce position for the
        # rest of the draft. These numbers are the measured season-long cost from
        # 400-draft simulations of forcing QB1 in each round, and they stop the
        # app from recommending something the tested plan rejects.
        if pos == "QB" and current_round:
            vona -= QB_EARLY_COST.get(current_round, 0.0)

        # Market check: reward a player the room has let slide past his price.
        # Capped, and deliberately smaller than the positional-need signal — it
        # is a tiebreaker between comparable players, not the thing driving the
        # ranking.
        slip = market_slip(p, current_pick)
        bonus = min(max(slip, 0), 45) * 0.30
        # A bargain at a position you already start is still only a bargain for
        # your bench, so it should not leapfrog a genuine starting need.
        if holes.get(pos, 0) == 0:
            bonus *= 0.45
        vona += bonus

        scored.append(
            dict(
                player=p["player"], pos=pos, tm=p["tm"], bye=p["bye"], fp=p["fp"],
                score=round(vona, 1), score_no_bye=round(vona_before_bye, 1),
                gain=round(gain, 1),
                alt=alt["player"] if alt else None,
                alt_fp=alt["fp"] if alt else None,
                cliff=round(p["fp"] - alt["fp"], 1) if alt else None,
                starts_now=starts_now, bye_clash=bye_clash,
                impact=bye_impact(p, roster),
                bye_cost=round(bcost, 1), same_pos_bye=same_pos_bye,
                bye_penalty=round(tie + bcost, 1),
                auc=p.get("auc"), implied_round=p.get("implied_round"),
                implied_pick=p.get("implied_pick"),
                adp=p.get("adp"), owned=p.get("owned"), _pick=current_pick,
                vorp=p.get("vorp"),
                pass_pts=p.get("pass_pts", 0), rush_pts=p.get("rush_pts", 0),
                rec_pts=p.get("rec_pts", 0), td_pts=p.get("td_pts", 0),
                tgt=p.get("tgt", 0), rec=p.get("rec", 0), ruat=p.get("ruat", 0),
                tds=p.get("tds", 0), wb_rank=p.get("wb_rank"),
                slip=slip, market_bonus=round(bonus, 1),
            )
        )

    scored.sort(key=lambda r: -r["score"])
    for i, r in enumerate(scored, 1):
        r["rank_vona"] = i
    lr = lens_ranks(available, needed)
    for r in scored:
        got = lr.get(r["player"], {})
        r["rank_edge"] = got.get("EDGE")
        r["rank_sheet"] = got.get("SHEET")
        r["rank_market"] = got.get("MARKET")
    out = scored[:top_n]

    # Agreement is judged over the shortlist actually shown, not the whole pool:
    # ranking against a pool that shrinks every pick made "unanimous" mean
    # something different at every turn.
    if out:
        head = [next(p for p in available if p["player"] == r["player"]) for r in out]
        firsts = {r["player"]: (1 if i == 0 else 0) for i, r in enumerate(out)}
        for key, srt in (("EDGE", lambda p: -(p.get("vorp") or -999)),
                         ("SHEET", lambda p: p.get("wb_rank") or 9999),
                         ("MARKET", lambda p: p.get("adp") or 9999)):
            firsts[min(head, key=srt)["player"]] += 1
        for r in out:
            r["consensus"] = consensus_of(firsts[r["player"]])

    # Did the bye penalty actually change the order? Only worth mentioning if so.
    by_raw = sorted(scored, key=lambda r: -r["score_no_bye"])
    raw_top = by_raw[0]["player"] if by_raw else None
    for rank, r in enumerate(out):
        r["rank"] = rank + 1
        r["bye_broke_tie"] = bool(
            rank == 0 and raw_top and raw_top != r["player"]
            and abs(by_raw[0]["score_no_bye"] - r["score_no_bye"]) <= 12)
        r["displaced"] = raw_top if r.get("bye_broke_tie") else None
        r["reasons"] = _reasons(r, available, roster, holes, counts,
                                picks_until_my_turn, current_round)
    return out


def _reasons(r, available, roster, holes, counts, gap, current_round=None):
    """Two to four specific, numeric reasons this player is on the shortlist."""
    pos, out = r["pos"], []

    # 1. Does he fill something you actually start?
    if holes.get(pos, 0) > 0:
        n = holes[pos]
        out.append(f"Fills an empty {pos} starting slot — you still need {n}.")
    elif r["starts_now"]:
        out.append(f"Slots straight into FLEX; you have {holes['FLEX']} flex spot(s) open.")
    else:
        out.append(f"Bench value — your {pos} starters are already set.")

    # 2. The cost of waiting, which is the whole point.
    if r["alt"] and r["cliff"] is not None:
        if r["cliff"] >= 25:
            out.append(
                f"Waiting is expensive: best {pos} likely left at your next pick is "
                f"{r['alt']} ({r['alt_fp']:.0f} pts) — a {r['cliff']:.0f}-point drop."
            )
        elif r["cliff"] >= 8:
            out.append(
                f"Modest cliff — next {pos} you'd get is {r['alt']}, "
                f"{r['cliff']:.0f} pts worse."
            )
        else:
            out.append(
                f"Position is deep — {r['alt']} is only {max(r['cliff'],0):.0f} pts behind, "
                f"so this is about value, not scarcity."
            )
    else:
        out.append(f"You are near the end of usable {pos} depth.")

    # 3. Scarcity of genuinely comparable players.
    near = [
        p for p in available
        if p["pos"] == pos and p["player"] != r["player"] and abs(p["fp"] - r["fp"]) <= 12
    ]
    if len(near) == 0:
        out.append("No comparable player left at this position — he is alone in his tier.")
    elif len(near) <= 2:
        out.append(f"Only {len(near)} comparable {pos} left; the tier is about to break.")
    else:
        out.append(f"{len(near)} similar {pos}s available — you could pivot without much loss.")

    # 4. Market value — where the room actually drafts him.
    adp, slip = r.get("adp"), r.get("slip", 0)
    if adp:
        adp_rd = int((adp - 1) // 10 + 1)
        if slip >= 12:
            out.append(
                f"Bargain: ESPN drafts him around pick {adp:.0f} (Round {adp_rd}) — "
                f"he has slid {slip:.0f} picks past his usual cost."
            )
        elif slip <= -12:
            out.append(
                f"This is a reach: ESPN's average draft position is pick {adp:.0f} "
                f"(Round {adp_rd}), and you're at pick {r.get('_pick', '?')}."
            )
        else:
            out.append(f"Goes around pick {adp:.0f} (Round {adp_rd}) — you're right on schedule.")
    elif r.get("auc"):
        out.append(f"No market data; workbook prices him at ${r['auc']:.0f}.")

    # 5. Bye-week collision, only when it actually costs you something.
    spb, clash, pen = r.get("same_pos_bye", 0), r.get("bye_clash", 0), r.get("bye_penalty", 0)
    imp = r.get("impact")
    # imp["out"] includes the candidate himself — obvious, and not worth saying.
    others_out = [p for p in (imp or {}).get("out", []) if p["player"] != r["player"]]
    if imp and others_out and imp["cost"] >= 1:
        gone = ", ".join(p["player"] for p in others_out[:3])
        entering = imp["entering"][0]["player"] if imp["entering"] else None
        tail = (f"{entering} comes off your bench" if entering
                else "you have nobody on the bench to cover")
        out.append(
            f"Week {imp['week']}: you would also be without {gone}. Your lineup shuffles up and "
            f"{tail} — about {imp['cost']:.0f} fewer points that week.")
    elif spb:
        out.append(
            f"Bye clash — {spb} other {pos}{'s' if spb > 1 else ''} of yours is off in week "
            f"{r['bye']} too. Small tie-break ({pen:.0f} pts); it will not stop you taking him "
            f"if he is clearly better.")
    elif clash:
        out.append(f"Shares bye week {r['bye']} with {clash} of yours at other positions — "
                   f"a slight tie-break ({pen:.0f} pts).")
    else:
        out.append(f"Clean bye: nobody else on your roster is off in week {r['bye']}.")

    # 5. Roster-shape guard rails.
    if pos == "QB" and counts.get("QB", 0) == 0 and gap > 0:
        out.append("You have no quarterback yet — one is required every week.")

    return out
