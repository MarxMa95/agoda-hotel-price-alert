# Changelog

All notable changes to this project are documented in this file.

The format is based on Keep a Changelog, and this project follows Semantic Versioning in a lightweight, practical form.

## [0.1.0] - 2026-04-17

### Added
- Initial public release of the Agoda-only open-source edition
- Dedicated Agoda sign-in flow with local session persistence
- Local watcher management, manual checks, scheduled polling, and price history UI
- Feishu and WeCom webhook notification support
- macOS launcher scripts and launchd helper scripts
- Public safety guardrails for the open-source edition

### Security
- Added `ACCEPTABLE_USE.md` and `SECURITY.md`
- Restricted target URLs to Agoda hotel pages
- Enforced localhost-only server binding
- Enforced a minimum polling interval of 5 minutes
- Enforced a maximum of 20 active watchers
- Excluded runtime session, logs, screenshots, and local database files from Git tracking

### Tooling
- Added pre-publish validation via `scripts/prepublish_check.sh`
