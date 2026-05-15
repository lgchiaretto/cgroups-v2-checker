"""
cgroups v2 Compatibility Scanner Engine
========================================
Scans OpenShift clusters for container images that may have cgroups v2
compatibility issues. Uses pod exec as the detection method — runs a
lightweight script inside each pod to detect OS version, runtime
versions (Java, Node.js, .NET), and cgroups v1 references.
"""

import json
import logging
import os
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

try:
    from kubernetes import client, config
    from kubernetes.client.rest import ApiException
    from kubernetes.stream import stream as k8s_stream
except ImportError:
    raise ImportError("Package 'kubernetes' not found. Install with: pip install kubernetes")

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Detection rules
# ─────────────────────────────────────────────────────────────────────────────

JAVA_SAFE_VERSIONS = "JDK 17+, JDK 11.0.16+, JDK 8u372+"
JAVA_DETAIL = (
    "JDK without cgroups v2 support does not recognize container memory/CPU limits. "
    "Memory impact: heap decouples from container size, causing OOMKill by the kernel. "
    "CPU impact: thread pools (including GC threads) are calculated from host CPU count, "
    "causing excessive threads, CPU throttling, and latency. "
    "See: developers.redhat.com/articles/2025/11/27/how-does-cgroups-v2-impact-java-net-and-nodejs-openshift-4"
)

KNOWN_PROBLEMATIC_OS = {
    ("centos", "7"): ("CentOS 7", "CRITICAL", "Migrate to UBI 8/9 or RHEL 8/9. CentOS 7 is EOL and has no cgroups v2 support."),
    ("rhel", "7"): ("RHEL 7", "CRITICAL", "Migrate to UBI 8/9. RHEL 7 does not support cgroups v2."),
    ("centos", "6"): ("CentOS 6", "CRITICAL", "Urgently migrate to UBI 8/9. CentOS 6 is EOL."),
    ("rhel", "6"): ("RHEL 6", "CRITICAL", "Urgently migrate to UBI 8/9. RHEL 6 is EOL."),
    ("ol", "7"): ("Oracle Linux 7", "CRITICAL", "Migrate to Oracle Linux 8/9."),
    ("amzn", "1"): ("Amazon Linux 1", "CRITICAL", "Migrate to Amazon Linux 2023. AL1 is EOL."),
}

CGROUPS_V1_PATHS = [
    "/sys/fs/cgroup/memory/", "/sys/fs/cgroup/cpu/",
    "/sys/fs/cgroup/cpuacct/", "/sys/fs/cgroup/cpuset/",
    "/sys/fs/cgroup/blkio/", "/sys/fs/cgroup/devices/",
    "/sys/fs/cgroup/freezer/", "/sys/fs/cgroup/pids/",
    "/sys/fs/cgroup/net_cls/", "/sys/fs/cgroup/net_prio/",
    "/sys/fs/cgroup/hugetlb/", "/sys/fs/cgroup/perf_event/",
    "/sys/fs/cgroup/systemd/",
]

CGROUPS_V1_FILES = [
    "memory.limit_in_bytes", "memory.usage_in_bytes",
    "memory.max_usage_in_bytes", "memory.failcnt",
    "cpu.cfs_quota_us", "cpu.cfs_period_us", "cpu.shares",
    "cpuacct.usage", "cpuacct.stat",
]

# ─────────────────────────────────────────────────────────────────────────────
# Pod exec inspection script
# ─────────────────────────────────────────────────────────────────────────────
# This is the primary detection method. Executed inside each running pod to
# detect cgroups v2 compatibility issues at runtime.
#
# Sections:
#   OS       — /etc/os-release to detect legacy OS (RHEL/CentOS 7, etc.)
#   JAVA     — java -version for real JDK version (not just ENV metadata)
#   JVMFLAGS — JVM env vars that may disable container support
#   NODE     — node --version for Node.js version
#   DOTNET   — dotnet --list-runtimes for .NET version
#   TYPE     — cgroups v1 vs v2 hierarchy
#   V1DIRS   — which v1-specific directories exist
#   V1REFS   — application files that reference v1 paths (timeout-protected)
#   ENVREF   — environment variables referencing cgroups v1
#
# Safety: all commands are read-only. `command -v` checks binary existence
# before execution to avoid noisy stderr. find+grep uses timeout 3s,
# maxdepth 3, size < 1MB to avoid hanging on large containers.
_EXEC_INSPECT_SCRIPT = (
    'echo "===OS===";'
    'cat /etc/os-release 2>/dev/null | grep -E "^(ID|VERSION_ID|PRETTY_NAME)=" || '
    'cat /etc/redhat-release 2>/dev/null || echo "unknown";'
    'echo "===JAVA===";'
    'command -v java >/dev/null 2>&1 && java -version 2>&1 || echo "NOT_FOUND";'
    'echo "===JVMFLAGS===";'
    'printenv JAVA_TOOL_OPTIONS JAVA_OPTS _JAVA_OPTIONS JDK_JAVA_OPTIONS 2>/dev/null; echo;'
    'echo "===NODE===";'
    'command -v node >/dev/null 2>&1 && node --version 2>/dev/null || echo "NOT_FOUND";'
    'echo "===DOTNET===";'
    'command -v dotnet >/dev/null 2>&1 && dotnet --list-runtimes 2>/dev/null | head -5 || echo "NOT_FOUND";'
    'echo "===TYPE===";'
    '[ -f /sys/fs/cgroup/cgroup.controllers ] && echo "v2" || echo "v1";'
    'echo "===V1DIRS===";'
    'for d in memory cpu cpuacct blkio pids devices freezer cpuset '
    'hugetlb net_cls net_prio perf_event; do'
    ' [ -d "/sys/fs/cgroup/$d" ] && echo "/sys/fs/cgroup/$d";'
    'done;'
    'echo "===V1REFS===";'
    'timeout 3 find /etc /opt /app /home -maxdepth 3 -type f -size -1M '
    '-exec grep -lE "sys/fs/cgroup/(memory|cpu|cpuacct|blkio)/|'
    'memory\\.limit_in_bytes|cpu\\.cfs_quota_us|cpu\\.shares|cpuacct\\." {} + '
    '2>/dev/null | head -20 || true;'
    'echo "===ENVREF===";'
    'cat /proc/1/environ 2>/dev/null | tr "\\0" "\\n" | grep -i cgroup 2>/dev/null || true;'
    'echo "===DONE==="'
)

