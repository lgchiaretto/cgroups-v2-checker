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

---

## setup.sh -- Complete Reference

`setup.sh` is the single management script for the entire lifecycle of the application: local development, building the container image, pushing to registries, deploying to OpenShift, and ongoing operations.

```
Usage: ./setup.sh [global options] <action> [<action> ...]
```

Multiple actions can be combined in one command and are executed in order.

### Global Options

Global options must come **before** actions. They configure how the actions behave.

| Option | Argument | Description |
|---|---|---|
| `--proxy <URL\|auto>` | Proxy URL or `auto` | Set HTTP/HTTPS proxy for build, push, and deployment |
| `--no-proxy <hosts>` | Comma-separated list | Override NO_PROXY hosts (merged with auto-detected CIDRs) |
| `--mirror` | *(none)* | Push to OCP internal registry instead of Quay.io |
| `--ca-cert <file>` | Path to PEM file | Custom CA certificate for TLS-intercepting proxies |

### Actions

#### Local Development

| Action | Description |
|---|---|
| `--build` | Build the container image with Podman |
| `--run-local` | Run the container locally on `localhost:8080` |
| `--stop` | Stop and remove the local container |
| `--status` | Show local container status and health |
| `--logs` | Tail local container logs (Ctrl+C to exit) |
| `--destroy` | Remove container, image, and all local data (with confirmation) |

#### OpenShift Deployment

| Action | Description |
|---|---|
| `--push` | Push the image to Quay.io |
| `--deploy` | Apply OpenShift manifests (namespace, RBAC, deployment, service, route). Auto-seeds registry credentials on first deploy |
| `--build-push-deploy` | Full pipeline: build + push (or mirror) + deploy + restart |
| `--restart` | Rolling restart of the deployment |
| `--persistent` | Persist registry credentials to a Kubernetes Secret (survives pod restarts) |
| `--openshift-status` | Show deployments, pods, services, and routes |
| `--openshift-logs` | Tail deployment logs (Ctrl+C to exit) |
| `--remove` | Completely remove the application from OpenShift (with confirmation) |

---

### How `--proxy` Works

The `--proxy` option configures HTTP/HTTPS proxy for every phase of the pipeline.

**With an explicit URL** (`--proxy http://proxy.corp.com:8080`):

| Phase | Effect |
|---|---|
| `--build` | Passes `http_proxy` / `https_proxy` as `--build-arg` to `podman build` |
| `--push` | Exports `HTTP_PROXY` / `HTTPS_PROXY` for `podman push` |
| `--deploy` | Injects `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` (+ lowercase variants) into the Deployment via `oc set env` |

**With `auto`** (`--proxy auto`):

1. Reads the cluster's Proxy object (`oc get proxy cluster`)
2. Extracts `spec.httpProxy`, `spec.httpsProxy`, and `spec.noProxy`
3. Auto-detects the cluster's **service CIDR** (from `network.config/cluster` or fallback from the `kubernetes` Service IP) and **pod CIDR**
4. Merges all NO_PROXY sources (cluster `noProxy` + detected CIDRs + essential entries like `.cluster.local`, `.svc`, `localhost`)
5. Uses the merged result to prevent in-cluster traffic from being routed through the proxy

**NO_PROXY auto-detection** ensures the Kubernetes API server is always reachable directly, even if the cluster's `spec.noProxy` doesn't explicitly include the service network CIDR. This prevents multi-minute connection timeouts when the proxy cannot reach internal cluster IPs.

### How `--no-proxy` Works

Explicitly sets NO_PROXY hosts. The provided list is **merged** (not replaced) with:

- Auto-detected cluster CIDRs (service network, pod network)
- Essential entries (`.cluster.local`, `.svc`, `localhost`, `127.0.0.1`)

This ensures in-cluster connectivity is never broken, regardless of what you specify.

### How `--mirror` Works

Pushes the image to the OCP **internal registry** instead of Quay.io. This is essential in environments where the cluster cannot pull from external registries (proxy/TLS interception, air-gapped networks, registry restrictions).

Steps performed:

1. Ensures the namespace exists
2. Exposes the internal registry route (patches `configs.imageregistry.operator.openshift.io/cluster` if needed)
3. Logs into the internal registry using the current `oc` session token (proxy bypassed)
4. Tags and pushes the image to `image-registry.openshift-image-registry.svc:5000/<namespace>/<app>` (proxy bypassed)
5. On `--deploy`, patches the Deployment to use the internal image reference

All mirror operations explicitly clear proxy env vars to ensure direct connectivity.

### How `--ca-cert` Works

Injects a custom CA certificate into the container image during build. This is needed when a corporate proxy performs TLS interception -- the custom CA must be trusted inside the container for `skopeo` to work correctly.

The certificate is copied as `.build-ca.pem` during `podman build` and removed afterwards.

### Auto-Seeded Registry Credentials

On the first `--deploy`, the script automatically:

1. Reads the cluster's global pull-secret (`openshift-config/pull-secret`)
2. Extracts registry credentials (registry.redhat.io, quay.io, etc.)
3. Creates a Kubernetes Secret (`cgroups-v2-checker-registries`) with `registries.json`
4. Patches the Deployment to mount the Secret and sets `REGISTRIES_FILE` env var

This gives the scanner immediate access to all registries configured in the cluster, without manual credential setup.

- Requires read access to `openshift-config/pull-secret` (typically cluster-admin)
- Skipped if the credentials Secret already exists (preserves user-added credentials)
- Additional registries can be added via the web UI and persisted with `--persistent`

### How `--persistent` Works

Persists registry credentials to a Kubernetes Secret so they survive pod restarts:

1. Reads `registries.json` from the running pod (credentials added via web UI)
2. Creates or updates the Secret `cgroups-v2-checker-registries`
3. Patches the Deployment to mount the Secret
4. Restarts the Deployment

If no running pod is found, offers interactive manual credential entry.

---

### Deployment Scenarios -- Which Combination to Use

#### Direct internet access (no proxy)

The simplest case. The cluster can pull from Quay.io and skopeo can reach all registries.

```bash
./setup.sh --build-push-deploy
```

#### Corporate proxy, cluster can pull from Quay.io

The cluster can pull the app image from Quay.io, but skopeo needs the proxy to reach external registries for inspection.

```bash
# Auto-detect proxy from cluster config
./setup.sh --proxy auto --build-push-deploy

# Or specify the proxy URL
./setup.sh --proxy http://proxy.corp.com:8080 --build-push-deploy
```

#### Corporate proxy, cluster CANNOT pull from Quay.io

The cluster cannot reach Quay.io (proxy blocks it, TLS interception, etc.). Push the image to the internal registry instead.

```bash
# Mirror + auto-detect proxy (most common in enterprise environments)
./setup.sh --mirror --proxy auto --build-push-deploy
```

#### TLS-intercepting proxy with custom CA

The proxy performs TLS inspection and replaces certificates. skopeo will fail without the proxy's CA certificate.

```bash
# Mirror + proxy + custom CA
./setup.sh --mirror --proxy auto --ca-cert /path/to/proxy-ca.pem --build-push-deploy

# Without mirror (if cluster can pull from Quay.io)
./setup.sh --proxy auto --ca-cert /path/to/proxy-ca.pem --build-push-deploy
```

#### Air-gapped / disconnected environment

No external network access. Build the image on a connected machine, transfer it, then deploy.

```bash
# On a connected machine: build and save
./setup.sh --build
podman save quay.io/chiaretto/cgroups-v2-checker:latest -o cgroups-v2-checker.tar

# On the disconnected cluster: load and deploy with mirror
podman load -i cgroups-v2-checker.tar
./setup.sh --mirror --deploy --restart
```

#### Updating after code changes

```bash
# Rebuild and redeploy (preserves existing proxy/mirror settings in the deployment)
./setup.sh --build-push-deploy

# Or with mirror + proxy
./setup.sh --mirror --proxy auto --build-push-deploy

# Quick restart only (no rebuild)
./setup.sh --restart
```

#### Persisting registry credentials after web UI setup

```bash
# After adding credentials via the web UI:
./setup.sh --persistent
```

#### Individual steps (granular control)

```bash
./setup.sh --build                          # Build only
./setup.sh --push                           # Push to Quay.io only
./setup.sh --mirror --deploy                # Mirror + deploy only (no build)
./setup.sh --proxy auto --deploy            # Deploy with proxy injection only
./setup.sh --deploy --restart               # Deploy manifests + restart
./setup.sh --openshift-status               # Check deployment status
./setup.sh --openshift-logs                 # Tail pod logs
./setup.sh --remove                         # Uninstall everything
```

#### Complete cleanup and reinstall

```bash
# Remove from OpenShift
./setup.sh --remove

# Remove local resources
./setup.sh --destroy

# Fresh install
./setup.sh --mirror --proxy auto --build-push-deploy
```

---

### Global Options vs Actions -- Quick Reference

```
./setup.sh [--proxy URL|auto] [--no-proxy hosts] [--mirror] [--ca-cert file] <action>
           \_________________/ \________________/ \________/ \_____________/
           Optional: proxy      Optional: extra     Optional:  Optional:
           for build/push/       NO_PROXY hosts     push to    custom CA
           deploy                                   internal   for TLS
                                                    registry   interception
```

**Combinations matrix:**

| Scenario | `--proxy` | `--mirror` | `--ca-cert` | `--no-proxy` |
|---|---|---|---|---|
| Direct internet | - | - | - | - |
| Proxy, external pull OK | `auto` or URL | - | - | optional |
| Proxy, no external pull | `auto` or URL | yes | - | optional |
| TLS-intercepting proxy | `auto` or URL | recommended | yes | optional |
| Air-gapped | - | yes | - | - |

---

## Local Development

```bash
pip install -r requirements.txt
export REPORT_DIR=./data/reports
python run.py
```

Access `http://localhost:8080`. Uses the local kubeconfig (`~/.kube/config` or `$KUBECONFIG`) to connect to the cluster.

### Run locally with Podman

```bash
./setup.sh --build --run-local    # Build and run
./setup.sh --status               # Check status
./setup.sh --logs                 # View logs
./setup.sh --stop                 # Stop
./setup.sh --destroy              # Remove everything
```

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `SECRET_KEY` | (auto-generated) | Flask secret key |
| `REPORT_DIR` | `/app/data/reports` | Directory for saving reports |
| `SKIP_SYSTEM_NAMESPACES` | `true` | Skip openshift-* and kube-* namespaces |
| `SKOPEO_TLS_VERIFY` | `true` | Verify TLS in skopeo calls |
| `SKOPEO_AUTH_FILE` | (empty) | Auth file path for private registries |
| `SKOPEO_MAX_WORKERS` | `20` | Parallel threads for skopeo inspection |
| `EXEC_MAX_WORKERS` | `10` | Parallel threads for pod exec checks |
| `USE_IMAGE_PULL_SECRETS` | `true` | Extract registry auth from pods/ServiceAccounts |
| `REGISTRIES_FILE` | (empty) | Path to mounted registries.json from K8s Secret |
| `IMAGE_TAG` | `latest` | Image tag used by setup.sh |
| `LOCAL_PORT` | `8080` | Local port for `--run-local` |

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
