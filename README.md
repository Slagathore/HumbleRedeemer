# Humble Steam Key Redeemer

If this tool rescued your key library, consider [buying me Half a cup of coffee](https://ko-fi.com/sparklemuffin). Tarriffs amirite. 🥲

## Web App (recommended)

Run `run_app.bat` (or `python app.py`) — a dashboard opens at http://127.0.0.1:5757 with:

- **Metrics dashboard** — key pipeline, per-service breakdown, redemption activity over time
- **Library browser** — search/filter every Humble key, see its reveal/match/redeem state
- **One-click actions** — Sync Humble, Sync Steam, Match ownership, Reveal, Redeem, or a Full Auto run
- **3-tier ownership matching** — Steam AppID, normalized exact name (handles ™/®, case, punctuation), then conservative fuzzy matching whose "likely owned" hits you confirm in the UI instead of a skipped.txt file
- **Reveal support** — unrevealed keys are revealed on Humble automatically during redemption (toggle in Settings)
- **Settings page** — Steam Web API key/SteamID64, match threshold, delays, rate-limit wait
- **SQLite state** (`redeemer.db`) — imports all history from the old CSVs on first run; "Reset all errored" puts keys the old pipeline gave up on back into the queue
- **Humble Choice auto-claim** — walks every Choice/Monthly month you've ever had and claims every unclaimed game (runs automatically as the first step of Full Auto)
- **Steam license verification** — cross-references Steam's account licenses page ("Activated as CD Key") against your redeemed keys, so each redeemed key gets a verified-in-Steam badge
- **Giveaway page** — spare keys for games you already own (never-consumed keys, including ones Steam refused with "already owned"), with copy/export, per-key gift-link creation for unrevealed spares, and given-away tracking

Sign in to Humble and Steam from the header chips (2FA/Steam Guard supported, including approve-in-app). Sessions persist in `.humblecookies` / `.steamcookies` and are restored automatically on every launch — you only sign in again when a service expires the session.

## Privacy

**Everything is 100% local.** This app has no server, no accounts, no telemetry, and collects no data. Your logins are stored as session cookies in local files (`.humblecookies`, `.steamcookies`, `.gogcookies`), and your keys/history live in a local SQLite database (`redeemer.db`) next to the app. The only network connections it ever makes are to Humble Bundle, Steam, and GOG — acting as you, on your machine, for you. Delete the cookie files to sign out; delete `redeemer.db` to wipe all history.

## Original CLI script

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

Requires Python version 3.6 or above

- `steam`: [ValvePython/steam](https://github.com/ValvePython/steam)  
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
