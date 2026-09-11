# Changelog

All notable changes to the **fantasybot** project for rival tracking, transfer accounting, and multi-season player scouting.

---

## [Unreleased] - 2026-08-21

### Added

#### 1. API Client Enhancements (`fantasybot/api.py`)
- **`all_players()`**: Fetches master catalog of all 700+ competition players with historical metrics (`lastSeasonPoints`, `marketValue`, `playerStatus`, etc.).
- **`league_teams(league_id)`**: Fetches live roster, market valuations, manager points, positions, and squad clause data for all league participants.
- **`league_activity(league_id, fetch_all=True)`**: Automatically iterates through paginated endpoints (`/leagues/{id}/activity/{idx}`) to retrieve the complete chronological transfer history from day 1 of the league.

#### 2. Persistent Transaction & Squad State (`fantasybot/state.py`)
- **Cumulative Activity Storage (`.state/activity_history.json`)**: Merges and de-duplicates transfer events across sessions so that historical transactions are never lost even after API circular buffer rollovers.
- **Rival Squad Snapshots (`.state/rivals_snapshot.json`)**: Tracks squad rosters and detects clause increases (clause protection) between runs.
- **Players Metadata Cache (`.state/players_cache.json`)**: Caches player name, position, and valuations locally to minimize API traffic.
- Added state management functions: `record_activity()`, `load_activity_history()`, `snapshot_rivals()`, `save_rivals_snapshot()`, `load_rivals_snapshot()`, `load_players_cache()`, `save_players_cache()`, and `diff_rival_clauses()`.

#### 3. Rival Strategy & Accounting Module (`fantasybot/strategy/rivals.py`)
- **`parse_activity()`**: Aggregates market purchases (`Type 31`), market sales (`Type 33`), manager-to-manager buyouts (`Type 1`), and matchday point rewards (`Type 6`).
- **`analyze_player_acquisitions()`**: Cross-references squad players with historical purchases to identify:
  - Exact purchase price (`BOUGHT AT`) and buy date.
  - Capital gain/loss (`PROFIT / LOSS`) in currency and as a percentage revaluation.
  - Identification of players from the initial assigned squad (`(Initial)`).
- **`analyze_squad_clauses()`**: Calculates total squad clause valuation, highest clause, and top protected player (clause minus market value).
- **`analyze_rivals()`**: Combines squad valuations, persistent transaction history, and pure baseline accounting to estimate available liquid cash for all league rivals. Auto-calibrates the league's starting budget from our own known balance (rather than assuming a constant), so every rival's cash estimate is anchored to real data.

#### 4. Trading History & P&L Module (`fantasybot/strategy/history.py`)
- **`compute_manager_trading_history()`**:
  - Matches buy and sell transactions (FIFO) by player to compute completed flips, holding duration in days, and return on equity (ROI %).
  - Tracks open purchased holdings with live unrealized capital gains/losses.
  - Tracks initial squad liquidations and total sales revenue.
- **`resolve_player_names()`**: Resolves player metadata from local cache and API.
- **`analyze_league_trading_history()`**: Produces league-wide speculation leaderboards sorted by total portfolio P&L.

#### 5. Multi-Season Player Scouting & Squad Intelligence (`fantasybot/strategy/scouting.py`)
- **`analyze_player_profile()`**: Evaluates multi-season points history (`lastSeasonPoints`), historical tier (🌟 *Top Star*, 🛡️ *Fixed Starter*, 🔄 *Rotation*), scoring pace evolution vs last year, FutbolFantasy starting probability (0-95%), role shifts (e.g. was starter last year -> benched now), fitness & availability, and value-for-money (€/pt) ratio.
- **`analyze_team_squad()`**: Full squad audit evaluating total past season output, squad stars, fitness risk, and line-by-line recommendations.
- **`search_player_in_list()`**: Fast fuzzy and nickname search over master player lists.

#### 6. CLI Commands (`fantasybot/cli.py`)
- **`python -m fantasybot rivals [manager|rank]`**:
  - General league overview with position, team value, squad size, total purchases, total sales, net profit, estimated cash, and top protected players.
  - Detailed individual squad performance table (`PLAYER`, `POS`, `BOUGHT AT`, `CURRENT VALUE`, `PROFIT / LOSS`, `CLAUSE`, `PROTECTION`).
  - Reality check comparison on user's own account (`Real Cash vs Pure Estimated`).
- **`python -m fantasybot history [manager|rank]`**:
  - League-wide speculation and trading ROI leaderboard (`TOTAL P&L`, `REALIZED`, `UNREALIZED`, `FLIPS`, `WIN%`, `AVG ROI`).
  - Detailed trade log showing open holdings, completed flips with ROI %, and initial squad sales.
- **`python -m fantasybot scout <player>` & `python -m fantasybot scout --team`**:
  - Deep-dive player scouting card with historical points, starting probability, role shift warnings, and tactical verdict.
  - Full squad audit breakdown by position with historical points and risk evaluation.
- **Flexible search support**: Query by multi-word name without quotes (`rivals EPT Alfaro`), rank position (`rivals 1` or `#1`), manager/team ID (`rivals 867521`), or shortcut for own account (`rivals me`).
- **`--json` flags**: Structured JSON export for programmatic consumption (`rivals --json`, `history --json`).
- **`--initial-budget` flag**: Allows custom league starting budget overrides.

#### 7. LLM Agent Integration (`fantasybot/agent.py`)
- Included league rival financial data and clause increases into `review()` dictionary and CLI summary output.

#### 8. Unit Tests (`tests/test_rivals.py`, `tests/test_history.py`, `tests/test_scouting.py`)
- Added comprehensive unit tests covering activity parsing, clause protection analysis, rival accounting, trade ROI, and scouting analysis.
- All unit tests passing.

### Fixed & Security Hardening

- **CLI Watch Command Architecture (`fantasybot/cli.py`)**: Restored clean execution lifecycle for `cmd_watch` (Ctrl+C event loop, browser launcher, background daemon threads) and separated `cmd_scout` into dedicated function.
- **Activity Pagination & Network Safety (`fantasybot/api.py`)**: Capped activity scraping at 100 pages, verified list types, and re-raised exceptions on initial page load to prevent state corruption while gracefully handling network hiccups on subsequent pages.
- **Null Safety in Flips (`fantasybot/strategy/flip.py`)**: Added defensive `.get()` chaining on `playerTeam` and `sellerTeam` to prevent `NoneType` crashes during market scans.
- **Chronological FIFO Lot Matching (`fantasybot/strategy/history.py`)**: Rebuilt trade matching with an ordered open buy lot queue, ensuring accurate P&L calculation and properly distinguishing day 1 squad sales from realized flips.
- **Player Cache Hygiene (`fantasybot/strategy/history.py`)**: Fixed `None` check to avoid caching `"None"` keys, and prevented persistent disk storage of placeholder names on network failures.
- **Incremental Traffic Optimization (`fantasybot/strategy/rivals.py`)**: Switched to page 0 incremental polling for leagues with existing local history, substantially reducing HTTP request volume.
- **Atomic State Persistence (`fantasybot/state.py`)**: Implemented atomic file replacement (`.tmp` + `os.replace`) to guarantee data integrity across process interruptions and crashes.
