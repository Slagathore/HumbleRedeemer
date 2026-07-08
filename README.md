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

This is an **unofficial** tool, not affiliated with or endorsed by Humble Bundle, Valve/Steam, or GOG. It automates actions on your own accounts (fetching your library, revealing and activating your keys); automated account access may conflict with those services' terms of service. Use at your own risk — the software is provided **as is**, with no warranty of any kind, and you are solely responsible for your accounts and keys. Originally inspired by [FailSpy's humble-steam-key-redeemer](https://github.com/FailSpy/humble-steam-key-redeemer), since fully rewritten.

## Privacy

**Everything is 100% local.** This app has no server, no accounts, no telemetry, and collects no data. Your logins are stored as session cookies in local files (`.humblecookies`, `.steamcookies`, `.gogcookies`), and your keys/history live in a local SQLite database (`redeemer.db`) next to the app. The only network connections it ever makes are to Humble Bundle, Steam, and GOG — acting as you, on your machine, for you. Delete the cookie files to sign out; delete `redeemer.db` to wipe all history.

## Running from source

Packaged builds need nothing installed. From source, you need Python 3.10+:

```
pip install -r requirements.txt
python app.py
```

Optional: `pip install python-Levenshtein` makes the fuzzy matching faster.
