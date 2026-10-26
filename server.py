"""ESPN Fantasy Basketball connector for Claude (read-only).

A small MCP server that reads one ESPN fantasy basketball league and exposes it
to Claude as tools: standings, rosters, live matchup scores, recent adds/drops,
free agents and player stats. It cannot make any changes in ESPN.

Configuration (set these as environment variables on your host, never in code):
  LEAGUE_ID      ESPN league ID, e.g. 455752333
  SEASON         ESPN season year; the 2026-27 season is 2027
  CONNECTOR_KEY  a long random secret; it becomes part of the connector URL
  ESPN_S2        (private leagues only) the espn_s2 cookie from your browser
  SWID           (private leagues only) the SWID cookie, including the { }
  PORT           set automatically by most hosts
"""

import os
import time
from datetime import datetime, timezone

from espn_api.basketball import League
from mcp.server.fastmcp import FastMCP

LEAGUE_ID = int(os.environ["LEAGUE_ID"])
SEASON = int(os.environ.get("SEASON", "2027"))
KEY = os.environ["CONNECTOR_KEY"]
ESPN_S2 = os.environ.get("ESPN_S2") or None
SWID = os.environ.get("SWID") or None

CATS = ["FG%", "FT%", "3PM", "REB", "AST", "STL", "BLK", "TO", "PTS"]
STAT_KEYS = ["GP", "MIN", "PTS", "REB", "AST", "STL", "BLK", "TO", "3PM",
             "FGM", "FGA", "FG%", "FTM", "FTA", "FT%"]

mcp = FastMCP(
    "ESPN Fantasy League",
    host="0.0.0.0",
    port=int(os.environ.get("PORT", "8000")),
    stateless_http=True,
    json_response=True,
    streamable_http_path=f"/{KEY}/mcp",
)

_cache = {"league": None, "at": 0.0}


@mcp.custom_route("/health", methods=["GET"])
async def health(request):
    """Public keep-awake endpoint for free uptime pingers. Reveals nothing."""
    from starlette.responses import PlainTextResponse
    return PlainTextResponse("ok")


def league(max_age: int = 120) -> League:
    """Load the league, reusing it for up to max_age seconds."""
    if _cache["league"] is None or time.time() - _cache["at"] > max_age:
        _cache["league"] = League(league_id=LEAGUE_ID, year=SEASON,
                                  espn_s2=ESPN_S2, swid=SWID)
        _cache["at"] = time.time()
    return _cache["league"]


def _round(v):
    return round(v, 3) if isinstance(v, float) else v


def _stats(player, split):
    s = player.stats.get(split) or {}
    avg = s.get("avg") or {}
    tot = s.get("total") or {}
    out = {k: _round(avg.get(k)) for k in STAT_KEYS if avg.get(k) is not None}
    if "GP" in tot:
        out["GP"] = tot["GP"]
    return out or None


def _player(p, detail: bool = False):
    d = {
        "name": p.name,
        "nba_team": p.proTeam,
        "positions": [s for s in p.eligibleSlots
                      if s in ("PG", "SG", "SF", "PF", "C")],
        "injury": p.injuryStatus,
        "lineup_slot": p.lineupSlot,
    }
    if p.expected_return_date:
        d["expected_return"] = p.expected_return_date.isoformat()
    season = _stats(p, f"{SEASON}_total")
    if season:
        d["season_avg"] = season
    if detail:
        for label, key in (("last_7", f"{SEASON}_last_7"),
                           ("last_15", f"{SEASON}_last_15"),
                           ("last_30", f"{SEASON}_last_30")):
            st = _stats(p, key)
            if st and st != season:
                d[label] = st
    return d


def _team_by_name(name: str):
    lg = league()
    name_l = name.lower().strip()
    for t in lg.teams:
        if name_l in t.team_name.lower() or name_l == t.team_abbrev.lower():
            return t
    raise ValueError(f"No team matching '{name}'. Teams: "
                     + ", ".join(t.team_name for t in lg.teams))


