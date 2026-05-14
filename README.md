# cgroups v2 Compatibility Checker for OpenShift

A web application that scans OpenShift clusters for container images with potential cgroups v2 compatibility issues before upgrading to OpenShift 4.19 (RHCOS 9, cgroups v2 mandatory).

Runs a lightweight detection script inside each running pod via `oc exec` to check OS version, runtime versions (Java, Node.js, .NET), JVM flags, and cgroups v1 references.

| [Documentacao em Portugues](README.pt-br.md) |
|---|

---

## Context

OpenShift 4.19 ships RHCOS 9, which operates exclusively with cgroups v2 (unified hierarchy). Container images based on old operating systems (RHEL 7, CentOS 7, Ubuntu < 20.04, etc.) or running outdated runtimes (JDK < 15, .NET < 6) may break in a cgroups v2-only environment.

This tool helps cluster administrators identify those images before the upgrade, providing severity-based findings and actionable recommendations.

## Features

- Pod exec-based runtime detection: OS, Java, Node.js, .NET, JVM flags, cgroups v1 references
- Optional skopeo fallback for images where exec fails (distroless/scratch images)
- Severity classification: CRITICAL, HIGH, LOW, INFO, OK, UNKNOWN
- Inspection metadata audit trail (shows what was detected for each image)
- Clickable severity cards to filter the report table
- Image drilldown modal showing pods and namespaces per image
- Pagination for large image lists (20/50/100/All)
- Text filter and severity filter buttons
- CSV and JSON report download
- Registry credentials management for private registries (used by skopeo fallback)
- Skip reason breakdown for excluded pods (system namespaces vs. user-excluded)
- Background scan with real-time progress reporting
- PatternFly 6 dark theme (OpenShift Console style)

## How It Works

### Pod Exec Inspection (primary)

Lists all pods via the Kubernetes API, then executes a lightweight detection script inside each running pod via `oc exec`. The script is read-only and uses `command -v` checks before executing runtime commands.

Detections performed:

- **Operating System**: reads `/etc/os-release` to detect legacy OS (CentOS 7, RHEL 7, Ubuntu < 20.04, Debian < 11, Alpine < 3.14)
- **Java**: runs `java -version` to get the real JDK version (safe: 17+, 11.0.16+, 8u372+)
- **JVM Flags**: checks `JAVA_TOOL_OPTIONS`, `JAVA_OPTS`, `_JAVA_OPTIONS`, `JDK_JAVA_OPTIONS` for `-XX:-UseContainerSupport`
- **Node.js**: runs `node --version` (safe: 20+)
- **.NET**: runs `dotnet --list-runtimes` (safe: 5+)
- **cgroups hierarchy**: checks if the pod runs under cgroups v1 or v2
- **cgroups v1 file references**: searches application files for hardcoded v1 paths (`memory.limit_in_bytes`, `cpu.cfs_quota_us`, etc.)
- **cgroups v1 env references**: checks PID 1 environment variables for v1 paths

### Skopeo Fallback (optional)

For images where exec fails (distroless/scratch images without a shell), skopeo can be used as a fallback to inspect image metadata remotely. Disabled by default.

### Severity Classification

#### CRITICAL -- Incompatible base OS, will break on cgroups v2

The operating system itself does not support cgroups v2. These containers **will fail** after the upgrade to OCP 4.19.

Real-world examples:
- `centos:7`, `quay.io/centos/centos:7` -- CentOS 7 (EOL June 2024), kernel and userspace tools only understand cgroups v1
- `registry.access.redhat.com/ubi7/ubi:latest` -- UBI 7 / RHEL 7 based images
- `registry.access.redhat.com/rhel6/rhel:latest` -- RHEL 6 based images
- `ubuntu:18.04`, `ubuntu:16.04` -- Ubuntu versions before 20.04
- `debian:10` (Buster), `debian:9` (Stretch) -- Debian versions before 11
- `openjdk:8u302-jre-slim` -- Java 8 < 8u372 (cannot read container memory/CPU limits under cgroups v2)

**Action required**: Rebuild the application on a supported base image (UBI 8/9, Ubuntu 22.04+, Debian 12+) **before** the upgrade.

#### HIGH -- Runtime needs update, may cause issues on cgroups v2

The base OS is compatible, but the application runtime has known cgroups v2 issues. The container will start, but the application may **behave incorrectly** (wrong memory limits, CPU throttling, OOM kills).

