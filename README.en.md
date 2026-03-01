# cgroups v2 Checker - Documentation

Web application that scans OpenShift clusters to identify container images with cgroups v2 compatibility issues before upgrading to OpenShift 4.19.

## Context

OpenShift 4.19 uses RHCOS 9, which operates exclusively with cgroups v2 (unified hierarchy). Images based on old operating systems (RHEL 7, CentOS 7, Ubuntu < 20.04, etc.) or with outdated runtimes (JDK < 15, .NET < 6) may fail in the new environment.

## How It Works

### Level 1 - Name/Tag Analysis

The application lists all pods in the cluster via the Kubernetes API and analyzes image names using detection rules:

- **Problematic base images**: RHEL/CentOS 6/7, Debian < 11, Ubuntu < 20.04, Alpine < 3.14, Amazon Linux 1/2, Oracle Linux 7, SLES 12
- **Outdated runtimes**: Java (< JDK 15 unpatched), .NET (< 6), Node.js (< 16), Python 2, Go (< 1.19)
- **Known software**: MySQL 5.x, PostgreSQL < 14, Elasticsearch < 8.x, Kafka < 3.x, Jenkins with JDK8, WildFly < 26, Tomcat < 10

### Level 2 - Remote Inspection via skopeo

Optionally, the application uses `skopeo inspect docker://IMAGE` to read image remote metadata (manifest and config JSON) without downloading any layers. It checks:

- OCI labels (`org.opencontainers.image.base.name`, `com.redhat.component`)
- Environment variables (`JAVA_VERSION`, JVM flags)
- References to cgroups v1 paths in CMD/Entrypoint
- Referenced cgroups v1 files (e.g., `memory.limit_in_bytes`)

### Severity Classification

| Severity | Description |
|---|---|
| CRITICAL | Incompatible or EOL base image (RHEL 6/7, CentOS 7) |
| HIGH | Runtime with serious cgroups v2 issues (Java < 11, Elasticsearch 6.x) |
| MEDIUM | Possible issue, needs validation (Alpine < 3.14, .NET < 6) |
| LOW | Low risk, update recommended |
| OK | No issues detected |

## Architecture

```
Browser  -->  Flask (PatternFly v6 dark theme)
                |
                +--> Kubernetes API (list pods, get cluster info)
                +--> skopeo inspect docker://IMAGE (metadata only, no pull)
                +--> JSON reports saved to /app/data/reports/
```

The application runs inside the OpenShift cluster with a ServiceAccount that has permission to list pods and read cluster info.

## File Structure

```
app/
  __init__.py        # Python module
  app.py             # Flask application factory
  config.py          # Configuration via environment variables
  scanner.py         # Scanning engine (skopeo + Kubernetes API)
  routes.py          # Web routes (dashboard, reports)
  api.py             # REST API (trigger scan, list reports)
  templates/         # Jinja2 templates (PatternFly v6 dark theme)
    base.html
    dashboard.html
    report.html
    error.html
  static/
    css/app.css
    js/app.js
openshift/           # OpenShift deployment manifests
  namespace.yaml
  rbac.yaml
  deployment.yaml
Containerfile        # Image build (UBI 9 + skopeo + Python)
gunicorn.conf.py     # Gunicorn configuration
run.py               # Application entrypoint
requirements.txt     # Python dependencies
```

## Deploy to OpenShift

### 1. Build the image

```bash
podman build -t quay.io/chiaretto/cgroups-v2-checker:latest -f Containerfile .
podman push quay.io/chiaretto/cgroups-v2-checker:latest
```

### 2. Apply the manifests

```bash
oc apply -f openshift/namespace.yaml
oc apply -f openshift/rbac.yaml
oc apply -f openshift/deployment.yaml
```

### 3. Access the application

```bash
oc get route -n cgroups-v2-checker
```

Open the Route URL in your browser. The web interface lets you:
- Trigger on-demand scans
- Configure specific namespaces or exclude namespaces
- Enable/disable skopeo inspection (Level 2)
- View and filter results by severity
- Download reports as JSON

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `SECRET_KEY` | (auto-generated) | Flask secret key |
| `REPORT_DIR` | `/app/data/reports` | Directory for saving reports |
| `SKIP_SYSTEM_NAMESPACES` | `true` | Skip OpenShift system namespaces |
| `SKOPEO_TLS_VERIFY` | `true` | Verify TLS in skopeo calls |
| `SKOPEO_AUTH_FILE` | (empty) | Auth file for private registries |
| `SKOPEO_MAX_WORKERS` | `10` | Parallel threads for skopeo inspection |

## REST API

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/scan` | Start a new scan |
| `GET` | `/api/scan/<id>` | Scan status and progress |
| `GET` | `/api/reports` | List all reports |
| `GET` | `/api/reports/<id>` | Get a specific report |
| `DELETE` | `/api/reports/<id>` | Delete a report |

### Example: Start a scan

```bash
curl -X POST http://cgroups-v2-checker-route/api/scan \
  -H "Content-Type: application/json" \
  -d '{"namespaces": ["my-app"], "inspect_images": true}'
```

## Local Development

```bash
pip install -r requirements.txt
export REPORT_DIR=./data/reports
python run.py
```

Access `http://localhost:8080`. The cluster connection uses the local kubeconfig (`~/.kube/config` or `$KUBECONFIG`).
