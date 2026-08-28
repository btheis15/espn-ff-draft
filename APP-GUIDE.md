# Draft Room — Bad News Bears

A live draft assistant that runs on this Mac. It knows your league's exact rules,
your two keepers, your slot-6 pick schedule, and every other team's keepers, and
it recommends three players every time you're on the clock — with the reasoning.

## Start it

Double-click **`Start Draft Room.command`**. Your browser opens automatically.
To stop it, press `Ctrl+C` in the Terminal window, or just close the window.

If double-clicking is blocked by macOS, open Terminal and run:

```
cd ~/Desktop/"Fantasy Football"/draft-app
python3 server.py
```

## Two ways to feed it picks

**Manual** — works immediately, nothing to set up. As each player comes off the
board, click `✕ taken` next to his name (or use the buttons on the recommendation
cards). It infers which team made the pick from the snake order. Hit `Undo` if you
misclick.

**ESPN sync** — click `⚙ Settings`, paste three values, then `▶ Start Sync`. It
reads the real draft every 20 seconds, tightening to 6 seconds when you're within
two picks of your turn. You do **not** need the draft page open — it calls ESPN's
data API directly, not your browser.

What you need:

| Value | Where to find it |
|---|---|
| League ID | already saved: **330075** |
| Your team ID | already saved: **12** |
| `espn_s2` | Chrome → your league page on espn.com → `⌥⌘I` → Application → Cookies → `https://www.espn.com` |
| `SWID` | same place, includes the curly braces |

Credentials are written to `config.json` beside this file, chmod 600. Nothing is
uploaded anywhere; the only outbound requests go to ESPN.

## How to read the status light

The pill in the header tells you exactly where you stand:

- **grey — "Manual mode"** — not syncing; your clicks are the source of truth
- **green, pulsing — "Live · N picks read from ESPN"** — connected and working
- **red** — connection failed, with the reason spelled out:
  - `401` → cookies missing or expired (re-copy them; they rotate)
  - `404` → wrong league ID
  - `Could not reach ESPN` → network problem

A failed sync never blocks you. Manual clicking keeps working, and the
recommendations keep updating either way.

Two warnings worth watching for in that message:

- *"could NOT find a team named 'Bad News Bears'"* — your ESPN team name differs,
  so it can't tell which picks are yours. Fix the name in `data.json` (`myteam`).
- *"N name(s) unmatched"* — a drafted player wasn't in the projection set. All 443
  projected players were verified to match ESPN's names, so this should only ever
  be a deep bench player nobody is choosing between.

## How a recommendation is made

Ranking is **value over next available**, not raw points. A player is worth taking
now only if he beats the best player you could still get at that position at your
*next* pick — which for you is often 9 to 13 picks later, and 19 after round one.

Who is still on the board at that next pick is modelled from **real ADP**, not
from a guess about how your rivals think. That is measured behaviour rather than
an assumption.

Four things adjust that number:

1. **Positional need** — filling an empty starting slot counts fully; bench depth
   is discounted.
2. **Market value** — ESPN publishes a real average draft position for every
   owned player, and that is public data (no cookies needed). The app pulls it
   for 440 of your 443 players, so it can say "ESPN drafts him around pick 24 and
   he has slid 18 picks past his usual cost." The workbook's auction prices are
   the fallback for anyone without an ADP. Capped, and halved for positions you
   have already filled.
3. **Quarterback timing** — a per-pick view can't see that an early QB costs you a
   scarce starter all draft. The penalties are the measured season-long cost from
   400-draft simulations: −25 in round 1 down to −2 by round 6, then nothing.
4. **Bye weeks** — a small penalty for stacking a week you're already thin on.

A note on the ADP data: ESPN lists duplicate names (there are two Justin
Jeffersons — a Minnesota receiver owned in 99.8% of leagues, and a 1%-owned
defender). Matching is keyed on name *and* position, keeping the most-owned
record, or the app would rank a star as a late-round flier.

Points come from The Athletic's 8/24 projections rescored to full 1.0 PPR,
verified to the decimal against the recalculated workbook. Replacement levels are
re-derived after removing all 20 keepers: QB 308 · RB 202 · WR 198 · TE 182.

## The visuals

Five charts, each answering one question you actually ask on the clock. All are
plain HTML/CSS or inline SVG — no chart library, nothing fetched.

**What kind of player is he?** A stacked bar under each name splits his projected
points into passing / rushing / receiving, with his target and carry volume and
what share of his points come from touchdowns. A back who is 51% rushing with 22%
of his points from TDs is a different asset from a receiver at 100% receiving on
148 targets — and TD-heavy players are flagged as swingy.

**How does he sit among who is left?** For the top recommendation, a bar chart of
everyone still available at his position, tallest first, with him highlighted and
the biggest remaining drop marked. Players below replacement are excluded — down
there everyone is interchangeable, so plotting them is ink without information.
When four of the top five tight ends are kept, this chart makes the cliff obvious
in a way a number cannot.

**How much do the methods disagree?** A four-row dot plot putting this engine,
raw value, the sheet's board and ESPN's market on one rank axis. A tight cluster
means take him; a wide split means read the reasons.

**Which position should I take now?** The cost-of-waiting panel: for each
position, the points you lose by waiting until your next pick instead of taking
the best one now, sorted worst-first. This deliberately does *not* rank by how
many players remain — quarterback can be ten-deep for ten slots and still cost
nothing to skip, because the tenth-best still throws for 308.

**Is there a run?** The last sixteen picks as position chips, with a warning when
three or more of one position go back-to-back.

### A note on the colours

The position palette is stepped for the dark surface and validated with a
colourblindness checker rather than chosen by eye: adjacent-pair CVD ΔE 8.4,
normal-vision ΔE 19.3, every hue at least 3:1 against the surface. The order
matters — aqua and magenta fail when adjacent, so they never are. Position
letters accompany every colour, so hue is never the only signal. The stacked
point-source segments are a separate validated trio, ordered so the pair that
fails for protanopia never touches.

## Seeing the sheet's pick next to mine

Under the recommendation cards, **"What the original workbook would take"** shows
the workbook's own top three available players, straight off its overall board
with nothing added — plus a line explaining why the two boards agree or differ.

The comparison is real, not cosmetic. Both rank on value over replacement, but
against different baselines: the workbook measures a player against the last
*rosterable* player, this engine against the last *startable* one. That is why the
workbook rates Josh Allen 13th overall at +148 while this engine has him at +63 —
a 10th-best quarterback still throws for 308 in your league, so Allen's raw total
overstates his real edge.

Each row shows both numbers side by side (`sheet VORP` vs `ours`), plus ADP and
projected points, so you can audit the disagreement rather than take it on trust.
When both boards land on the same player, the row is flagged **SAME PICK** — that
is your strongest signal of the night.

The workbook board covers its top 300 players; deeper fliers show only on the
engine's side.

## Things it knows that are easy to forget

- Your league starts **zero** kickers and **zero** defenses, so neither ever
  appears on the board.
- You have **13 picks, not 15** — rounds 2 and 12 are gone to James Cook and Kyle
  Monangai.
- All 20 keepers are off the board from pick 1, and each keeper's designated round
  is auto-skipped in the pick counter.

## Files

| File | What it is |
|---|---|
| `server.py` | local web server, ESPN sync, draft state |
| `engine.py` | the recommendation logic |
| `data.json` | players, projections, prices, keepers, your pick schedule |
| `index.html` | the interface |
| `config.json` | your ESPN credentials (created when you save Settings) |
