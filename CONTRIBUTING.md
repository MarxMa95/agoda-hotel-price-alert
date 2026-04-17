# Contributing

Thanks for your interest in improving this project.

This repository is a local-first, low-volume Agoda price watcher intended for personal desktop use. Contributions are welcome as long as they stay within that scope and do not weaken the project's safety boundaries.

## Contribution principles

Please keep changes aligned with these principles:

- Local-first operation only
- Low-volume, personal-use workflows
- No hosted scraping platform features
- No bulk account management or credential automation
- No bypassing access controls, anti-abuse protections, or rate limits
- No committing runtime data, cookies, session files, or private webhook URLs

## Good contribution areas

Examples of changes that are a good fit:

- Bug fixes in the local UI or backend
- Stability improvements for local session handling
- Safer room matching or price extraction logic
- Documentation improvements
- Better error messages and recovery flows
- Safer defaults for polling, storage, or notifications
- macOS local launcher improvements

## Out of scope

Please do not propose or submit changes for:

- High-frequency polling
- Large-scale scraping workflows
- Multi-tenant or hosted deployment support
- Proxy rotation, account farming, or anti-detection tooling
- Features that remove or weaken the built-in safety limits

## Development setup

```bash
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
python3 app.py
```

Open `http://127.0.0.1:8767` in your browser.

## Before opening a pull request

Please make sure that:

- The change stays within the project's local-first scope
- No runtime data or secrets are included
- `./scripts/prepublish_check.sh` passes
- Documentation is updated if behavior changed
- The change is small, focused, and easy to review

## Pull request checklist

When submitting a PR, include:

- A short summary of the problem
- A clear explanation of the proposed solution
- Any tradeoffs or limitations
- Screenshots if the UI changed
- Notes about safety or compliance implications, if relevant

## Security and sensitive reports

For sensitive issues, please do not open a public issue with secrets or exploit details.

See:
- `SECURITY.md`
- `ACCEPTABLE_USE.md`
