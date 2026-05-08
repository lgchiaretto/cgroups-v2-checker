# cgroups v2 Compatibility Checker for OpenShift

A web application that scans OpenShift clusters for container images with potential cgroups v2 compatibility issues before upgrading to OpenShift 4.19 (RHCOS 9, cgroups v2 mandatory).

Uses **skopeo** for remote image metadata inspection without downloading layers.

| [Documentacao em Portugues](README.pt-br.md) |
|---|

---

## Context

OpenShift 4.19 ships RHCOS 9, which operates exclusively with cgroups v2 (unified hierarchy). Container images based on old operating systems (RHEL 7, CentOS 7, Ubuntu < 20.04, etc.) or running outdated runtimes (JDK < 15, .NET < 6) may break in a cgroups v2-only environment.

This tool helps cluster administrators identify those images before the upgrade, providing severity-based findings and actionable recommendations.

## Features

- Three-level image analysis (name/tag rules + remote metadata via skopeo + pod exec runtime detection)
- Severity classification: CRITICAL, HIGH, MEDIUM, LOW, INFO, OK, UNKNOWN
- Inspection metadata audit trail (shows what skopeo found for each image)
- Insufficient metadata detection (flags images that lack labels/OS identification)
- Clickable severity cards to filter the report table
- Image drilldown modal showing pods and namespaces per image
- Pagination for large image lists (20/50/100/All)
- Text filter and severity filter buttons
- Filter for images that failed skopeo inspection
- CSV and JSON report download
- Registry credentials management for private registries
- Skip reason breakdown for excluded pods (system namespaces vs. user-excluded)
- Background scan with real-time progress reporting
- PatternFly 6 dark theme (OpenShift Console style)

## How It Works

### Level 1 -- Name/Tag Analysis

Lists all pods via the Kubernetes API and analyzes image names using detection rules:

- **Problematic base images**: RHEL/CentOS 6/7, Debian < 11, Ubuntu < 20.04, Alpine < 3.14, Amazon Linux 1/2, Oracle Linux 7, SLES 12
- **Outdated runtimes**: Java (< JDK 15), .NET (< 6), Node.js (< 16), Python 2, Go (< 1.19)
- **Known software**: MySQL 5.x, PostgreSQL < 14, Elasticsearch < 8.x, Kafka < 3.x, Jenkins with JDK8, WildFly < 26, Tomcat < 10

### Level 2 -- Remote Inspection via skopeo

Uses `skopeo inspect docker://IMAGE` to read remote metadata (manifest and config JSON) without downloading layers:

- OCI labels (`org.opencontainers.image.base.name`, `com.redhat.component`)
- Environment variables (`JAVA_VERSION`, JVM flags)
- References to cgroups v1 paths in CMD/Entrypoint
- Direct cgroups v1 file references (`memory.limit_in_bytes`, etc.)
- Red Hat middleware detection (JBoss EAP, Data Grid versions via labels)

Images without base image labels or OS identification are flagged with "Insufficient Metadata" (LOW severity), recommending exec check or adding OCI labels. The JSON report includes an `inspection_metadata` block for each inspected image showing what skopeo found (label count, relevant labels, env vars), providing a clear audit trail for why an image was marked OK.

### Level 3 -- Pod Exec Runtime Detection (optional)

Executes a detection script inside running pods to check for cgroups v1 usage at runtime:

- Whether the pod runs under cgroups v1 or v2 hierarchy
- Application files that reference v1-specific paths
- Environment variables referencing cgroups v1

### Severity Classification

| Severity | Description |
|---|---|
| CRITICAL | Incompatible or EOL base image (RHEL 6/7, CentOS 7) |
| HIGH | Runtime with serious cgroups v2 issues (Java < 11, Elasticsearch 6.x) |
| MEDIUM | Possible issue, needs validation (Alpine < 3.14, .NET < 6) |
| LOW | Low risk or insufficient metadata for validation |
| INFO | Informational finding |
| OK | No issues detected (metadata validated) |
| UNKNOWN | Not inspected (skopeo disabled or failed) |