_EXEC_MAX_WORKERS = int(os.environ.get("EXEC_MAX_WORKERS", "20"))
_EXEC_TIMEOUT = 15


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Finding:
    """A single finding for an image."""
    category: str
    severity: str
    message: str
    recommendation: str
    details: str = ""

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "severity": self.severity,
            "message": self.message,
            "recommendation": self.recommendation,
            "details": self.details,
        }


@dataclass
class ImageReport:
    """Report for a single container image."""
    image: str
    namespaces: Set[str] = field(default_factory=set)
    pods: Set[str] = field(default_factory=set)
    containers: Set[str] = field(default_factory=set)
    init_containers: Set[str] = field(default_factory=set)
    findings: List[Finding] = field(default_factory=list)
    inspected: bool = False
    inspection_error: str = ""
    only_in_init: bool = False
    inspection_metadata: Dict = field(default_factory=dict)

    @property
    def max_severity(self) -> str:
        order = {"CRITICAL": 0, "HIGH": 1, "LOW": 2, "INFO": 3}
        if not self.findings:
            if self.only_in_init:
                return "OK"
            if not self.inspected:
                return "UNKNOWN"
            return "OK"
        return min(self.findings, key=lambda f: order.get(f.severity, 99)).severity

    @property
    def severity_sort_key(self) -> int:
        order = {"CRITICAL": 0, "HIGH": 1, "LOW": 2, "INFO": 3, "UNKNOWN": 4, "OK": 5}
        return order.get(self.max_severity, 99)

    def to_dict(self) -> dict:
        d = {
            "image": self.image,
            "max_severity": self.max_severity,
            "namespaces": sorted(self.namespaces),
            "pods": sorted(self.pods),
            "containers": sorted(self.containers),
            "init_containers": sorted(self.init_containers),
            "only_in_init": self.only_in_init,
            "pod_count": len(self.pods),
            "inspected": self.inspected,
            "inspection_error": self.inspection_error,
            "findings": [f.to_dict() for f in self.findings],
        }
        if self.inspection_metadata:
            d["inspection_metadata"] = self.inspection_metadata
        return d


# ─────────────────────────────────────────────────────────────────────────────
# OpenShift system namespaces (skipped by default)
# ─────────────────────────────────────────────────────────────────────────────
OPENSHIFT_SYSTEM_NS = {
    "openshift", "openshift-apiserver", "openshift-apiserver-operator",
    "openshift-authentication", "openshift-authentication-operator",
    "openshift-cloud-controller-manager", "openshift-cloud-controller-manager-operator",
    "openshift-cloud-credential-operator", "openshift-cloud-network-config-controller",
    "openshift-cluster-csi-drivers", "openshift-cluster-machine-approver",
    "openshift-cluster-node-tuning-operator", "openshift-cluster-samples-operator",
    "openshift-cluster-storage-operator", "openshift-cluster-version",
    "openshift-config", "openshift-config-managed", "openshift-config-operator",
    "openshift-console", "openshift-console-operator", "openshift-console-user-settings",
    "openshift-controller-manager", "openshift-controller-manager-operator",
    "openshift-dns", "openshift-dns-operator",
    "openshift-etcd", "openshift-etcd-operator",
    "openshift-host-network", "openshift-image-registry", "openshift-infra",
    "openshift-ingress", "openshift-ingress-canary", "openshift-ingress-operator",
    "openshift-insights", "openshift-kni-infra",
    "openshift-kube-apiserver", "openshift-kube-apiserver-operator",
    "openshift-kube-controller-manager", "openshift-kube-controller-manager-operator",
    "openshift-kube-scheduler", "openshift-kube-scheduler-operator",
    "openshift-kube-storage-version-migrator", "openshift-kube-storage-version-migrator-operator",
    "openshift-machine-api", "openshift-machine-config-operator",
    "openshift-marketplace", "openshift-monitoring", "openshift-multus",
    "openshift-network-diagnostics", "openshift-network-node-identity",
    "openshift-network-operator", "openshift-node", "openshift-nutanix-infra",
    "openshift-oauth-apiserver", "openshift-openstack-infra",
    "openshift-operator-lifecycle-manager", "openshift-operators",
    "openshift-ovirt-infra", "openshift-route-controller-manager",
    "openshift-sdn", "openshift-ovn-kubernetes",
    "openshift-service-ca", "openshift-service-ca-operator",
    "openshift-user-workload-monitoring", "openshift-vsphere-infra",
    "openshift-catalogd", "openshift-cluster-olm-operator",
    "openshift-operator-controller", "openshift-operator-lifecycle-manager",
    "openshift-network-console", "openshift-network-operator",
    "openshift-pipelines", "openshift-gitops",
    "openshift-storage", "openshift-logging",
    "openshift-serverless", "openshift-service-mesh",
    "kube-system", "kube-public", "kube-node-lease", "default",
}