@mcp.tool()
def league_overview() -> dict:
    """Standings, records, divisions and season adds used for every team, plus the current matchup period."""
    lg = league(max_age=60)
    teams = sorted(lg.teams, key=lambda t: (t.standing or 99))
    return {
        "league_id": LEAGUE_ID,
        "season": SEASON,
        "current_matchup_period": lg.currentMatchupPeriod,
        "current_scoring_period": lg.scoringPeriodId,
        "teams": [{
            "name": t.team_name, "abbrev": t.team_abbrev,
            "division": t.division_name,
            "record": f"{t.wins}-{t.losses}-{t.ties}",
            "standing": t.standing,
            "season_adds": t.acquisitions, "drops": t.drops, "trades": t.trades,
        } for t in teams],
    }


@mcp.tool()
def team_roster(team: str, detail: bool = False) -> dict:
    """Roster for one team (name or abbreviation, partial match is fine): lineup slot, positions, injury status and season averages. Set detail=true to add last 7/15/30-day averages."""
    t = _team_by_name(team)
    return {"team": t.team_name,
            "players": [_player(p, detail) for p in t.roster]}


@mcp.tool()
def all_rosters() -> dict:
    """Every team's roster (names, NBA team, positions, injury status, lineup slot). Use to see who is owned."""
    lg = league()
    return {t.team_name: [
        {"name": p.name, "nba_team": p.proTeam, "injury": p.injuryStatus,
         "lineup_slot": p.lineupSlot} for p in t.roster] for t in lg.teams}


@mcp.tool()
def matchup_scores(matchup_period: int | None = None) -> dict:
    """Live (or past) head-to-head category scores for every matchup: each team's totals in all 9 categories and the category win-loss-tie score. Defaults to the current matchup period."""
    lg = league(max_age=60)
    period = matchup_period or lg.currentMatchupPeriod
    names = {t.team_id: t.team_name for t in lg.teams}

    def team_name(t):
        if hasattr(t, "team_name"):
            return t.team_name
        return names.get(t, "BYE")

    out = []
    for m in lg.scoreboard(matchupPeriod=period):
        def side(prefix):
            cats = getattr(m, f"{prefix}_team_cats", None) or {}
            res = [c.get("result") for c in cats.values()]
            return {
                "team": team_name(getattr(m, f"{prefix}_team")),
                "score": f"{res.count('WIN')}-{res.count('LOSS')}-{res.count('TIE')}",
                "categories": {c: {"value": _round(cats[c]["score"]),
                                   "result": cats[c].get("result")}
                               for c in CATS if c in cats},
            }
        out.append({"home": side("home"), "away": side("away")})
    return {"matchup_period": period, "matchups": out}


@mcp.tool()
def recent_activity(size: int = 30) -> dict:
    """Most recent league transactions (adds, drops, waiver claims, trades), newest first, with NZ-agnostic UTC timestamps."""
    lg = league(max_age=60)
    items = []
    for a in lg.recent_activity(size=size):
        when = datetime.fromtimestamp(a.date / 1000, tz=timezone.utc).isoformat()
        for act in a.actions:
            team, action, player = act[0], act[1], act[2]
            items.append({
                "utc": when,
                "team": getattr(team, "team_name", str(team)),
                "action": action,
                "player": getattr(player, "name", str(player)),
            })
    return {"activity": items}


@mcp.tool()
def free_agents(position: str | None = None, size: int = 40) -> dict:
    """Best available free agents (ESPN ranking), optionally filtered by position (PG, SG, SF, PF, C, G, F). Includes injury status and season plus last-7-day averages."""
    lg = league()
    players = lg.free_agents(size=size, position=position)
    return {"free_agents": [_player(p, detail=True) for p in players]}


@mcp.tool()
def player_stats(name: str) -> dict:
    """Detailed info for one player: NBA team, injury, expected return, season and last 7/15/30-day averages, recent news headlines and which fantasy team owns him."""
    lg = league()
    p = lg.player_info(name=name, include_news=True)
    if not p:
        raise ValueError(f"No player found for '{name}'")
    d = _player(p, detail=True)
    owner = next((t.team_name for t in lg.teams
                  if any(r.playerId == p.playerId for r in t.roster)), None)
    d["fantasy_owner"] = owner or "Free agent"
    if isinstance(p.news, list):
        d["news"] = [{"published": n.get("published"), "headline": n.get("headline")}
                     for n in p.news[:5]]
    return d


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
