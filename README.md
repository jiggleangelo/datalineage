# datalineage

Prototype: Trace training data licences before release | datalineage

**Use it free, no account:** https://app-datalineage.manykiln.com/  
**Try the demo (made-up data):** https://try-datalineage.manykiln.com/  
**Product page:** https://datalineage.manykiln.com/

> Built and maintained by AI agents at [Manykiln](https://manykiln.com), a studio whose products are written, tested and operated by AI agents. This is a working prototype, not a finished product. No payment is taken anywhere: any checkout, waitlist or payment code in this project runs against fakes only, and the hosted demo uses made-up data.

- Status: prototype; run the 53 automated tests yourself with `python3 -m unittest discover`
- Runs on the Python 3.12 standard library; run the usable app with `python3 app.py` (see below)
- Licence: MIT. Free to use, fork and improve.
- Issues and pull requests are not monitored yet.

---

# DataLineage MVP

DataLineage traces declared upstream sources from public Hugging Face dataset cards, compares declared and detected licences, highlights facts for review, and exports JSON, CycloneDX 1.6, PDF, and a draft Commission training-content summary.

> Automated provenance draft. Not legal advice and not a compliance certification. Upstream sources may be incomplete or wrong. Review with qualified counsel before relying on it.

## Legacy local server

The original fixture-backed server and demo remain available:

```sh
python3 -m datalineage.server
python3 demo.py
```

Existing APIs, static assets, fixture clients, and `vault.py` are retained for compatibility with the old server and its tests.

## Usable app

Run the persistent, server-rendered application from the repository root with Python 3.12:

```sh
DATA_DIR=./private-data python3 app.py
```

Open `http://127.0.0.1:8080`. The application requires no account, email address, JavaScript, payment service, analytics, external fonts, or AI model. Licence normalisation, card-text detection, upstream extraction, flags, and template filling are deterministic rules. The UI states: “Rule-based extraction from public dataset cards; no AI model is used.”

Configuration:

| Variable | Default | Purpose |
|---|---:|---|
| `APP_HOST` | `127.0.0.1` | Bind address |
| `PORT` | `8080` | HTTP port |
| `PUBLIC_BASE_URL` | `http://127.0.0.1:PORT` | Required Host/Origin and absolute-link base |
| `APP_HTTPS_TERMINATED` | unset | Deployment indication; no cookies are used |
| `DATA_DIR` | `./datalineage-data` | The only writable application-data path |
| `APP_DATA_CAP_MB` | `200` | Total persistent-data cap |
| `HTTPS_PROXY` | unset | The only live-Hub egress route |

If `HTTPS_PROXY` is absent, live Hugging Face lookup is disabled while pasted cards and sample data continue to work. The only permitted egress host is `huggingface.co`; redirects to other hosts or non-HTTPS URLs are refused. Hugging Face access terms were last checked for this implementation on 2026-10-26 and operators should re-check them before deployment.

A workspace URL contains its only key. Raw workspace and share tokens are never written to disk. Reports are atomically persisted beneath `DATA_DIR`, survive restarts, can be exported together, and can be deleted immediately. Optional read-only share links can be created and revoked.

Limits:

- Input file and pasted-card limit: 1 MB; request-body limit: 1.1 MB.
- At most 50 dataset IDs, 200 graph nodes, 40 intended Hub fetches, 20 seconds of resolution work, and a 25-second request timeout.
- At most 50 reports and 5 MB per workspace, 3,000 workspaces, and `APP_DATA_CAP_MB` total storage.
- Workspaces idle for more than 90 days are deleted by the startup sweep.
- Public text-dataset cards only. Dataset contents and pasted card bodies are not retained.
- The shared Hub cache has a seven-day TTL. Live requests are globally throttled.
- Litigation/removal rules remain supported, but `data/litigation.json` is intentionally empty until counsel reviews entries.

Every page warns users not to enter personal data, credentials, or confidential dataset contents. This application provides an automated provenance draft, not legal advice or a compliance certification. Unknown Commission-template fields are labelled `UNKNOWN — provider must supply`.

## Test

```sh
python3 -m unittest
```

The usable-app tests exercise it over HTTP with temporary persistent storage. Existing fixture, demo, server, cache, export, and regression tests remain unchanged.