# ─────────────────────────────────────────────────────────────────────────────
# Scanner class
# ─────────────────────────────────────────────────────────────────────────────
class CGroupsV2Scanner:
    """Scans an OpenShift cluster for cgroups v2 compatibility issues.

    Detection is via pod exec (runs a script inside each pod).
    """

    _K8S_PAGE_SIZE = 500

    def __init__(
        self,
        namespaces: Optional[List[str]] = None,
        exclude_namespaces: Optional[List[str]] = None,
        namespace_patterns: Optional[List[str]] = None,
        exclude_patterns: Optional[List[str]] = None,
        skip_system_ns: bool = True,
        progress_callback=None,
    ):
        self.target_namespaces = namespaces
        self.exclude_namespaces = set(exclude_namespaces or [])
        self._ns_include_patterns = self._compile_patterns(namespace_patterns)
        self._ns_exclude_patterns = self._compile_patterns(exclude_patterns)
        self.skip_system_ns = skip_system_ns
        self.progress_callback = progress_callback

        self.image_reports: Dict[str, ImageReport] = {}
        self.cluster_info: Dict[str, str] = {}
        self._image_pod_map: Dict[str, Tuple[str, str, str]] = {}
        self._api_client: Optional[client.ApiClient] = None

    @staticmethod
    def _compile_patterns(patterns: Optional[List[str]]) -> List[re.Pattern]:
        """Compile a list of regex pattern strings, ignoring invalid ones."""
        compiled = []
        for p in (patterns or []):
            p = p.strip()
            if not p:
                continue
            try:
                compiled.append(re.compile(p))
            except re.error as e:
                logger.warning(f"Invalid regex pattern '{p}': {e}")
        return compiled

    def _namespace_included(self, ns: str) -> bool:
        if not self._ns_include_patterns:
            return True
        return any(p.search(ns) for p in self._ns_include_patterns)

    def _namespace_excluded(self, ns: str) -> bool:
        if ns in self.exclude_namespaces:
            return True
        return any(p.search(ns) for p in self._ns_exclude_patterns)

    # ─────────────────────────────────────────────────────────────────────
    # Progress reporting
    # ─────────────────────────────────────────────────────────────────────
    def _report_progress(self, stage: str, current: int, total: int, detail: str = ""):
        if self.progress_callback:
            self.progress_callback(stage, current, total, detail)

    # ─────────────────────────────────────────────────────────────────────
    # Cluster connection
    # ─────────────────────────────────────────────────────────────────────
    def connect(self):
        """Connect to the OpenShift cluster."""
        try:
            config.load_incluster_config()
            logger.info("Using in-cluster configuration (ServiceAccount).")
        except config.ConfigException:
            config.load_kube_config()
            logger.info("Using local kubeconfig.")

        cfg = client.Configuration.get_default_copy()
        cfg.proxy = ""
        proxy_env = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or \
                    os.environ.get("https_proxy") or os.environ.get("http_proxy")
        if proxy_env:
            logger.info("HTTP(S)_PROXY detected; forcing direct connection to K8s API (proxy disabled in Configuration).")
        api_client = client.ApiClient(configuration=cfg)

        self._api_client = api_client
        self.v1 = client.CoreV1Api(api_client)

        version_api = client.VersionApi(api_client)
        version = version_api.get_code()
        self.cluster_info["kubernetes_version"] = version.git_version
        logger.info(f"Connected: Kubernetes {version.git_version}")

        self._check_openshift()
        self._discover_internal_registry()
        self._check_node_info()

    def _check_openshift(self):
        try:
            custom = client.CustomObjectsApi(self._api_client)
            cv = custom.get_cluster_custom_object(
                "config.openshift.io", "v1", "clusterversions", "version",
            )
            ver = cv.get("status", {}).get("desired", {}).get("version", "unknown")
            self.cluster_info["openshift_version"] = ver
            logger.info(f"OpenShift {ver} detected.")
        except ApiException:
            self.cluster_info["openshift_version"] = "N/A (not OpenShift?)"

    def _discover_internal_registry(self):
        try:
            custom = client.CustomObjectsApi(self._api_client)
            ic = custom.get_cluster_custom_object(
                "config.openshift.io", "v1", "images", "cluster",
            )
            reg = ic.get("status", {}).get("internalRegistryHostname", "")
            if reg:
                self.cluster_info["internal_registry"] = reg
        except Exception:
            pass

    def _check_node_info(self):
        try:
            nodes = self.v1.list_node(limit=1)
            if nodes.items:
                node = nodes.items[0]
                self.cluster_info["node_os"] = node.status.node_info.os_image
                self.cluster_info["kubelet"] = node.status.node_info.kubelet_version
                self.cluster_info["container_runtime"] = node.status.node_info.container_runtime_version
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────────────
    # Kubernetes API paginated listing
    # ─────────────────────────────────────────────────────────────────────
    def _list_pods_paginated(self, namespace: Optional[str] = None) -> List:
        """List pods with pagination to avoid API timeouts on large clusters."""
        all_pods = []
        _continue = None

        while True:
            try:
                if namespace:
                    result = self.v1.list_namespaced_pod(
                        namespace=namespace,
                        limit=self._K8S_PAGE_SIZE,
                        _continue=_continue,
                    )
                else:
                    result = self.v1.list_pod_for_all_namespaces(
                        limit=self._K8S_PAGE_SIZE,
                        _continue=_continue,
                    )
            except ApiException as e:
                raise RuntimeError(f"Failed to list pods: {e}")

            all_pods.extend(result.items)
            _continue = result.metadata._continue
            if not _continue:
                break

        return all_pods

    # ─────────────────────────────────────────────────────────────────────
    # Image collection
    # ─────────────────────────────────────────────────────────────────────
    def collect_images(self):
        """Collect all container images from pods."""
        self._report_progress("collect", 0, 1, "Listing pods (paginated)...")

        if self.target_namespaces:
            all_pods = []
            for ns in self.target_namespaces:
                all_pods.extend(self._list_pods_paginated(namespace=ns))
        else:
            all_pods = self._list_pods_paginated()

        total_pods = 0
        skipped = 0
        skipped_excluded = 0
        skipped_system = 0
        skipped_pattern = 0
        skipped_not_running = 0

        for pod in all_pods:
            ns = pod.metadata.namespace
            if self._namespace_excluded(ns):
                skipped += 1
                skipped_excluded += 1
                continue
            if self.skip_system_ns and ns in OPENSHIFT_SYSTEM_NS:
                skipped += 1
                skipped_system += 1
                continue
            if not self._namespace_included(ns):
                skipped += 1
                skipped_pattern += 1
                continue

            is_running = bool(pod.status and pod.status.phase == "Running")
            if not is_running:
                skipped += 1
                skipped_not_running += 1
                continue

            running_containers = self._get_running_containers(pod)

            total_pods += 1
            pod_name = pod.metadata.name

            if pod.spec.init_containers:
                for c in pod.spec.init_containers:
                    if not c.image:
                        continue
                    img = self._normalize_image(c.image)
                    if img not in self.image_reports:
                        self.image_reports[img] = ImageReport(image=img)
                    r = self.image_reports[img]
                    r.namespaces.add(ns)
                    r.pods.add(f"{ns}/{pod_name}")
                    r.init_containers.add(c.name)

            if pod.spec.containers:
                for c in pod.spec.containers:
                    if not c.image:
                        continue
                    img = self._normalize_image(c.image)
                    if img not in self.image_reports:
                        self.image_reports[img] = ImageReport(image=img)
                    r = self.image_reports[img]
                    r.namespaces.add(ns)
                    r.pods.add(f"{ns}/{pod_name}")
                    r.containers.add(c.name)
                    if c.name in running_containers and img not in self._image_pod_map:
                        self._image_pod_map[img] = (ns, pod_name, c.name)

        for report in self.image_reports.values():
            if report.init_containers and not report.containers:
                report.only_in_init = True

        self.cluster_info["total_pods_scanned"] = str(total_pods)
        skip_parts = []
        if skipped_system > 0:
            skip_parts.append(f"system namespaces: {skipped_system}")
        if skipped_excluded > 0:
            skip_parts.append(f"excluded namespaces: {skipped_excluded}")
        if skipped_pattern > 0:
            skip_parts.append(f"pattern filter: {skipped_pattern}")
        if skipped_not_running > 0:
            skip_parts.append(f"not running: {skipped_not_running}")
        if skip_parts:
            self.cluster_info["pods_skipped"] = f"{skipped} ({', '.join(skip_parts)})"
        else:
            self.cluster_info["pods_skipped"] = str(skipped)
        self.cluster_info["unique_images"] = str(len(self.image_reports))

        self._report_progress("collect", 1, 1,
            f"{total_pods} pods, {len(self.image_reports)} unique images")
        logger.info(f"Collected {total_pods} pods, {len(self.image_reports)} unique images "
                     f"(skipped {skipped} pods)")

    @staticmethod
    def _get_running_containers(pod) -> Set[str]:
        """Return names of containers actually running (not CrashLoopBackOff, waiting, etc.)."""
        running = set()
        for cs in (pod.status.container_statuses or []):
            if cs.state and cs.state.running:
                running.add(cs.name)
        return running

    @staticmethod
    def _normalize_image(image: str) -> str:
        image = image.strip()
        if "@" not in image and ":" not in image.split("/")[-1]:
            image += ":latest"
        return image

    # ─────────────────────────────────────────────────────────────────────
    # Analysis
    # ─────────────────────────────────────────────────────────────────────
    def analyze(self):
        """Run full analysis on all collected images via pod exec."""
        self._exec_inspect_all()

    # ─────────────────────────────────────────────────────────────────────
    # Exec-based inspection
    # ─────────────────────────────────────────────────────────────────────
    def _exec_inspect_all(self):
        """Execute inspection script inside running pods.

        Picks one running pod per unique image and runs the comprehensive
        detection script that checks OS, runtimes, and cgroups v1 usage.
        """
        exec_targets = {
            img: pod_info
            for img, pod_info in self._image_pod_map.items()
            if img in self.image_reports
        }

        if not exec_targets:
            logger.info("Exec inspection: no running pods available")
            return

        total = len(exec_targets)
        logger.info(f"Exec inspection: {total} images to inspect "
                     f"({_EXEC_MAX_WORKERS} workers, {_EXEC_TIMEOUT}s timeout)")

        with ThreadPoolExecutor(max_workers=_EXEC_MAX_WORKERS) as executor:
            futures = {
                executor.submit(self._exec_inspect_one, img, ns, pod_name, container):
                img for img, (ns, pod_name, container) in exec_targets.items()
            }
            done = 0
            for future in as_completed(futures):
                done += 1
                img = futures[future]
                self._report_progress("inspecting", done, total,
                    f"Inspecting ({done}/{total}): {img[:60]}")
                try:
                    future.result()
                except Exception as e:
                    logger.debug(f"Exec inspection failed for {img}: {e}")

        inspected = sum(1 for r in self.image_reports.values() if r.inspected)
        with_findings = sum(1 for r in self.image_reports.values() if r.findings)
        self.cluster_info["exec_inspected"] = str(inspected)
        logger.info(f"Exec inspection: {inspected}/{total} inspected, {with_findings} with findings")

    def _exec_inspect_one(self, image: str, namespace: str, pod_name: str, container: str):
        """Execute the inspection script inside a single pod container."""
        report = self.image_reports[image]
        pod_ref = f"{namespace}/{pod_name}:{container}"

        try:
            output = k8s_stream(
                self.v1.connect_get_namespaced_pod_exec,
                pod_name, namespace,
                container=container,
                command=["/bin/sh", "-c", _EXEC_INSPECT_SCRIPT],
                stderr=True, stdin=False, stdout=True, tty=False,
                _request_timeout=_EXEC_TIMEOUT,
            )
        except ApiException as e:
            error = self._clean_exec_error(e)
            report.inspection_error = error
            if e.status != 403:
                logger.debug(f"Exec rejected for {pod_ref}: {error}")
            return
        except Exception as e:
            error = self._clean_exec_error(e)
            report.inspection_error = error
            logger.debug(f"Exec error for {pod_ref}: {error}")
            return

        if not output or "===DONE===" not in output:
            report.inspection_error = self._diagnose_exec_failure(output)
            logger.debug(f"Exec incomplete for {pod_ref}: {report.inspection_error}")
            return

        report.inspected = True
        self._parse_exec_inspection(report, output, namespace, pod_name, container)

    @staticmethod
    def _clean_exec_error(exc: Exception) -> str:
        """Extract a meaningful error from exec exceptions.

        The kubernetes client wraps websocket/HTTP errors with verbose
        headers and response bodies. This extracts just the useful part.
        For websocket handshake failures (ApiException status=0), the
        reason field contains the full HTTP response — parse it for
        known patterns before falling back to the generic message.
        """
        err_str = str(exc)

        if isinstance(exc, ApiException):
            if hasattr(exc, 'status') and hasattr(exc, 'reason'):
                if exc.status == 403:
                    return "exec forbidden (RBAC)"
                if exc.status == 404:
                    return "pod no longer running"

                reason = exc.reason or ""

                # Websocket handshake failures (status=0) carry the real
                # error buried in the reason/body. Check known patterns
                # before returning the raw message.
                m = re.search(r'container not found', reason)
                if m:
                    cn = re.search(r'container not found \(\\?"([^"\\]+)\\?"\)', reason)
                    name = cn.group(1) if cn else "unknown"
                    return f'container not found ("{name}")'

                if "connection refused" in reason:
                    m = re.search(r'dial tcp ([^\s:]+:\d+)', reason)
                    addr = m.group(1) if m else "node"
                    return f"node kubelet unreachable ({addr})"

                if "not found" in reason.lower() and "pod" in reason.lower():
                    return "pod no longer running"

                # For status=0 (websocket errors), don't dump the full reason
                if exc.status == 0:
                    m = re.search(r'Handshake status (\d+)', reason)
                    if m:
                        return f"exec handshake error (HTTP {m.group(1)})"
                    return f"exec failed (websocket error)"

                return f"API error: {exc.status} {reason[:100]}"

        # Parse "container not found" from non-ApiException errors
        m = re.search(r'container not found \("([^"]+)"\)', err_str)
        if m:
            return f'container not found ("{m.group(1)}")'

        # Parse "pods not found" from 404 responses
        m = re.search(r'"pods\s*\\*"([^"\\]+)\\*"\s*not found"', err_str)
        if m:
            return "pod no longer running"
        if "not found" in err_str.lower() and "pod" in err_str.lower():
            return "pod no longer running"

        # Connection refused
        if "connection refused" in err_str.lower():
            return "node kubelet unreachable"

        # Handshake status errors
        m = re.search(r'Handshake status (\d+)', err_str)
        if m:
            status = m.group(1)
            if status == "500":
                return "exec failed (container may not be running)"
            return f"exec handshake error (HTTP {status})"

        # Timeout
        if "timed out" in err_str.lower() or "timeout" in err_str.lower():
            return "exec timeout"

        # Fallback: truncate to something reasonable
        return err_str[:150] if len(err_str) > 150 else err_str

    @staticmethod
    def _diagnose_exec_failure(output: str) -> str:
        """Produce a clear message when exec output is missing or incomplete.

        Common cause: distroless / scratch images with no /bin/sh.
        The OCI runtime returns "no such file or directory" or similar,
        which may appear in the captured output or as empty output.
        """
        if not output:
            return (
                "Distroless image — no shell available. "
                "The container has no /bin/sh, so it cannot be inspected via exec. "
                "Typically Go/Rust/static binaries without cgroups v2 concerns."
            )

        lower = output.lower()
        if "no such file or directory" in lower or "executable file not found" in lower:
            return (
                "Distroless image — no shell available. "
                "The container has no /bin/sh, so it cannot be inspected via exec. "
                "Typically Go/Rust/static binaries without cgroups v2 concerns."
            )

        if "permission denied" in lower:
            return (
                "Shell execution denied — the container's security context "
                "or read-only filesystem prevented exec."
            )

        return (
            "Exec returned incomplete output — the container shell may be "
            "restricted or the image may be minimal/distroless."
        )

    # ─────────────────────────────────────────────────────────────────────
    # Exec output parsing
    # ─────────────────────────────────────────────────────────────────────
    def _parse_exec_inspection(self, report: ImageReport, output: str,
                                namespace: str, pod_name: str, container: str):
        """Parse the comprehensive exec inspection output."""
        sections = {}
        current_section = None
        current_lines = []

        for line in output.splitlines():
            line = line.strip()
            if line.startswith("===") and line.endswith("==="):
                if current_section:
                    sections[current_section] = current_lines
                current_section = line.strip("=")
                current_lines = []
            elif current_section:
                if line:
                    current_lines.append(line)

        if current_section:
            sections[current_section] = current_lines

        pod_ref = f"{namespace}/{pod_name}:{container}"
        metadata: Dict = {}

        # OS detection
        self._parse_os_info(report, sections.get("OS", []), metadata)

        # Java version
        self._parse_java_version(report, sections.get("JAVA", []), pod_ref, metadata)

        # JVM flags
        self._parse_jvm_flags(report, sections.get("JVMFLAGS", []))

        # Node.js version
        self._parse_node_version(report, sections.get("NODE", []), metadata)

        # .NET version
        self._parse_dotnet_version(report, sections.get("DOTNET", []), metadata)

        # cgroups v1 file references
        v1_refs = sections.get("V1REFS", [])
        if v1_refs:
            unique_refs = sorted(set(v1_refs))
            report.findings.append(Finding(
                "cgroups v1 Reference", "HIGH",
                "Application files reference cgroups v1 paths inside the container",
                "Update the application to use cgroups v2 unified hierarchy paths. "
                "Example: /sys/fs/cgroup/memory.max instead of "
                "/sys/fs/cgroup/memory/memory.limit_in_bytes",
                f"Files in {pod_ref}: {', '.join(unique_refs[:10])}"
                + (f" (+{len(unique_refs) - 10} more)" if len(unique_refs) > 10 else ""),
            ))

        # cgroups v1 in environment variables
        env_refs = sections.get("ENVREF", [])
        cgroup_v1_env = [e for e in env_refs if any(
            v1 in e for v1 in ["/sys/fs/cgroup/memory/", "/sys/fs/cgroup/cpu/",
                                "memory.limit_in_bytes", "cpu.cfs_quota_us",
                                "cpu.shares", "cpuacct."]
        )]
        if cgroup_v1_env:
            report.findings.append(Finding(
                "cgroups v1 Reference", "HIGH",
                "Environment variables reference cgroups v1 paths",
                "Update environment variables to use cgroups v2 paths.",
                f"Pod {pod_ref}: {'; '.join(cgroup_v1_env[:5])}",
            ))

        # Store cgroups type info in metadata
        cg_type = sections.get("TYPE", [])
        if cg_type:
            metadata["cgroups_type"] = cg_type[0]

        v1_dirs = sections.get("V1DIRS", [])
        if v1_dirs:
            metadata["v1_dirs_found"] = len(v1_dirs)

        report.inspection_metadata = metadata

    def _parse_os_info(self, report: ImageReport, lines: List[str], metadata: Dict):
        """Parse /etc/os-release output and detect problematic OS versions."""
        if not lines or lines == ["unknown"]:
            return

        os_info = {}
        for line in lines:
            if "=" in line:
                key, val = line.split("=", 1)
                os_info[key] = val.strip('"')

        os_id = os_info.get("ID", "").lower()
        version_id = os_info.get("VERSION_ID", "")
        pretty_name = os_info.get("PRETTY_NAME", "")

        metadata["os_id"] = os_id
        metadata["os_version"] = version_id
        if pretty_name:
            metadata["os_pretty_name"] = pretty_name

        # Check major version only (e.g. "7" from "7.9.2009" or "7")
        major_version = version_id.split(".")[0] if version_id else ""

        for (check_id, check_ver), (name, sev, rec) in KNOWN_PROBLEMATIC_OS.items():
            if os_id == check_id and major_version == check_ver:
                report.findings.append(Finding(
                    "Operating System", sev,
                    f"{name} detected — no cgroups v2 support",
                    rec,
                    f"OS: {pretty_name or f'{os_id} {version_id}'}",
                ))
                return

        # Ubuntu < 20.04
        if os_id == "ubuntu" and version_id:
            try:
                ubuntu_ver = float(version_id)
                if ubuntu_ver < 20.04:
                    report.findings.append(Finding(
                        "Operating System", "CRITICAL",
                        f"Ubuntu {version_id} — limited cgroups v2 support",
                        "Migrate to Ubuntu 22.04+ or UBI 8/9.",
                        f"OS: {pretty_name or f'Ubuntu {version_id}'}",
                    ))
            except ValueError:
                pass

        # Debian < 11
        if os_id == "debian" and major_version:
            try:
                if int(major_version) < 11:
                    report.findings.append(Finding(
                        "Operating System", "CRITICAL",
                        f"Debian {version_id} — limited cgroups v2 support",
                        "Migrate to Debian 11+ or UBI 8/9.",
                        f"OS: {pretty_name or f'Debian {version_id}'}",
                    ))
            except ValueError:
                pass

        # Alpine < 3.14
        if os_id == "alpine" and version_id:
            try:
                parts = version_id.split(".")
                alpine_major, alpine_minor = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
                if alpine_major == 3 and alpine_minor < 14:
                    report.findings.append(Finding(
                        "Operating System", "CRITICAL",
                        f"Alpine {version_id} — requires upgrade for cgroups v2",
                        "Migrate to Alpine 3.14+ or UBI 8/9.",
                        f"OS: {pretty_name or f'Alpine {version_id}'}",
                    ))
            except (ValueError, IndexError):
                pass

    def _parse_java_version(self, report: ImageReport, lines: List[str],
                            pod_ref: str, metadata: Dict):
        """Parse java -version output and determine cgroups v2 safety."""
        if not lines or lines == ["NOT_FOUND"]:
            return

        version_output = "\n".join(lines)

        # Parse: openjdk version "1.8.0_412" or java version "17.0.2"
        m = re.search(r'version "([^"]+)"', version_output)
        if not m:
            metadata["java_detected"] = True
            metadata["java_parse_error"] = version_output[:100]
            return

        real_version = m.group(1)
        metadata["java_version"] = real_version

        # Determine major, minor, patch
        m_old = re.match(r"1\.(\d+)\.0[_\-](\d+)", real_version)
        m_new = re.match(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", real_version)
        major = minor = patch = 0

        if m_old:
            major, patch = int(m_old.group(1)), int(m_old.group(2))
        elif m_new:
            major = int(m_new.group(1))
            minor = int(m_new.group(2) or 0)
            patch = int(m_new.group(3) or 0)

        if major == 0:
            return

        safe = (major >= 15 or
                (major == 11 and (minor > 0 or patch >= 16)) or
                (major == 8 and patch >= 372))

        metadata["java_cgroups_v2_safe"] = safe

        if not safe:
            report.findings.append(Finding(
                "Java Runtime", "HIGH",
                f"Java {real_version} — does not support cgroups v2",
                f"{JAVA_DETAIL} Safe versions: {JAVA_SAFE_VERSIONS}",
                f"Verified via 'java -version' in pod {pod_ref}",
            ))

    def _parse_jvm_flags(self, report: ImageReport, lines: List[str]):
        """Check JVM environment variables for dangerous flags."""
        if not lines:
            return

        all_flags = " ".join(lines)
        if "-UseContainerSupport" in all_flags and ":-UseContainerSupport" in all_flags:
            report.findings.append(Finding(
                "JVM Configuration", "HIGH",
                "-XX:-UseContainerSupport disables container resource detection",
                "Remove this flag. JVM needs UseContainerSupport enabled for cgroups v2. "
                "Without it, the JVM uses host memory/CPU limits instead of container limits.",
            ))

    def _parse_node_version(self, report: ImageReport, lines: List[str], metadata: Dict):
        """Parse node --version output."""
        if not lines or lines == ["NOT_FOUND"]:
            return

        version_str = lines[0].strip().lstrip("v")
        metadata["node_version"] = version_str

        m = re.match(r"(\d+)", version_str)
        if not m:
            return

        node_major = int(m.group(1))
        if node_major < 16:
            report.findings.append(Finding(
                "Node.js Runtime", "HIGH",
                f"Node.js {version_str} — no cgroups v2 support",
                "Migrate to Node.js 20+ (recommended: Node.js 22+). "
                "Memory heap calculation will use host limits, not container limits.",
            ))
        elif node_major < 20:
            report.findings.append(Finding(
                "Node.js Runtime", "HIGH",
                f"Node.js {version_str} — limited cgroups v2 support",
                "Migrate to Node.js 20+ for full cgroups v2 support. "
                "Node.js 22+ recommended for improved container memory management.",
            ))

    def _parse_dotnet_version(self, report: ImageReport, lines: List[str], metadata: Dict):
        """Parse dotnet --list-runtimes output."""
        if not lines or lines == ["NOT_FOUND"]:
            return

        # Parse lines like: "Microsoft.NETCore.App 6.0.25 [/usr/share/dotnet/...]"
        for line in lines:
            m = re.match(r"Microsoft\.NETCore\.App\s+(\d+)\.(\d+)", line)
            if m:
                dotnet_major = int(m.group(1))
                metadata["dotnet_version"] = f"{m.group(1)}.{m.group(2)}"
                if dotnet_major < 5:
                    report.findings.append(Finding(
                        ".NET Runtime", "HIGH",
                        f".NET {m.group(1)}.{m.group(2)} — no cgroups v2 support",
                        "Migrate to .NET 5+ (recommended: .NET 8+). "
                        ".NET 5+ has full cgroups v2 compatibility since 2020.",
                    ))
                return

    # ─────────────────────────────────────────────────────────────────────
    # Report generation
    # ─────────────────────────────────────────────────────────────────────
    def get_summary(self) -> dict:
        """Return scan summary as a dictionary."""
        by_severity = defaultdict(int)
        for r in self.image_reports.values():
            by_severity[r.max_severity] += 1

        init_only = sum(1 for r in self.image_reports.values() if r.only_in_init)
        inspected = sum(1 for r in self.image_reports.values() if r.inspected)
        inspection_errors = sum(1 for r in self.image_reports.values() if r.inspection_error)

        summary = {
            "generated_at": datetime.now().isoformat(),
            "cluster_info": self.cluster_info,
            "total_images": len(self.image_reports),
            "init_only_images": init_only,
            "inspected": inspected,
            "inspection_errors": inspection_errors,
            "by_severity": dict(by_severity),
        }

        ocp_ver = self.cluster_info.get("openshift_version", "")
        summary["cgroups_context"] = self._get_cgroups_context(ocp_ver)

        return summary

    @staticmethod
    def _get_cgroups_context(ocp_version: str) -> dict:
        """Provide cgroups v1/v2 context based on the detected OCP version."""
        ctx = {"version": ocp_version, "risk_level": "unknown", "detail": ""}
        if not ocp_version or ocp_version.startswith("N/A"):
            ctx["detail"] = "Could not detect OpenShift version."
            return ctx

        m = re.match(r"(\d+)\.(\d+)", ocp_version)
        if not m:
            return ctx

        major, minor = int(m.group(1)), int(m.group(2))
        if major != 4:
            return ctx

        if minor <= 13:
            ctx["risk_level"] = "low"
            ctx["cgroups_default"] = "v1"
            ctx["v1_support"] = True
            ctx["v2_support"] = False
            ctx["detail"] = (
                f"OCP {ocp_version} uses cgroups v1 only. "
                "No immediate cgroups v2 concern, but plan for future upgrades."
            )
        elif minor <= 18:
            ctx["risk_level"] = "moderate"
            ctx["cgroups_default"] = "v2 (new installs since 4.14)"
            ctx["v1_support"] = True
            ctx["v2_support"] = True
            ctx["detail"] = (
                f"OCP {ocp_version} supports both cgroups v1 and v2. "
                "New installations default to v2 since 4.14. Upgrades keep previous setting. "
                "Prepare applications for cgroups v2 before moving to 4.19+."
            )
        else:  # 4.19+
            ctx["risk_level"] = "high"
            ctx["cgroups_default"] = "v2"
            ctx["v1_support"] = False
            ctx["v2_support"] = True
            ctx["detail"] = (
                f"OCP {ocp_version} requires cgroups v2 — v1 is removed. "
                "All applications MUST be cgroups v2 compatible. "
                "Incompatible apps will use host limits, causing OOMKill and CPU throttling."
            )

        return ctx

    def get_full_report(self) -> dict:
        """Return the full report as a serializable dictionary."""
        summary = self.get_summary()
        sorted_reports = sorted(self.image_reports.values(), key=lambda r: r.severity_sort_key)
        summary["images"] = [r.to_dict() for r in sorted_reports]
        return summary
