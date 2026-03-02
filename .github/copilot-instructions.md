# Copilot Instructions — cgroups v2 Compatibility Checker

## Project Overview
This is an **OpenShift cgroups v2 Compatibility Checker** — a Flask web application that scans OpenShift clusters for container images that may have cgroups v2 compatibility issues before upgrading to OCP 4.19.

## Tech Stack
- **Backend**: Python 3.11, Flask 3.x, Gunicorn
- **Frontend**: PatternFly 6 (dark theme), vanilla JavaScript (no frameworks)
- **Container**: Podman, UBI 9 base image, skopeo for image inspection
- **Platform**: Red Hat OpenShift (Kubernetes), deployed via `oc` CLI
- **Registry**: Quay.io (`quay.io/chiaretto/cgroups-v2-checker`)

## Architecture
- `app/app.py` — Flask application factory
- `app/routes.py` — Web routes (HTML pages, CSV download)
- `app/api.py` — REST API endpoints (`/api/scan`, `/api/reports`)
- `app/scanner.py` — Core scan engine (Kubernetes API + skopeo)
- `app/config.py` — Configuration from environment variables
- `app/templates/` — Jinja2 templates extending `base.html`
- `app/static/` — CSS (PatternFly 6 dark theme), JS (vanilla)
- `openshift/` — Kubernetes/OpenShift manifests (Deployment, Service, Route, RBAC)
- `setup.sh` — Build, push, and deploy script (Podman + oc CLI)

## Coding Standards
- **Python**: Follow PEP 8, type hints where practical, docstrings on public functions
- **JavaScript**: Vanilla JS only (no jQuery, no React). Use `var` for compatibility. PatternFly 6 CSS classes for all UI components
- **HTML/CSS**: PatternFly 6 component classes (`pf-v6-c-*`), OpenShift Console-style dark theme
- **Templates**: Jinja2, always extend `base.html`
- **API**: RESTful JSON under `/api/` prefix, scan runs async in background thread

## Key Patterns
- Scanner uses Kubernetes Python client with **paginated** pod listing (`_K8S_PAGE_SIZE = 500`)
- Image metadata inspected via **skopeo** (no layer download), results cached in-memory (`_skopeo_cache`)
- **ImagePullSecrets** are dynamically extracted from pods/ServiceAccounts for private registry auth
- Report data stored as JSON files in `REPORT_DIR` (default: `/app/data/reports`)
- Background scans use `threading.Thread` with progress callbacks
- initContainer-only images get severity downgraded by one level

## Build & Deploy
```bash
./setup.sh --build-push-deploy   # Full pipeline
./setup.sh --build               # Build only
./setup.sh --push                # Push to Quay.io
./setup.sh --deploy              # Apply OpenShift manifests
./setup.sh --restart             # Rollout restart
```

## Environment Variables
| Variable | Default | Description |
|---|---|---|
| `REPORT_DIR` | `/app/data/reports` | Report storage path |
| `SKIP_SYSTEM_NAMESPACES` | `true` | Skip openshift-* and kube-* namespaces |
| `SKOPEO_TLS_VERIFY` | `true` | TLS verification for skopeo |
| `SKOPEO_MAX_WORKERS` | `10` | Concurrent skopeo inspection threads |
| `USE_IMAGE_PULL_SECRETS` | `true` | Extract registry auth from pods |
| `SKOPEO_AUTH_FILE` | (empty) | Global skopeo auth file path |

## When Making Changes
- Always use PatternFly 6 components and dark theme CSS variables
- Keep the OpenShift Console-style look and feel
- Test with `python run.py` locally on port 8080
- Container image: `quay.io/chiaretto/cgroups-v2-checker:latest`
- Run `./setup.sh --build-push-deploy` to ship changes
- Reports page supports clickable severity cards (filters table), image drilldown modal (shows pods/namespaces), and CSV download
- Registries page allows users to add/remove private registry credentials (username/password) used by skopeo during scans. Credentials stored in `REPORT_DIR/../registries.json` and merged into scanner's `_registry_auths` at scan start.
