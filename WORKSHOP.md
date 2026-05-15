# cgroups v2 Checker -- Workshop

Step by step guide to install the cgroups v2 Checker on an OpenShift cluster, run scans, analyze results, and clean up. The test lab workloads are already deployed on the cluster as part of the product installation.

**Prerequisites:**
- `oc` CLI logged into an OpenShift 4.x cluster with cluster-admin privileges
- `git` installed

---

## 1. Clone the Repository

```bash
git clone https://github.com/lgchiaretto/cgroups-v2-checker.git
cd cgroups-v2-checker
```

---

## 2. Deploy to OpenShift

The `setup.sh` script handles the full lifecycle:

**Direct internet access:**

```bash
./setup.sh --deploy
```

---

## 3. Verify the Deployment

Check that the pods are running and the route is available:

```bash
./setup.sh --openshift-status
```

Expected output:

```
Deployments:
NAME                    READY   UP-TO-DATE   AVAILABLE
cgroups-v2-checker      1/1     1            1

Pods:
NAME                                  READY   STATUS    RESTARTS
cgroups-v2-checker-xxxxx-yyyyy        1/1     Running   0

Routes:
NAME                    HOST/PORT
cgroups-v2-checker      cgroups-v2-checker-cgroups-v2-checker.<cluster-domain>
```

Save the route URL for later:

```bash
ROUTE_URL=$(oc get route cgroups-v2-checker -n cgroups-v2-checker -o jsonpath='{.spec.host}')
echo "Open in browser: https://${ROUTE_URL}"
```

---

## 4. Open the Web Interface

Open the route URL in your browser. You will see the cgroups v2 Checker dashboard with:

- A **Start Scan** button
- A list of previous reports (empty on first run)

---

## 5. Run Your First Scan

Run a scan against the cluster workloads to see the scanner in action. The test lab namespaces (`cgv2-lab-*`) are already deployed on the cluster and contain sample workloads with intentionally problematic images across every severity level.

**Via the web interface:**

1. Click **Start Scan** on the dashboard
2. Watch the real-time progress bar as pods are inspected
3. When complete, click the report to see results

---

## 6. Understand the Test Lab Workloads

The product installation automatically deployed sample workloads with intentionally problematic images to exercise every severity level. The test lab runs across 6 namespaces:

| Namespace | Workloads | Expected Severities |
|-----------|-----------|---------------------|
| `cgv2-lab-java` | OpenJDK 8u302, 11.0.11, 8 (safe), 17, 21 | CRITICAL, HIGH, OK |
| `cgv2-lab-os` | CentOS 7, UBI 7, UBI 8, UBI 9 | CRITICAL, OK |
| `cgv2-lab-node` | Node.js 16, 18, 20 | HIGH, OK |
| `cgv2-lab-mixed` | Multi-container with CentOS 7 sidecar, initContainer, JVM flags | CRITICAL, HIGH, OK |
| `cgv2-lab-apps-1` | API gateway, auth, order, notification, frontend (all safe) | OK |
| `cgv2-lab-apps-2` | Billing, report, cache, metrics (all safe) | OK |

---

## 7. Analyze the Report

Open the report in the browser. The report page shows:

- **Severity cards** at the top: CRITICAL, HIGH, LOW, INFO, OK, UNKNOWN -- click any card to filter
- **Image table** with columns: Image, Severity, Findings, Pods
- **Text filter** to search by image name
- **Pagination** for large image lists
- **CSV / JSON download** buttons

> **Tip:** Click on any image row to see the detailed findings modal, showing which pods use that image and what was detected (OS version, runtime version, JVM flags, cgroups hierarchy).

---

## 8. Run a Targeted Scan (test lab only)

Now scan only the test lab namespaces to isolate the expected severity distribution from other cluster workloads:

**Via the API:**

```bash
curl -sk -X POST https://${ROUTE_URL}/api/scan \
  -H "Content-Type: application/json" \
  -d '{
    "namespace_patterns": ["^cgv2-lab-"]
  }'
```

**Or via the web interface:**

1. Click **Start Scan**
2. In the namespace filter, enter pattern: `cgv2-lab-`
3. Click **Scan**

Monitor progress:

```bash
# Replace <scan-id> with the returned ID
curl -sk https://${ROUTE_URL}/api/scan/<scan-id> | python3 -m json.tool
```

---

## 9. Analyze the Scan Results

Open the report in the browser. You should see the following severity distribution:

### Expected Results

| Severity | Count | Images |
|----------|-------|--------|
| **CRITICAL** | 4 | `openjdk:8u302-jre-slim`, `centos:7`, `ubi7/ubi:latest`, `centos:7` (sidecar/init) |
| **HIGH** | 4 | `openjdk:11.0.11-jre-slim`, `nodejs-16`, `nodejs-18`, `openjdk-17` with `-XX:-UseContainerSupport` |
| **OK** | ~10 | `openjdk-8:latest` (safe), `openjdk-17`, `openjdk-21`, `nodejs-20`, `ubi8-minimal`, `ubi9-minimal` |

### What to Look For

1. **CRITICAL findings**: Click the red card. You should see CentOS 7 and UBI 7 images flagged as incompatible OS. The `openjdk:8u302` image is CRITICAL because its Java version (8u302) is below the safe threshold (8u372+).

2. **HIGH findings**: Click the orange card. You should see:
   - `openjdk:11.0.11` -- Java 11 below 11.0.16+
   - `nodejs-16` and `nodejs-18` -- Node.js below 20+
   - `openjdk-17` with `-XX:-UseContainerSupport` -- JVM flag disables container awareness

3. **OK findings**: Click the green card. All UBI 8/9 based images with safe runtime versions.

4. **Image drilldown**: Click any image row to see which pods and namespaces use it.

5. **Multi-container detection**: The `multi-container` pod shows both the safe main container (UBI 9) and the CRITICAL sidecar (CentOS 7) as separate image entries.

6. **InitContainer detection**: The `init-only` pod shows the CentOS 7 init container flagged with the same severity as regular containers.

---

## 10. Download the Report

Download the scan results for offline analysis or sharing:

**CSV format** (from the web UI):

1. Open the report
2. Click the **CSV** button at the top

**JSON format** (via API):

---

## 11. Cleanup: Remove the cgroups v2 Checker

When you are done with the tool, remove it completely from the cluster:

```bash
./setup.sh --remove
```

This deletes the deployment, service, route, RBAC (ClusterRole, ClusterRoleBinding, ServiceAccount), and the namespace.

---

## Quick Reference

| Action | Command |
|--------|---------|
| Deploy | `./setup.sh --deploy` |
| Check status | `./setup.sh --openshift-status` |
| View logs | `./setup.sh --openshift-logs` |
| Restart | `./setup.sh --restart` |
| Remove | `./setup.sh --remove` |
| Start scan (API) | `curl -X POST https://$ROUTE_URL/api/scan -H "Content-Type: application/json" -d '{}'` |
| List reports (API) | `curl https://$ROUTE_URL/api/reports` |

---

## Severity Reference

| Severity | Meaning | Action |
|----------|---------|--------|
| **CRITICAL** | Incompatible base OS (CentOS 7, RHEL 7, old Ubuntu/Debian) | Rebuild on supported base image before OCP 4.19 upgrade |
| **HIGH** | Runtime needs update (Java < 11.0.16, Node.js < 20, JVM flags disabling container support) or very old Java (< 8u372) | Update runtime version or fix configuration |
| **INFO** | Informational | No action needed |
| **OK** | Fully compatible | No action needed |
| **UNKNOWN** | Could not inspect (no shell, crash, permissions) | Verify manually |
