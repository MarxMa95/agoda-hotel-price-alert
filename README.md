# Agoda Hotel Price Alert

A local web tool for monitoring Agoda hotel room prices.

## What it does

- Create Agoda hotel price watchers
- Keep a dedicated Agoda login session
- Preview and select room types
- Run manual checks and scheduled polling
- Send Feishu or WeCom webhook notifications
- Show local price history charts

## Project structure

- `app.py` — backend server, scraper, scheduler, notification logic
- `templates/index.html` — frontend page and interaction logic
- `static/styles.css` — frontend styles
- `scripts/` — launchd helpers and local utility scripts
- `session_profiles/agoda` — local Agoda browser profile (runtime only, not committed)
- `data.db` — local watcher database (runtime only, not committed)

## Requirements

- macOS
- Python 3.11+
- `playwright` Python package
- A Chromium/Chrome browser, or Playwright-installed Chromium

## Install

```bash
python3 -m pip install playwright
python3 -m playwright install chromium
```

## Run locally

```bash
python3 app.py
```

Open: `http://127.0.0.1:8767`

## Typical workflow

1. Open an Agoda hotel page with dates and guest count selected
2. Copy the page URL into the tool
3. Open the dedicated Agoda login flow
4. Finish login in the dedicated browser window
5. Confirm that login is completed in the web UI
6. Preview room candidates
7. Save the watcher and run a manual check

## Notes

- This project stores login state locally under `session_profiles/`
- Runtime data such as database, logs, screenshots, and browser profiles are excluded by `.gitignore`
- The launcher scripts are designed for local macOS usage and derive paths from the script location

## Local command wrappers

For local macOS usage, the repo also includes these `.command` launchers:

- `launch_agoda_latest.command`
- `launch_latest.command`
- `install_launchd.command`
- `stop_launchd.command`
- `show_status.command`
- `open_web.command`
- `run_local.command`
- `run_doctor.command`
- `refresh_desktop_shortcuts.command`

## Responsible Use

This open-source edition is designed for low-volume personal use on a local machine.

Built-in safeguards:
- localhost-only server binding
- Agoda-only URL restriction
- minimum polling interval of 5 minutes
- active watcher limit of 20
- runtime session and login data excluded from Git

Please use it responsibly and respect Agoda's terms, rate limits, and access controls.
Do not use this repository as a public scraping platform or a high-volume monitoring service.

See also:
- `ACCEPTABLE_USE.md`
- `SECURITY.md`

## Launchd helpers

```bash
./scripts/install_launchd.sh
./scripts/status_launchd.sh
./scripts/uninstall_launchd.sh
```

## Before publishing

Make sure you do **not** commit:

- `session_profiles/`
- `data.db`
- `logs/`
- `debug_screens/`

## License

This project is released under the MIT License. See `LICENSE`.

## Pre-publish check

Run before pushing public changes:

```bash
./scripts/prepublish_check.sh
```