Real-world examples:
- `openjdk:11.0.11-jre-slim` -- Java 11 < 11.0.16 reads cgroups v1 paths for memory/CPU, gets host values instead of container limits
- `registry.access.redhat.com/ubi8/nodejs-16:latest` -- Node.js 16 has limited cgroups v2 heap sizing
- `registry.access.redhat.com/ubi9/nodejs-18:latest` -- Node.js 18 has partial cgroups v2 support, should upgrade to 20+
- Alpine 3.13 or older with application runtimes -- musl libc < 1.2.2 has incomplete cgroups v2 support
- Java 17 with `JAVA_TOOL_OPTIONS="-XX:-UseContainerSupport"` -- explicitly disables container awareness, JVM ignores cgroups limits
- Application files referencing hardcoded cgroups v1 paths (`/sys/fs/cgroup/memory/memory.limit_in_bytes`, `/sys/fs/cgroup/cpu/cpu.cfs_quota_us`)
- Environment variables pointing to cgroups v1 paths

**Action required**: Update the runtime to a cgroups v2-compatible version or fix the configuration. Safe versions: Java 17+ / 11.0.16+ / 8u372+, Node.js 20+, .NET 6+.

#### LOW -- Minor risk, review when convenient

Low-risk findings or situations where metadata is insufficient for a definitive assessment.

Real-world examples:
- Insufficient metadata to determine full compatibility
- Minor configuration items that are unlikely to cause failures

**Action**: Review when convenient. These findings are unlikely to cause immediate issues during the upgrade.

#### INFO -- Informational, no action needed

Purely informational findings that provide context but do not indicate any risk.

**Action**: No action required.

#### OK -- Fully compatible with cgroups v2

The image was inspected and no compatibility issues were found.

Real-world examples:
- `registry.access.redhat.com/ubi9/openjdk-17:latest` -- Java 17 on UBI 9
- `registry.access.redhat.com/ubi9/openjdk-21:latest` -- Java 21 on UBI 9
- `registry.access.redhat.com/ubi8/openjdk-8:1.18` -- Java 8u392+ (safe build)
- `registry.access.redhat.com/ubi9/nodejs-20:latest` -- Node.js 20 on UBI 9
- `registry.access.redhat.com/ubi9/ubi-minimal:latest` -- UBI 9 minimal (no runtime)
- `registry.access.redhat.com/ubi8/ubi-minimal:latest` -- UBI 8 minimal

#### UNKNOWN -- Could not inspect

The checker was unable to inspect the image. Typically because `oc exec` failed (the pod crashed, the container has no shell, or permissions were denied) and skopeo fallback was not enabled.

Real-world examples:
- `gcr.io/distroless/java17-debian11` -- Distroless images have no shell, exec cannot run
- Pods in `CrashLoopBackOff` or `Error` state
- Containers with `securityContext.readOnlyRootFilesystem` and restricted exec permissions

**Action**: Enable skopeo fallback to inspect these images via remote metadata, or verify them manually.

---

Images found only in initContainers are flagged in the report but receive the same severity as regular containers -- if the image has a problem, it needs to be fixed regardless of where it runs.

## Architecture

```
Browser  -->  Flask (Gunicorn, PatternFly v6 dark theme)
                |
                +--> Kubernetes API (list pods, read cluster info)
                +--> pod exec (primary: OS, runtimes, cgroups detection)
                +--> skopeo inspect (optional fallback for exec failures)
                +--> JSON reports saved to REPORT_DIR
```

The application runs inside the OpenShift cluster with a ServiceAccount that has permission to list pods, exec into pods, read secrets (ImagePullSecrets), and read cluster version info.

## File Structure

```
app/
  app.py             Flask application factory
  config.py          Configuration via environment variables
  scanner.py         Scanning engine (Kubernetes API + pod exec + skopeo fallback)
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
| `SKOPEO_MAX_WORKERS` | `20` | Parallel threads for skopeo fallback inspection |
| `EXEC_MAX_WORKERS` | `20` | Parallel threads for pod exec inspection |
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
  -d '{"namespaces": ["my-app"]}'
```

### Scan options

| Field | Type | Default | Description |
|---|---|---|---|
| `namespaces` | list | all | Specific namespaces to scan |
| `exclude_namespaces` | list | none | Namespaces to exclude |
| `namespace_patterns` | list | none | Regex patterns to include namespaces |
| `exclude_patterns` | list | none | Regex patterns to exclude namespaces |
| `skopeo_fallback` | bool | false | Enable skopeo fallback for images where exec fails |

## Container Image

The application runs on UBI 9 with Python 3.11 and skopeo pre-installed. Built with Podman and deployable on any OpenShift 4.x cluster.

## License

This project is provided as-is for OpenShift upgrade readiness assessment.