Images found only in initContainers have their severity downgraded by one level.

## Architecture

```
Browser  -->  Flask (Gunicorn, PatternFly v6 dark theme)
                |
                +--> Kubernetes API (list pods, read cluster info)
                +--> skopeo inspect docker://IMAGE (metadata only)
                +--> pod exec (optional runtime detection)
                +--> JSON reports saved to REPORT_DIR
```

The application runs inside the OpenShift cluster with a ServiceAccount that has permission to list pods, exec into pods, read secrets (ImagePullSecrets), and read cluster version info.

## File Structure

```
app/
  app.py             Flask application factory
  config.py          Configuration via environment variables
  scanner.py         Scanning engine (Kubernetes API + skopeo + exec)
  routes.py          Web routes (dashboard, reports, CSV download)
  api.py             REST API (/api/scan, /api/reports, /api/registries)
  templates/         Jinja2 templates (PatternFly v6 dark theme)
    base.html        Base layout with sidebar navigation
    dashboard.html   Home page with scan controls and report list
    report.html      Report view with cards, filters, pagination, drilldown
    registries.html  Registry credentials management
    error.html       Error page
  static/
    css/app.css      Custom styles (dark theme, pagination, cards)
    js/app.js        Dashboard scan interaction
    icons/           SVG icons
openshift/           OpenShift deployment manifests
  namespace.yaml     Namespace definition
  rbac.yaml          ServiceAccount, ClusterRole, ClusterRoleBinding
  deployment.yaml    Deployment, Service, Route
setup.sh             Build, push, deploy, and manage script
Containerfile        Image build (UBI 9 + skopeo + Python 3.11)
gunicorn.conf.py     Gunicorn configuration (1 worker, 4 threads)
run.py               Application entrypoint
requirements.txt     Python dependencies
```

## Quick Start

### Deploy to OpenShift

Using the setup script:

```bash
# Full pipeline: build, push to Quay.io, deploy, restart
./setup.sh --build-push-deploy

# Individual steps
./setup.sh --build      # Build container image
./setup.sh --push       # Push to Quay.io
./setup.sh --deploy     # Apply OpenShift manifests
./setup.sh --restart    # Rollout restart

# Remove from cluster
./setup.sh --remove
```

### Deploy Behind a Corporate Proxy

When the cluster is behind a corporate proxy that intercepts TLS, image pulls from external registries may fail. Use `--mirror` to push the image directly to the OCP internal registry, bypassing the proxy entirely:

```bash
# Build locally and push to the internal registry (recommended for proxy environments)
./setup.sh --mirror --build-push-deploy

# Auto-detect proxy from cluster config and inject into the deployment (for skopeo)
./setup.sh --mirror --proxy auto --build-push-deploy

# Use a specific proxy URL
./setup.sh --mirror --proxy http://proxy.corp.com:8080 --build-push-deploy
```

### setup.sh Reference

```
Usage: ./setup.sh [global options] <action>

Global Options:
  --proxy <URL|auto>       Set HTTP/HTTPS proxy (used in build, push,
                           and injected into the OpenShift deployment).
                           Use "auto" to read from cluster Proxy object.
  --no-proxy <hosts>       Comma-separated NO_PROXY hosts (optional,
                           auto-detected from cluster or uses defaults)
  --mirror                 Push image to the OCP internal registry
                           instead of Quay.io (avoids proxy/TLS issues)

Actions:
  Local Development:
    --build                Build the container image
    --run-local            Run locally with Podman
    --stop                 Stop the local container
    --status               Check local container status
    --logs                 Show local container logs
    --destroy              Remove container, image, and data

  OpenShift Deployment:
    --push                 Push image to Quay.io
    --deploy               Deploy to OpenShift (apply manifests).
                           On first deploy, auto-seeds registry
                           credentials from the cluster pull-secret.
    --build-push-deploy    Full pipeline: build, push, deploy, restart
    --restart              Restart the OpenShift deployment
    --persistent           Persist registry credentials to a K8s Secret
    --openshift-status     Show OpenShift resources status
    --openshift-logs       Tail OpenShift deployment logs
    --remove               Completely remove app from OpenShift
```

