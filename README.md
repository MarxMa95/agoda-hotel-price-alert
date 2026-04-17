# Agoda Hotel Price Alert

![Platform](https://img.shields.io/badge/platform-macOS-black)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Local Only](https://img.shields.io/badge/server-localhost--only-success)
![Scope](https://img.shields.io/badge/scope-Agoda%20only-orange)
![License](https://img.shields.io/github/license/MarxMa95/agoda-hotel-price-alert)
![Release](https://img.shields.io/github/v/release/MarxMa95/agoda-hotel-price-alert)

A local-first Agoda hotel price watcher with dedicated sign-in, room-level matching, scheduled checks, and webhook alerts.

## Highlights

- Dedicated Agoda sign-in flow with local session persistence
- Room candidate preview and room-level matching
- Manual checks plus scheduled polling with local history
- Feishu and WeCom webhook notifications
- Local-only server binding for personal desktop usage
- Built-in public safety limits for the open-source edition

## Safety model

This repository is intentionally designed for low-volume personal use on a local machine.

Built-in safeguards:
- localhost-only server binding
- Agoda-only target URL restriction
- minimum polling interval of 5 minutes
- active watcher limit of 20
- runtime profiles, screenshots, logs, and database excluded from Git

Please respect Agoda's terms, rate limits, robots controls, and other access restrictions.
Do not use this project as a hosted scraping service, bulk-monitoring platform, or resale system.

See also:
- `ACCEPTABLE_USE.md`
- `SECURITY.md`
- `CHANGELOG.md`
- `CONTRIBUTING.md`
- `CODE_OF_CONDUCT.md`

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
python3 -m pip install -r requirements.txt
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

Run the public safety check before pushing changes:

```bash
./scripts/prepublish_check.sh
```

## Issue policy

- Use bug reports for reproducible defects in the open-source codebase
- Use feature requests for product-level suggestions
- Do not post live credentials, cookies, personal booking data, or private webhook URLs
- Support is best-effort and focused on the public open-source edition

## Community docs

- Contribution guide: `CONTRIBUTING.md`
- Community expectations: `CODE_OF_CONDUCT.md`

## License

This project is released under the MIT License. See `LICENSE`.
