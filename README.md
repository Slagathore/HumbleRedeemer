# Humble Steam Key Redeemer

If this tool rescued your key library, consider [buying me Half a cup of coffee](https://ko-fi.com/sparklemuffin). Tarriffs amirite. 🥲

## Web App (recommended)

Grab a packaged build from [Releases](../../releases) (Windows/macOS/Linux — unzip and run, no Python needed), or run from source with `run_app.bat` / `python app.py`. A dashboard opens at http://127.0.0.1:5757.

Use whatever browser you like for the dashboard — Chrome, Firefox, Opera GX, anything. The Humble/GOG automation runs in a separate hidden browser, which uses Chrome or Firefox; if neither is installed, Selenium usually downloads a private copy of Chrome automatically.

Features:

- **Metrics dashboard** — key pipeline, per-service breakdown, redemption activity over time
- **Library browser** — search/filter every Humble key, see its reveal/match/redeem state
- **One-click actions** — Sync Humble, Sync Steam, Match ownership, Reveal, Redeem, or a Full Auto run
- **3-tier ownership matching** — Steam AppID, normalized exact name (handles ™/®, case, punctuation), then conservative fuzzy matching whose "likely owned" hits you confirm in the UI instead of a skipped.txt file
- **Reveal support** — unrevealed keys are revealed on Humble automatically during redemption (toggle in Settings)
- **Settings page** — Steam Web API key/SteamID64, match threshold, delays, rate-limit wait
- **SQLite state** (`redeemer.db`) — imports all history from the old CSVs on first run; "Reset all errored" puts keys the old pipeline gave up on back into the queue
- **Humble Choice auto-claim** — walks every Choice/Monthly month you've ever had and claims every unclaimed game (runs automatically as the first step of Full Auto)
- **Steam license verification** — cross-references Steam's account licenses page ("Activated as CD Key") against your redeemed keys, so each redeemed key gets a verified-in-Steam badge
- **Giveaway page** — spare keys for games you already own (never-consumed keys, including ones Steam refused with "already owned"), with click-to-copy, email drafts, Reddit-post and names-only exports, key expiration dates, gift-link creation for unrevealed spares, given-away tracking, and a slow-burn job that resolves ambiguous "verify first" spares against Steam
- **Attention page** — everything that can't be auto-redeemed: other-store keys with where-to-redeem links, dead/exhausted keys with the Steam error decoded, expired keys, gift links, and the not-actually-a-game oddities (trials, coupons, playtests)
- **GOG integration** — sign in via a browser window on your PC, library sync with ownership skip, and browser-driven redemption at gog.com/redeem (GOG sometimes interjects a captcha; those keys stay listed for manual entry)

Sign in to Humble, Steam, and GOG from the header chips (2FA/Steam Guard supported, including approve-in-app). Sessions persist in `.humblecookies` / `.steamcookies` / `.gogcookies` and are restored automatically on every launch — you only sign in again when a service expires the session.

## Disclaimer

This is an **unofficial** tool, not affiliated with or endorsed by Humble Bundle, Valve/Steam, or GOG. It automates actions on your own accounts (fetching your library, revealing and activating your keys); automated account access may conflict with those services' terms of service. Use at your own risk — the software is provided **as is**, with no warranty of any kind, and you are solely responsible for your accounts and keys. Based on [FailSpy's humble-steam-key-redeemer](https://github.com/FailSpy/humble-steam-key-redeemer).

## Privacy

**Everything is 100% local.** This app has no server, no accounts, no telemetry, and collects no data. Your logins are stored as session cookies in local files (`.humblecookies`, `.steamcookies`, `.gogcookies`), and your keys/history live in a local SQLite database (`redeemer.db`) next to the app. The only network connections it ever makes are to Humble Bundle, Steam, and GOG — acting as you, on your machine, for you. Delete the cookie files to sign out; delete `redeemer.db` to wipe all history.

## Original CLI script (legacy)

The console script the web app grew out of — kept for reference and standalone use (`run_redeemer.bat`). The web app does everything it does; note that its Steam ownership check needs your own `STEAM_API_KEY` / `STEAM_ID_64` filled in at the top of `humblesteamkeysredeemer.py` (the web app keeps these in its Settings instead).

Python utility script to extract Humble keys, and redeem them on Steam automagically by detecting when a game is already owned on Steam.

This is primarily designed to be a set-it-and-forget-it tool that maximizes successful entry of keys into Steam, assuring that no Steam game goes unredeemed.

This script will login to both Humble and Steam, automating the whole process. It's not perfect as I made this mostly for my own personal case and couldn't test all possibilities so YMMV. Feel free to send submit an issue if you do bump into issues.

Any revealing and redeeming the script does will output to spreadsheet files based on their actions for you to easily review what actions it took and whether it redeemed, skipped, or failed on specific keys.

## Modes
### Auto-Redeem Mode (Steam)
Find Steam games from Humble that are unowned by your Steam user, and ONLY of those that are unowned, redeem on Steam revealed keys (This EXCLUDES non-Steam keys and unclaimed Humble Choice games)

If you choose to reveal keys in this mode, it will only reveal keys that it goes to redeem (ignoring those that are detected as already owned)
### Export Mode
Find all games from Humble, optionally revealing all unrevealed keys, and output them to a CSV (comes with an optional Steam ownership column). 

This is great if you want a manual review of what games are in your keys list that you may have missed.
### Humble Chooser Mode
For those subscribed to Humble Choice, this mode will find any Humble Monthly/Choice that has unclaimed choices, and will let you select, reveal, and optionally autoredeem on Steam the keys you select

#
### Notes

To remove an already added account, delete the associated `.(humble|steam)cookies` file.

### Dependencies

Running from source requires Python 3.10+ (packaged builds need nothing)

- `flask`: [Flask](https://flask.palletsprojects.com/) (web app)
- `steam`: [FailSpy's fork of ValvePython/steam](https://github.com/FailSpy/steam-py-lib)  
- `fuzzywuzzy`: [seatgeek/fuzzywuzzy](https://github.com/seatgeek/fuzzywuzzy)  
- `requests`: [requests](https://requests.readthedocs.io/en/master/)
- `selenium`: [selenium](https://www.selenium.dev/)
- `pwinput`: [pwinput](https://github.com/asweigart/pwinput)
- `python-Levenshtein`: [ztane/python-Levenshtein](https://github.com/ztane/python-Levenshtein) **OPTIONAL**  

Install the required dependencies with
```
pip install -r requirements.txt
```
If you want to install `python-Levenshtein`:
```
pip install python-Levenshtein
```