**Auto-seeded registry credentials:**

On the first `--deploy`, the script automatically extracts credentials from the cluster's global pull-secret (`openshift-config/pull-secret`) and creates a Kubernetes Secret with `registries.json`. This gives the scanner immediate access to `registry.redhat.io`, `quay.io`, and other registries configured in the cluster — no manual credential setup needed.

- Requires read access to `openshift-config/pull-secret` (typically cluster-admin)
- Skipped if the credentials Secret already exists (preserves user-added credentials)
- Additional registries can be added via the web UI and persisted with `--persistent`

**How `--mirror` works:**

1. Exposes the OCP internal registry route (if not already exposed)
2. Logs in using the current `oc` session token
3. Tags and pushes the image to `image-registry.openshift-image-registry.svc:5000/<namespace>/<app>`
4. Patches the Deployment to use the internal image reference

**How `--proxy` works:**

| Action | Effect |
|---|---|
| `--build` | Passes `http_proxy`/`https_proxy` as `--build-arg` to podman |
| `--push` | Exports proxy env vars for podman push |
| `--deploy` | Injects `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` into the Deployment via `oc set env` |
| `auto` | Reads proxy config from `oc get proxy cluster` (httpProxy, httpsProxy, noProxy) |

### Local Development

```bash
pip install -r requirements.txt
export REPORT_DIR=./data/reports
python run.py
```

Access `http://localhost:8080`. Uses the local kubeconfig (`~/.kube/config` or `$KUBECONFIG`) to connect to the cluster.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `SECRET_KEY` | (auto-generated) | Flask secret key |
| `REPORT_DIR` | `/app/data/reports` | Directory for saving reports |
| `SKIP_SYSTEM_NAMESPACES` | `true` | Skip openshift-* and kube-* namespaces |
| `SKOPEO_TLS_VERIFY` | `true` | Verify TLS in skopeo calls |
| `SKOPEO_AUTH_FILE` | (empty) | Auth file path for private registries |
| `SKOPEO_MAX_WORKERS` | `20` | Parallel threads for skopeo inspection |
| `USE_IMAGE_PULL_SECRETS` | `true` | Extract registry auth from pods/ServiceAccounts |
| `REGISTRIES_FILE` | (empty) | Path to mounted registries.json from K8s Secret |

## REST API

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/scan` | Start a new scan |
| `GET` | `/api/scan/<id>` | Get scan status and progress |
| `GET` | `/api/reports` | List all reports |
| `GET` | `/api/reports/<id>` | Get a specific report |
| `DELETE` | `/api/reports/<id>` | Delete a report |
| `GET` | `/api/registries` | List configured registry credentials |
| `POST` | `/api/registries` | Add registry credentials |
| `DELETE` | `/api/registries/<host>` | Remove registry credentials |

### Start a scan

```bash
curl -X POST http://ROUTE_URL/api/scan \
  -H "Content-Type: application/json" \
  -d '{"namespaces": ["my-app"], "inspect_images": true, "exec_check": true}'
```

### Scan options

| Field | Type | Default | Description |
|---|---|---|---|
| `namespaces` | list | all | Specific namespaces to scan |
| `exclude_namespaces` | list | none | Namespaces to exclude |
| `namespace_patterns` | list | none | Regex patterns to include namespaces |
| `exclude_patterns` | list | none | Regex patterns to exclude namespaces |
| `inspect_images` | bool | true | Enable skopeo remote inspection (Level 2) |
| `exec_check` | bool | false | Enable pod exec runtime detection (Level 3) |

## Container Image

The application runs on UBI 9 with Python 3.11 and skopeo pre-installed. Built with Podman and deployable on any OpenShift 4.x cluster.

## License

This project is provided as-is for OpenShift upgrade readiness assessment.
