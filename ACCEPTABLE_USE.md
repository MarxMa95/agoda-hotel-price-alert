# Acceptable Use

This project is intended for low-volume, personal monitoring of Agoda hotel prices.

You may use it to:
- monitor your own shortlist of hotel rooms
- run local manual checks
- receive personal notifications

You may not use it to:
- run large-scale scraping
- build a public or multi-tenant scraping service
- bypass access controls, abuse login flows, or evade platform protections
- use other people's session data or credentials
- overwhelm Agoda with high-frequency automated traffic

Safety constraints in this repository:
- localhost-only server binding
- Agoda-only URL restriction in the OSS edition
- minimum polling interval
- active watcher cap
- runtime session data excluded from Git
