"""
Post-draft: waiver targets and trade proposals.

Both answer the same question in different currencies — what raises the points
your *starting lineup* scores. A player who does not crack your lineup adds
nothing, however good he looks in isolation, and that is why both features are
built on lineup_points rather than on raw projections.

Nothing here touches the draft path.
"""

import engine


# --------------------------------------------------------------------------- #
# waiver wire
# --------------------------------------------------------------------------- #
def free_agents(all_players, rostered, my_roster, top_n=12):
    """
    Unrostered players ranked by what they would add to your starting lineup,
    not by projection. The distinction matters: the best free agent on the board
    is worthless to you if he would sit behind two better players at his
    position, and a mediocre one is valuable if he plugs a hole.

    Each candidate is paired with the player he would displace, so a pickup that
    forces a drop is priced honestly.
    """
    taken = {p["player"] for p in rostered}
    pool = [p for p in all_players
            if p["player"] not in taken and p.get("fp", 0) > 0]
    base = engine.lineup_points(my_roster)
    out = []
    for p in sorted(pool, key=lambda x: -x["fp"])[:120]:
        gain = engine.lineup_points(my_roster + [p]) - base
        # If the roster is full, adding him means cutting your worst player.
        drop, net = None, gain
        if len(my_roster) >= engine.ROSTER_MAX:
            best_net, best_drop = None, None
            for d in my_roster:
                trimmed = [x for x in my_roster if x is not d]
                cand = engine.lineup_points(trimmed + [p]) - base
                if best_net is None or cand > best_net:
                    best_net, best_drop = cand, d
            net, drop = best_net, best_drop
        if net <= 0.2:
            continue
        out.append(dict(
            player=p["player"], pos=p["pos"], tm=p.get("tm"), bye=p.get("bye"),
            fp=p["fp"], adp=p.get("adp"), owned=p.get("owned"),
            gain=round(net, 1), starts=gain > 0.2,
            drop=drop["player"] if drop else None,
            drop_fp=drop["fp"] if drop else None))
    out.sort(key=lambda r: -r["gain"])
    if out:
        return out[:top_n]

    # Right after a draft nobody on waivers improves a full roster — every
    # startable player is owned. Rather than show an empty panel, fall back to
    # what the wire is actually for at that moment: covering the weeks your own
    # byes leave you thin, and the best body at each position if someone gets
    # hurt.
    weak = sorted({p["bye"] for p in my_roster if p.get("bye")},
                  key=lambda w: -engine.bye_damage([x for x in my_roster if x.get("bye") == w]))
    fallback = []
    for pos in ("RB", "WR", "TE", "QB"):
        best = [p for p in pool if p["pos"] == pos]
        best.sort(key=lambda p: -p["fp"])
        for p in best[:2]:
            fallback.append(dict(
                player=p["player"], pos=pos, tm=p.get("tm"), bye=p.get("bye"),
                fp=p["fp"], adp=p.get("adp"), owned=p.get("owned"),
                gain=0.0, starts=False, drop=None, drop_fp=None,
                cover=p.get("bye") not in {x.get("bye") for x in my_roster}))
    fallback.sort(key=lambda r: -r["fp"])
    return fallback[:top_n]


# --------------------------------------------------------------------------- #
# trades
# --------------------------------------------------------------------------- #
def _needs(roster):
    """Surplus and shortfall against what you must start each week."""
    counts = {}
    for p in roster:
        counts[p["pos"]] = counts.get(p["pos"], 0) + 1
    return {pos: counts.get(pos, 0) - n for pos, n in engine.STARTERS.items()}


def find_trades(my_roster, other_rosters, max_per_team=2, min_gain=3.0):
    """
    Trades where *both* sides' starting lineups improve.

    A proposal only makes the list if it raises the other manager's lineup too,
    measured the same way as your own. That is the whole test of reasonableness:
    not a points-for-points balance, which ignores roster shape, but whether the
    deal genuinely helps the person you are asking to accept it. Two teams thin
    and thick at opposite positions can both gain from the same swap, and those
    are the only trades worth sending.

    Covers 1-for-1 and 2-for-1 in both directions.
    """
    my_base = engine.lineup_points(my_roster)
    proposals = []

    for team, roster in other_rosters.items():
        their_base = engine.lineup_points(roster)
        found = []

        def consider(give, get):
            mine_after = [p for p in my_roster if p not in give] + list(get)
            them_after = [p for p in roster if p not in get] + list(give)
            if len(mine_after) > engine.ROSTER_MAX or len(them_after) > engine.ROSTER_MAX:
                return
            my_gain = engine.lineup_points(mine_after) - my_base
            their_gain = engine.lineup_points(them_after) - their_base
            if my_gain < min_gain or their_gain < min_gain:
                return
            found.append(dict(
                team=team,
                give=[dict(player=p["player"], pos=p["pos"], fp=p["fp"]) for p in give],
                get=[dict(player=p["player"], pos=p["pos"], fp=p["fp"]) for p in get],
                my_gain=round(my_gain, 1), their_gain=round(their_gain, 1),
                total=round(my_gain + their_gain, 1)))

        # Only players worth moving: skip the deep bench on both sides.
        mine = sorted(my_roster, key=lambda p: -p["fp"])[:11]
        theirs = sorted(roster, key=lambda p: -p["fp"])[:11]

        for a in mine:
            for b in theirs:
                if a["pos"] == b["pos"] and abs(a["fp"] - b["fp"]) < 1:
                    continue
                consider([a], [b])
        # two of yours for one of theirs, and the reverse
        for i, a1 in enumerate(mine):
            for a2 in mine[i + 1:]:
                for b in theirs:
                    consider([a1, a2], [b])
        for a in mine:
            for i, b1 in enumerate(theirs):
                for b2 in theirs[i + 1:]:
                    consider([a], [b1, b2])

        found.sort(key=lambda t: -t["my_gain"])
        # one proposal per shape, so the list is not ten variations of one idea
        seen, kept = set(), []
        for t in found:
            key = tuple(sorted(p["player"] for p in t["give"]))
            if key in seen:
                continue
            seen.add(key)
            kept.append(t)
            if len(kept) >= max_per_team:
                break
        proposals += kept

    proposals.sort(key=lambda t: -t["my_gain"])
    return proposals


def describe(t):
    """One line a human can read, and send."""
    g = " + ".join(f"{p['player']} ({p['pos']})" for p in t["give"])
    r = " + ".join(f"{p['player']} ({p['pos']})" for p in t["get"])
    return (f"Send {g} to {t['team']} for {r} — "
            f"you +{t['my_gain']:.0f}, they +{t['their_gain']:.0f}")
