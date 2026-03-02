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

- Two-level image analysis (name/tag rules + remote metadata inspection via skopeo)
- Severity classification: CRITICAL, HIGH, MEDIUM, LOW, OK
- Clickable severity cards to filter the report table
- Image drilldown modal showing pods and namespaces per image
- Pagination for large image lists (20/50/100/All)
- Text filter and severity filter buttons
- Filter for images that failed skopeo inspection
- CSV and JSON report download
- Registry credentials management for private registries (in-memory only, not persisted)
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

### Severity Classification

| Severity | Description |
|---|---|
| CRITICAL | Incompatible or EOL base image (RHEL 6/7, CentOS 7) |
| HIGH | Runtime with serious cgroups v2 issues (Java < 11, Elasticsearch 6.x) |
| MEDIUM | Possible issue, needs validation (Alpine < 3.14, .NET < 6) |
| LOW | Low risk, update recommended |
| OK | No issues detected |

Images found only in initContainers have their severity downgraded by one level.

## Architecture

```
Browser  -->  Flask (Gunicorn, PatternFly v6 dark theme)
                |
                +--> Kubernetes API (list pods, read cluster info)
                +--> skopeo inspect docker://IMAGE (metadata only)
                +--> JSON reports saved to REPORT_DIR
```

The application runs inside the OpenShift cluster with a ServiceAccount that has permission to list pods, read secrets (ImagePullSecrets), and read cluster version info.

## File Structure

```
app/
  app.py             Flask application factory
  config.py          Configuration via environment variables
  scanner.py         Scanning engine (Kubernetes API + skopeo)
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
# Full pipeline: build, push, deploy, restart
./setup.sh --build-push-deploy

# Individual steps
./setup.sh --build      # Build container image
./setup.sh --push       # Push to registry
./setup.sh --deploy     # Apply OpenShift manifests
./setup.sh --restart    # Rollout restart

# Remove from cluster
./setup.sh --remove
```

Or manually:

```bash
# Build and push
podman build -t quay.io/YOUR_USER/cgroups-v2-checker:latest -f Containerfile .
podman push quay.io/YOUR_USER/cgroups-v2-checker:latest

# Deploy
oc apply -f openshift/namespace.yaml
oc apply -f openshift/rbac.yaml
oc apply -f openshift/deployment.yaml

# Get the application URL
oc get route -n cgroups-v2-checker
```

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
  -d '{"namespaces": ["my-app"], "inspect_images": true}'
```

### Scan options

| Field | Type | Default | Description |
|---|---|---|---|
| `namespaces` | list | all | Specific namespaces to scan |
| `exclude_namespaces` | list | none | Namespaces to exclude |
| `inspect_images` | bool | true | Enable skopeo remote inspection |

## Container Image

The application runs on UBI 9 with Python 3.11 and skopeo pre-installed. Built with Podman and deployable on any OpenShift 4.x cluster.

## License

This project is provided as-is for OpenShift upgrade readiness assessment.
