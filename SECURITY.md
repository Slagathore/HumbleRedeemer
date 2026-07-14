# Security

## Reporting a problem

Open a private security advisory on this repo (the Security tab, "Report a vulnerability") at
https://github.com/Slagathore/HumbleRedeemer, or email charcham7@gmail.com. This is a solo
personal project, so response time depends on when I see it, but I do read both.

## What this app is

A single user desktop app. A local Flask dashboard plus a hidden Selenium browser that signs
in to Humble Bundle, Steam, and GOG as you and automates actions on your own accounts: finding
your keys, claiming Choice games, revealing keys, and activating them. There is no account
system, no backend service, and nobody but you runs this app against your accounts.

## What runs where, and what leaves the machine

- **The dashboard.** Binds to `127.0.0.1` only (`app.py`, port 5757 by default, override with
  `APP_PORT`), never `0.0.0.0`. It also checks the request's Host header and, on POSTs, the
  Origin header, so a page open in a normal browser tab can't drive it and it isn't reachable
  from your LAN.
- **The automation browser.** A hidden Chrome or Firefox instance (Selenium) that loads the
  real gog.com, humblebundle.com, and steampowered.com pages and drives them as you. Your
  Humble/Steam/GOG password is typed into those sites' own login pages, never handled or seen
  by this app.
- **Network calls.** Everything goes to Humble Bundle, Steam, or GOG, acting as you, on your
  machine, for you, plus a read only check of this GitHub repo at launch for update
  notifications. Set `APP_NO_UPDATE_CHECK=1` to turn that off. Nothing else phones home. No
  telemetry, no analytics, no third party server in between.
- **Updates.** The installed Windows build can download and run a new installer, which means it
  can execute code it fetched off the internet. That only happens after both of these pass, in
  `redeemer/update_install.py`: the file's SHA256 matches the `SHA256SUMS.txt` published with
  the release, and Windows itself reports the file's Authenticode signature as Valid with the
  project's signing certificate on it (`CN=Charles Chambers`). A download that is unsigned,
  tampered with, or signed by anyone else is deleted without being run, and the installer is
  never launched from any state other than "verified". There is no setting that skips this, and
  transport is HTTPS to GitHub throughout. If a release has no checksums file, the signature
  check still has to pass on its own, and the app says that is what happened. Source and
  portable runs never download or execute anything, they are told to update by hand.

## Secrets and local storage

- These files live in `%LOCALAPPDATA%\HumbleRedeemer` for the installed build, and next to the
  app for the portable exe and source runs (`redeemer/paths.py`, override with
  `HUMBLEREDEEMER_DATA`). Keeping them out of the program folder is what lets an update replace
  the app without ever writing over your keys, history or sessions.
- `.humblecookies`, `.steamcookies`, and `.gogcookies` hold live session cookies for each
  store. They're read and written as JSON (gitignored), not unpickled, so a
  corrupted or planted cookie file can't run code when the app tries to restore a session with
  it, it just fails to load and you sign in again.
- `redeemer.db` (SQLite, also gitignored) holds your key inventory: titles, the actual key
  values once you reveal them, redemption status, and an event log.
- None of the above is encrypted at rest. That's a known tradeoff of a single user desktop
  tool, see Known limitations. Delete the cookie files to sign out of a store; delete
  `redeemer.db` to wipe all history.

## Local threat model

- Someone with a login on the host machine, or anything that can run code as your OS user, is
  fully trusted by design. They can read the cookie files and hijack your live Humble/Steam/GOG
  session, and can read `redeemer.db` including any key values you've revealed. That's the same
  trust boundary as any other desktop app that isn't sandboxed from its own user. This isn't
  built to resist another account or process on a machine you don't fully control, on a
  personal, single user computer that's the accepted model.
- Someone on your LAN can't reach the dashboard at all under default settings, it only listens
  on `127.0.0.1`.
- The automation browser only ever visits the real Humble, Steam, and GOG pages. Nothing gets
  injected into those sessions beyond what the app's own redemption flow does.

## Known limitations

- No encryption at rest for the cookie files or the database. Plaintext on disk, same as most
  single user desktop tools.
- macOS builds are unsigned, Gatekeeper says so on first open, right click then Open gets past
  it. Windows builds are signed before each release (see `SIGNING.md`); CI based signing is
  wired into `release.yml` but not enabled yet, it needs Azure secrets that aren't configured.
- This automates real account actions against Humble, Steam, and GOG's own web pages, not
  official partner APIs. That can run up against those services' terms of service, see the
  README's Disclaimer section.
- The Steam login library (`steam-py-lib`, a third party fork) is pinned to a specific commit
  rather than tracking its upstream branch automatically, so it won't pick up a fix there until
  someone deliberately bumps the pin.
- No professional security audit has been done on this app. It's a personal project I use
  myself and also ship publicly. The above is an honest description of how it behaves, not a
  guarantee.
