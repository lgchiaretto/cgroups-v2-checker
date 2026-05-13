"""
cgroups v2 Compatibility Scanner Engine
========================================
Scans OpenShift clusters for container images that may have cgroups v2
compatibility issues. Uses skopeo for remote metadata inspection (no layer pull).
"""

import base64
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
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

KNOWN_PROBLEMATIC_BASES = {
    r"(^|/)rhel7|redhat/ubi7|centos:7|centos/centos7|centos7": (
        "RHEL/CentOS 7", "CRITICAL",
        "Migrate to UBI 8/9 or RHEL 8/9. RHEL 7 does not support cgroups v2.",
    ),
    r"centos:6|centos/centos6|centos6|rhel6": (
        "RHEL/CentOS 6", "CRITICAL",
        "Urgently migrate to UBI 8/9. RHEL 6 is EOL and has no cgroups v2 support.",
    ),
    r"ubuntu:(14\.|16\.|18\.)": (
        "Ubuntu < 20.04", "HIGH",
        "Migrate to Ubuntu 20.04+ (Focal) or 22.04+ (Jammy).",
    ),
    r"debian:(8|9|10|jessie|stretch|buster)": (
        "Debian < 11 (Bullseye)", "HIGH",
        "Migrate to Debian 11 (Bullseye) or later.",
    ),
    r"alpine:(3\.[0-9](?:\.|$)|3\.1[0-3])": (
        "Alpine < 3.14", "MEDIUM",
        "Consider migrating to Alpine 3.14+.",
    ),
    r"amazonlinux:1|amzn1": (
        "Amazon Linux 1", "CRITICAL",
        "Migrate to Amazon Linux 2023. AL1 is EOL.",
    ),
    r"amazonlinux:2(?!\d)|amzn2": (
        "Amazon Linux 2", "MEDIUM",
        "Check compatibility. Consider Amazon Linux 2023.",
    ),
    r"oraclelinux:7|ol7": (
        "Oracle Linux 7", "CRITICAL",
        "Migrate to Oracle Linux 8/9.",
    ),
    r"sles:12|suse.*:12": (
        "SLES 12", "HIGH",
        "Migrate to SLES 15.",
    ),
}

JAVA_SAFE_VERSIONS = "JDK 17+, JDK 11.0.16+, JDK 8u372+"
JAVA_DETAIL = (
    "JDK without cgroups v2 support does not recognize container memory/CPU limits. "
    "Memory impact: heap decouples from container size, causing OOMKill by the kernel. "
    "CPU impact: thread pools (including GC threads) are calculated from host CPU count, "
    "causing excessive threads, CPU throttling, and latency. "
    "See: developers.redhat.com/articles/2025/11/27/how-does-cgroups-v2-impact-java-net-and-nodejs-openshift-4"
)

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
# Pod exec cgroups v1 detection script
# ─────────────────────────────────────────────────────────────────────────────
# This script is executed inside running pods to detect cgroups v1 usage
# at runtime. It checks:
# 1. Whether the pod runs under cgroups v1 or v2 hierarchy
# 2. Which v1-specific directories exist in /sys/fs/cgroup
# 3. Application files that reference v1-specific paths (with timeout & depth limit)
# 4. Environment variables referencing cgroups v1
#
# Performance: script uses a single combined grep with maxdepth=3, size<1MB,
# and a 5-second timeout to avoid hanging on large containers.
_EXEC_CHECK_SCRIPT = (
    'echo "===TYPE===";'
    '[ -f /sys/fs/cgroup/cgroup.controllers ] && echo "v2" || echo "v1";'
    'echo "===V1DIRS===";'
    'for d in memory cpu cpuacct blkio pids devices freezer cpuset '
    'hugetlb net_cls net_prio perf_event; do'
    ' [ -d "/sys/fs/cgroup/$d" ] && echo "/sys/fs/cgroup/$d";'
    'done;'
    'echo "===V1REFS===";'
    # Single find+grep: maxdepth 3, text files <1MB, 5s timeout
    'timeout 5 find /etc /opt /app /home -maxdepth 3 -type f -size -1M '
    '-exec grep -lE "sys/fs/cgroup/(memory|cpu|cpuacct|blkio)/|'
    'memory\\.limit_in_bytes|cpu\\.cfs_quota_us|cpu\\.shares|cpuacct\\." {} + '
    '2>/dev/null | head -20 || true;'
    'echo "===ENVREF===";'
    'cat /proc/1/environ 2>/dev/null | tr "\\0" "\\n" | grep -i cgroup 2>/dev/null || true;'
    'echo "===DONE==="'
)

_EXEC_MAX_WORKERS = int(os.environ.get("EXEC_MAX_WORKERS", "10"))
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
    exec_checked: bool = False
    only_in_init: bool = False
    inspection_metadata: Dict = field(default_factory=dict)

    @property
    def max_severity(self) -> str:
        order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
        if not self.findings:
            if not self.inspected and not self.exec_checked:
                return "UNKNOWN"
            return "OK"
        return min(self.findings, key=lambda f: order.get(f.severity, 99)).severity

    @property
    def severity_sort_key(self) -> int:
        order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4, "UNKNOWN": 5, "OK": 6}
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
            "exec_checked": self.exec_checked,
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
    """Scans an OpenShift cluster for cgroups v2 compatibility issues."""

    # Kubernetes API pagination limit (avoids large single responses on big clusters)
    _K8S_PAGE_SIZE = 500

    def __init__(
        self,
        namespaces: Optional[List[str]] = None,
        exclude_namespaces: Optional[List[str]] = None,
        namespace_patterns: Optional[List[str]] = None,
        exclude_patterns: Optional[List[str]] = None,
        skip_system_ns: bool = True,
        inspect_images: bool = True,
        exec_check: bool = False,
        skopeo_tls_verify: bool = True,
        skopeo_auth_file: str = "",
        max_workers: int = 20,
        progress_callback=None,
        use_image_pull_secrets: bool = True,
    ):
        self.target_namespaces = namespaces
        self.exclude_namespaces = set(exclude_namespaces or [])
        # Compiled regex patterns for namespace include/exclude
        self._ns_include_patterns = self._compile_patterns(namespace_patterns)
        self._ns_exclude_patterns = self._compile_patterns(exclude_patterns)
        self.skip_system_ns = skip_system_ns
        self.inspect_images = inspect_images
        self.exec_check = exec_check
        self.skopeo_tls_verify = skopeo_tls_verify
        self.skopeo_auth_file = skopeo_auth_file
        self.max_workers = max_workers
        self.progress_callback = progress_callback
        self.use_image_pull_secrets = use_image_pull_secrets

        self.image_reports: Dict[str, ImageReport] = {}
        self.cluster_info: Dict[str, str] = {}
        self._sa_token: Optional[str] = None
        self._internal_registry: Optional[str] = None
        # Registry -> auth credentials extracted from ImagePullSecrets
        self._registry_auths: Dict[str, dict] = {}
        # Tracks which image pull secrets have already been processed
        self._processed_pull_secrets: Set[str] = set()
        # Map image -> (namespace, pod_name, container_name) for exec checks
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
        """Check if namespace matches include patterns (if any are defined)."""
        if not self._ns_include_patterns:
            return True  # no include patterns = include all
        return any(p.search(ns) for p in self._ns_include_patterns)

    def _namespace_excluded(self, ns: str) -> bool:
        """Check if namespace matches exclude patterns."""
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
            self._load_sa_token()
        except config.ConfigException:
            config.load_kube_config()
            logger.info("Using local kubeconfig.")

        # Kubernetes client >= 28 falls back to HTTPS_PROXY env var when
        # Configuration.proxy is None. urllib3 may also read proxy env vars
        # at request time (not just at client creation). Setting proxy to ""
        # explicitly disables proxy at the Configuration level, which is
        # authoritative regardless of env vars.
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

    def _load_sa_token(self):
        token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
        try:
            if os.path.exists(token_path):
                with open(token_path) as f:
                    self._sa_token = f.read().strip()
        except Exception:
            pass

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
                self._internal_registry = reg
                self.cluster_info["internal_registry"] = reg
        except Exception:
            self._internal_registry = "image-registry.openshift-image-registry.svc:5000"

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
        """List pods with pagination to avoid API timeouts on large clusters.

        Uses continue tokens to paginate through results in batches of
        _K8S_PAGE_SIZE. This prevents overwhelming the API server when
        clusters have thousands of pods (>500 nodes).
        """
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
    # ImagePullSecrets extraction
    # ─────────────────────────────────────────────────────────────────────
    def _extract_pull_secrets_for_pod(self, pod) -> None:
        """Extract registry credentials from the pod's ImagePullSecrets.

        Reads the ImagePullSecrets referenced by the pod (including those
        inherited from its ServiceAccount) and merges their .dockerconfigjson
        credentials into _registry_auths for use by skopeo.
        """
        if not self.use_image_pull_secrets:
            return

        ns = pod.metadata.namespace
        secret_refs = []

        # Direct imagePullSecrets on the pod spec
        if pod.spec.image_pull_secrets:
            secret_refs.extend(pod.spec.image_pull_secrets)

        # ImagePullSecrets from the pod's ServiceAccount
        sa_name = pod.spec.service_account_name or "default"
        try:
            sa = self.v1.read_namespaced_service_account(sa_name, ns)
            if sa.image_pull_secrets:
                secret_refs.extend(sa.image_pull_secrets)
        except ApiException:
            pass

        for ref in secret_refs:
            secret_key = f"{ns}/{ref.name}"
            if secret_key in self._processed_pull_secrets:
                continue
            self._processed_pull_secrets.add(secret_key)

            try:
                secret = self.v1.read_namespaced_secret(ref.name, ns)
                if not secret.data:
                    continue

                # Support both .dockerconfigjson and .dockercfg formats
                raw = (
                    secret.data.get(".dockerconfigjson")
                    or secret.data.get(".dockercfg")
                )
                if not raw:
                    continue

                docker_config = json.loads(base64.b64decode(raw))
                auths = docker_config.get("auths", docker_config)
                for registry, creds in auths.items():
                    if registry not in self._registry_auths:
                        self._registry_auths[registry] = creds
                        logger.debug(f"Loaded pull secret for registry: {registry}")
            except (ApiException, json.JSONDecodeError, Exception) as e:
                logger.debug(f"Could not read pull secret {secret_key}: {e}")

    def _build_auth_file_for_image(self, image: str) -> Optional[str]:
        """Build a temporary auth file for skopeo if we have credentials for the image's registry.

        Returns the path to the temp auth file, or None if no matching credentials found.
        The caller is responsible for cleaning up the temp file.
        """
        if not self._registry_auths:
            return None

        # Extract registry hostname from image reference
        registry = self._get_registry_from_image(image)
        if not registry:
            return None

        # Look for matching credentials (try exact match, then with/without port)
        creds = None
        for reg_key, reg_creds in self._registry_auths.items():
            # Normalize: strip https:// prefixes for comparison
            normalized_key = reg_key.replace("https://", "").replace("http://", "").rstrip("/")
            if registry == normalized_key or registry.split(":")[0] == normalized_key.split(":")[0]:
                creds = reg_creds
                break

        if not creds:
            return None

        # Write temporary auth file in dockerconfigjson format
        auth_data = {"auths": {registry: creds}}
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(auth_data, tmp)
        tmp.close()
        return tmp.name

    @staticmethod
    def _get_registry_from_image(image: str) -> Optional[str]:
        """Extract registry hostname from an image reference."""
        # Remove tag/digest
        ref = image.split("@")[0].split(":")[0] if "@" in image else image.rsplit(":", 1)[0]
        parts = ref.split("/")
        # Images like "nginx" or "library/nginx" are Docker Hub
        if len(parts) == 1:
            return "docker.io"
        # If first part has a dot or colon, it is a registry
        if "." in parts[0] or ":" in parts[0]:
            return parts[0]
        # Otherwise assume Docker Hub (e.g., "myuser/myimage")
        return "docker.io"

    # ─────────────────────────────────────────────────────────────────────
    # Image collection
    # ─────────────────────────────────────────────────────────────────────
    def collect_images(self):
        """Collect all container images from pods.

        Uses paginated API calls to handle large clusters efficiently.
        Tracks initContainers separately from regular containers so that
        findings can indicate reduced severity for init-only images.
        Also extracts ImagePullSecrets for use by skopeo.
        """
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

            # Only consider pods in Running phase. Pods in Pending, Succeeded,
            # Failed or Unknown phases are skipped to avoid noise from images
            # that aren't actively running on the cluster.
            is_running = bool(pod.status and pod.status.phase == "Running")
            if not is_running:
                skipped += 1
                skipped_not_running += 1
                continue

            total_pods += 1
            pod_name = pod.metadata.name

            # Extract ImagePullSecrets for registry authentication
            self._extract_pull_secrets_for_pod(pod)

            # Process initContainers (tracked separately for severity differentiation)
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

            # Process regular containers
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
                    # Track first running pod per image for exec checks
                    if is_running and img not in self._image_pod_map:
                        self._image_pod_map[img] = (ns, pod_name, c.name)

        # Mark images that appear ONLY in initContainers
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
        self.cluster_info["registry_credentials"] = str(len(self._registry_auths))

        self._report_progress("collect", 1, 1,
            f"{total_pods} pods, {len(self.image_reports)} unique images")
        logger.info(f"Collected {total_pods} pods, {len(self.image_reports)} unique images "
                     f"(skipped {skipped} system pods, "
                     f"{len(self._registry_auths)} registry credentials loaded)")

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
        """Run full analysis on all collected images."""
        total = len(self.image_reports)

        # Level 1: name/tag analysis
        for idx, (img, report) in enumerate(self.image_reports.items(), 1):
            self._report_progress("analyze", idx, total, f"Name analysis: {img[:60]}")
            self._analyze_name(report)

        # Level 2: skopeo remote inspection (with cache)
        if self.inspect_images and self._check_skopeo():
            self._skopeo_inspect_all()

        # Level 3: pod exec cgroups v1 runtime detection
        if self.exec_check:
            self._exec_check_all()

        # Post-processing: reduce severity for initContainer-only images
        self._apply_init_container_severity_downgrade()

    def _check_skopeo(self) -> bool:
        try:
            result = subprocess.run(
                ["skopeo", "--version"], capture_output=True, text=True, timeout=10,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            logger.warning("skopeo not found — remote inspection disabled.")
            return False

    # ── Level 1: name/tag analysis ──────────────────────────────────────
    def _analyze_name(self, report: ImageReport):
        image = report.image.lower()
        self._check_base_image(report, image)
        self._check_runtime_versions(report, image)
        self._check_known_problematic(report, image)

    def _check_base_image(self, report: ImageReport, img: str):
        for pattern, (name, sev, rec) in KNOWN_PROBLEMATIC_BASES.items():
            if re.search(pattern, img):
                report.findings.append(Finding("Base Image", sev,
                    f"Base image identified as {name}", rec,
                    f"Image: {report.image}"))

    def _check_runtime_versions(self, report: ImageReport, img: str):
        # Java
        m = re.search(
            r"(?:openjdk|jdk|java|eclipse-temurin|amazoncorretto|liberica|zulu)"
            r"[:\-_]?(\d+)(?:[.\-_](\d+))?(?:[.\-_](\d+))?", img)
        if m:
            major, minor, patch = int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0)
            safe = (major >= 15 or
                    (major == 11 and (minor > 0 or patch >= 16)) or
                    (major == 8 and patch >= 372))
            if not safe:
                sev = "HIGH" if major < 11 else "MEDIUM"
                report.findings.append(Finding("Java Runtime", sev,
                    f"Java {major} detected — may not recognize cgroups v2 limits",
                    f"{JAVA_DETAIL} Safe versions: {JAVA_SAFE_VERSIONS}",
                    f"Tag version: {m.group(0)}"))

        # .NET — .NET 5+ is fully cgroups v2 compatible (since 2020)
        m = re.search(r"(?:dotnet|aspnet|dotnet-runtime)[:\-/](\d+)\.(\d+)", img)
        if m and int(m.group(1)) < 5:
            report.findings.append(Finding(".NET Runtime", "HIGH",
                f".NET {m.group(1)}.{m.group(2)} — no cgroups v2 support",
                "Migrate to .NET 5+ (recommended: .NET 8+). "
                ".NET 5+ has full cgroups v2 compatibility since 2020."))

        # Node.js — Node.js 20+ is fully cgroups v2 compatible;
        # Node.js 22+ has additional memory management improvements
        m = re.search(r"node[:\-_](\d+)", img)
        if m:
            node_major = int(m.group(1))
            if node_major < 16:
                report.findings.append(Finding("Node.js Runtime", "HIGH",
                    f"Node.js {node_major} — no cgroups v2 support",
                    "Migrate to Node.js 20+ (recommended: Node.js 22+). "
                    "Memory heap calculation will use host limits, not container limits."))
            elif node_major < 20:
                report.findings.append(Finding("Node.js Runtime", "MEDIUM",
                    f"Node.js {node_major} — limited cgroups v2 support",
                    "Migrate to Node.js 20+ for full cgroups v2 support. "
                    "Node.js 22+ recommended for improved container memory management."))

        # Python
        m = re.search(r"python[:\-_](\d+)\.(\d+)", img)
        if m and int(m.group(1)) == 2:
            report.findings.append(Finding("Python Runtime", "MEDIUM",
                "Python 2 detected — EOL", "Migrate to Python 3.8+."))

        # Go
        m = re.search(r"golang[:\-_](\d+)\.(\d+)", img)
        if m and int(m.group(1)) == 1 and int(m.group(2)) < 19:
            report.findings.append(Finding("Go Runtime", "LOW",
                f"Go 1.{m.group(2)} — cgroups v2 GOMAXPROCS improved in 1.19+",
                "Consider Go 1.19+."))

    def _check_known_problematic(self, report: ImageReport, img: str):
        checks = [
            (r"mysql[:\-_/](5\.[0-6]|5\.7\.[0-2]\d(?:\D|$))", "Database", "MEDIUM",
             "Old MySQL — cgroups v2 memory limit issues", "Update to MySQL 8.0+."),
            (r"postgres(?:ql)?[:\-_/](9\.|10\.|11\.)", "Database", "LOW",
             "Old PostgreSQL — check base image OS", "Update to PostgreSQL 14+."),
            (r"elasticsearch[:\-_/](5\.|6\.|7\.[0-9](?:\.|$))", "Search Engine", "HIGH",
             "Old Elasticsearch — JVM may not support cgroups v2", "Update to Elasticsearch 8.x."),
            (r"kafka[:\-_/](2\.[0-5]|1\.|0\.)", "Messaging", "MEDIUM",
             "Old Kafka — internal JVM may not support cgroups v2", "Update to Kafka 3.x+."),
            (r"jenkins.*(?:jdk8|jdk11(?!\.0\.[2-9]\d))", "CI/CD", "HIGH",
             "Jenkins with old JDK — memory issues with cgroups v2", "Use Jenkins with JDK 17+."),
            (r"wildfly[:\-_/](8|9|10|11|12|13|14|15|16)\.", "App Server", "HIGH",
             "Old WildFly — JVM likely without cgroups v2 support", "Update to WildFly 26+."),
            (r"tomcat[:\-_/](7\.|8\.0|8\.5\.[0-6]\d(?:\D|$))", "App Server", "MEDIUM",
             "Old Tomcat — check JDK for cgroups v2 compatibility", "Use Tomcat 10+ with JDK 17+."),
            # Red Hat middleware — scripts may be cgroups v1-only
            (r"jboss-eap-7[:\-_/](7\.0|7\.1|7\.2|7\.3)(?:\.|$)", "App Server", "HIGH",
             "JBoss EAP 7 (old) — init scripts may be cgroups v1-only, causing Xmx miscalculation",
             "Upgrade to latest JBoss EAP 7.4.x+ or EAP 8. EAP 8 relies on OpenJDK for cgroups detection."),
            (r"eap-xp[:\-_/][12]\.", "App Server", "MEDIUM",
             "JBoss EAP XP (old) — check init scripts for cgroups v1 assumptions",
             "Upgrade to latest EAP XP or EAP 8."),
            (r"datagrid-8-rhel8|datagrid[:\-_/]8\.([0-2]\.|3\.[0-6])", "Data Grid", "HIGH",
             "Red Hat Data Grid 8 (pre-8.3.7) — init script is cgroups v1-only, Xmx miscalculated",
             "Upgrade to Data Grid 8.3.7+ (fixes cgroups v2 Xmx script) or 8.4.6+ (50% heap). "
             "Pre-8.3.7 defaults to 25% of host memory instead of container memory."),
            (r"amq-broker[:\-_/](7\.[0-9]|7\.1[01])(?:\.|$)", "Messaging", "MEDIUM",
             "Old AMQ Broker — check JVM and init scripts for cgroups v2 support",
             "Upgrade to latest AMQ Broker with JDK 17+."),
        ]
        for pattern, cat, sev, msg, rec in checks:
            if re.search(pattern, img):
                report.findings.append(Finding(cat, sev, msg, rec))

    # ── Level 2: skopeo remote inspection ───────────────────────────────
    def _skopeo_inspect_all(self):
        images = list(self.image_reports.keys())
        total = len(images)

        images_to_inspect = images

        logger.info(f"Skopeo inspection: {total} images to inspect")

        inspect_total = len(images_to_inspect)
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {executor.submit(self._skopeo_inspect_one, img): img for img in images_to_inspect}
            done = 0
            for future in as_completed(futures):
                done += 1
                img = futures[future]
                self._report_progress("skopeo", done, total,
                    f"Inspecting ({done}/{inspect_total}): {img[:60]}")
                try:
                    future.result()
                except Exception as e:
                    self.image_reports[img].inspection_error = str(e)

    @staticmethod
    def _parse_skopeo_error(stderr: str, image: str) -> str:
        """Extract a meaningful error from skopeo's stderr output.

        Skopeo wraps connection/auth errors inside 'Error parsing image name',
        hiding the real cause.  This method extracts the actual reason.
        """
        stderr = stderr.strip()
        # skopeo structured log: extract msg="..." content
        msg_match = re.search(r'msg="(.+)"?\s*$', stderr, re.DOTALL)
        inner = msg_match.group(1).rstrip('"') if msg_match else stderr

        # Known real-cause patterns (ordered by specificity)
        patterns = [
            (r"unauthorized", "unauthorized – registry credentials required"),
            (r"authentication required", "authentication required"),
            (r"no such host", "registry host not found (DNS)"),
            (r"connection refused", "registry connection refused"),
            (r"certificate.+unknown authority", "TLS certificate not trusted"),
            (r"manifest unknown", "manifest not found in registry"),
            (r"timeout", "registry connection timeout"),
            (r"no basic auth credentials", "no auth credentials provided"),
        ]
        for pat, friendly in patterns:
            if re.search(pat, inner, re.IGNORECASE):
                return f"skopeo: {friendly}"

        # Fallback: return full stderr (up to 500 chars)
        return f"skopeo failed: {stderr[:500]}"

    def _skopeo_inspect_one(self, image: str):
        report = self.image_reports[image]
        cmd = ["skopeo", "inspect"]
        tmp_auth_file = None

        is_internal = bool(self._internal_registry and self._internal_registry in image)

        # Internal registry always needs --tls-verify=false (self-signed certs)
        if not self.skopeo_tls_verify or is_internal:
            cmd.append("--tls-verify=false")

        # Auth selection:
        # - For the OpenShift internal registry, always use the scanner's own
        #   ServiceAccount token (granted system:image-puller cluster-wide).
        #   We must NOT pass --authfile here, because pull-secret tokens
        #   harvested from pods are SA tokens scoped to a single namespace,
        #   which would shadow our cluster-wide token and cause
        #   "authentication required" for imagestreams in other namespaces.
        # - For external registries, use (in order): explicit global authfile,
        #   then a temp authfile built from ImagePullSecrets / saved registry
        #   credentials.
        if is_internal and self._sa_token:
            cmd.extend(["--creds", f"serviceaccount:{self._sa_token}"])
        elif self.skopeo_auth_file:
            cmd.extend(["--authfile", self.skopeo_auth_file])
        elif self._registry_auths:
            tmp_auth_file = self._build_auth_file_for_image(image)
            if tmp_auth_file:
                cmd.extend(["--authfile", tmp_auth_file])

        # Normalize image reference for skopeo.
        # Images with both tag and digest (e.g. image:v4.18@sha256:abc) cause
        # "Error parsing image name" because skopeo cannot handle that format.
        # Strip the tag portion, keeping only the digest which is the precise ref.
        skopeo_ref = image
        if "@sha256:" in skopeo_ref:
            # Split at @, then remove the tag from the name part (before @)
            name_part, digest_part = skopeo_ref.split("@", 1)
            # name_part might be "registry:5000/repo:tag" — strip only the TAG,
            # not the registry port.  The tag is always after the last '/'.
            last_slash = name_part.rfind("/")
            if last_slash >= 0:
                repo_part = name_part[last_slash + 1:]
                if ":" in repo_part:
                    # Strip the tag from the final path segment
                    name_part = name_part[:last_slash + 1] + repo_part.rsplit(":", 1)[0]
            else:
                # No slash at all (e.g. "image:tag@sha256:...")
                if ":" in name_part:
                    name_part = name_part.rsplit(":", 1)[0]
            skopeo_ref = f"{name_part}@{digest_part}"
        cmd.append(f"docker://{skopeo_ref}")

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode != 0:
                report.inspection_error = self._parse_skopeo_error(result.stderr, image)
                return
            data = json.loads(result.stdout)
        except subprocess.TimeoutExpired:
            report.inspection_error = "skopeo timeout"
            return
        except json.JSONDecodeError:
            report.inspection_error = "invalid JSON from skopeo"
            return
        finally:
            # Clean up temporary auth file
            if tmp_auth_file:
                try:
                    os.unlink(tmp_auth_file)
                except OSError:
                    pass

        report.inspected = True
        self._analyze_skopeo_data(report, data)

    def _analyze_skopeo_data(self, report: ImageReport, data: dict):
        labels = data.get("Labels") or {}
        env_list = data.get("Env") or []
        cmd = data.get("Cmd") or []
        entrypoint = data.get("Entrypoint") or []

        metadata = self._build_inspection_metadata(labels, env_list, cmd, entrypoint)
        report.inspection_metadata = metadata

        # Base image from OCI labels
        base_image = (
            labels.get("org.opencontainers.image.base.name", "")
            or labels.get("base-image", "")
            or labels.get("io.openshift.build.image", "")
        )
        if base_image:
            for pattern, (name, sev, rec) in KNOWN_PROBLEMATIC_BASES.items():
                if re.search(pattern, base_image.lower()):
                    existing = [f for f in report.findings
                                if f.category.startswith("Base Image") and name in f.message]
                    if not existing:
                        report.findings.append(Finding("Base Image (skopeo)", sev,
                            f"Real base image: {name} (OCI label)", rec,
                            f"Label: {base_image}"))

        # Red Hat component label
        rh_comp = labels.get("com.redhat.component", "")
        if rh_comp and ("el7" in rh_comp.lower() or "rhel-7" in rh_comp.lower()):
            if not any("RHEL" in f.message and "7" in f.message for f in report.findings):
                report.findings.append(Finding("Base Image (skopeo)", "CRITICAL",
                    "Red Hat component based on RHEL 7 detected via label",
                    "Migrate to UBI 8/9.",
                    f"Label: {rh_comp}"))

        # Red Hat middleware detection via labels
        self._check_middleware_labels(report, labels)

        # ENV / CMD / Entrypoint cgroups v1 references
        all_text = " ".join(str(x) for x in entrypoint + cmd + env_list)
        for v1_path in CGROUPS_V1_PATHS:
            if v1_path in all_text:
                report.findings.append(Finding("cgroups v1 Reference (skopeo)", "HIGH",
                    f"cgroups v1 path reference in image config: {v1_path}",
                    "Update application to use cgroups v2 unified hierarchy."))
                break
        for v1_file in CGROUPS_V1_FILES:
            if v1_file in all_text:
                report.findings.append(Finding("cgroups v1 Reference (skopeo)", "HIGH",
                    f"cgroups v1 file reference in image config: {v1_file}",
                    "This file does not exist in cgroups v2. Check v1→v2 mapping."))
                break

        # JVM flags
        for env_var in env_list:
            if isinstance(env_var, str):
                if "-UseContainerSupport" in env_var and ":-UseContainerSupport" in env_var:
                    report.findings.append(Finding("JVM Config (skopeo)", "HIGH",
                        "-XX:-UseContainerSupport disables container detection",
                        "Remove this flag. JVM needs it enabled for cgroups v2."))
                jm = re.match(r"JAVA_VERSION=(.+)", env_var)
                if jm:
                    self._check_java_env(report, jm.group(1))

        if not report.findings and not metadata.get("has_base_image_label") and not metadata.get("has_os_labels"):
            report.findings.append(Finding(
                "Insufficient Metadata (skopeo)", "LOW",
                "Image has no base image labels or OS identification — cannot validate via metadata alone",
                "Add OCI labels (org.opencontainers.image.base.name, com.redhat.component) to the image, "
                "or run scan with exec_check enabled to verify at runtime.",
                f"Labels found: {metadata.get('label_count', 0)}, "
                f"ENV vars: {metadata.get('env_count', 0)}",
            ))

    @staticmethod
    def _build_inspection_metadata(labels: dict, env_list: list,
                                   cmd: list, entrypoint: list) -> dict:
        """Summarize what skopeo returned for audit trail purposes."""
        # Collect relevant label keys that help identify base image / OS
        os_label_keys = {
            "org.opencontainers.image.base.name", "base-image",
            "io.openshift.build.image", "com.redhat.component",
            "org.opencontainers.image.version", "version",
            "release", "name", "summary", "description",
            "org.opencontainers.image.title",
        }
        found_labels = {k: v for k, v in labels.items() if k in os_label_keys and v}

        base_label = (
            labels.get("org.opencontainers.image.base.name", "")
            or labels.get("base-image", "")
            or labels.get("io.openshift.build.image", "")
        )

        has_os_labels = bool(
            labels.get("com.redhat.component")
            or labels.get("org.opencontainers.image.base.name")
            or labels.get("base-image")
            or labels.get("release")
        )

        env_relevant = []
        for e in env_list:
            if isinstance(e, str):
                key = e.split("=", 1)[0] if "=" in e else e
                if key in ("JAVA_VERSION", "JAVA_HOME", "NODE_VERSION",
                           "PYTHON_VERSION", "DOTNET_VERSION", "GOLANG_VERSION",
                           "PATH", "HOME"):
                    env_relevant.append(e)

        metadata = {
            "label_count": len(labels),
            "env_count": len(env_list),
            "has_base_image_label": bool(base_label),
            "has_os_labels": has_os_labels,
            "relevant_labels": found_labels if found_labels else None,
            "relevant_env": env_relevant if env_relevant else None,
            "has_cmd": bool(cmd),
            "has_entrypoint": bool(entrypoint),
        }
        return {k: v for k, v in metadata.items() if v is not None}

    def _check_java_env(self, report: ImageReport, ver_str: str):
        ver = ver_str.strip()
        m_old = re.match(r"1\.(\d+)\.0[_\-](\d+)", ver)
        m_new = re.match(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", ver)
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
        if not safe:
            if not any(f.category == "Java Runtime (skopeo)" for f in report.findings):
                sev = "HIGH" if major < 11 else "MEDIUM"
                report.findings.append(Finding("Java Runtime (skopeo)", sev,
                    f"Java {ver} via JAVA_VERSION ENV — may not support cgroups v2",
                    f"Safe versions: {JAVA_SAFE_VERSIONS}",
                    f"ENV JAVA_VERSION={ver}"))

    def _check_middleware_labels(self, report: ImageReport, labels: dict):
        """Detect Red Hat middleware products via OCI/Red Hat labels.

        JBoss EAP 7, Data Grid 8 (pre-8.3.7), and other middleware may use
        init scripts that are cgroups v1-only for Xmx calculation.
        """
        rh_comp = labels.get("com.redhat.component", "").lower()
        version_label = (
            labels.get("version", "")
            or labels.get("org.opencontainers.image.version", "")
        )
        name_label = labels.get("name", "").lower()
        summary_label = labels.get("summary", "").lower()

        # JBoss EAP detection
        if "eap" in rh_comp or "jboss-eap" in name_label or "eap" in summary_label:
            # Old EAP 7 images had cgroups v1-only memory scripts
            if re.search(r"7\.[0-3]", version_label):
                if not any("EAP" in f.message for f in report.findings):
                    report.findings.append(Finding("Middleware (skopeo)", "HIGH",
                        f"JBoss EAP {version_label} — init scripts may be cgroups v1-only",
                        "Upgrade to latest JBoss EAP 7.4.x+ or EAP 8. "
                        "EAP 8 fully relies on OpenJDK layer for cgroups detection.",
                        f"Label: com.redhat.component={rh_comp}"))

        # Red Hat Data Grid detection
        if "datagrid" in rh_comp or "infinispan" in name_label or "data grid" in summary_label:
            m = re.match(r"(\d+)\.(\d+)\.?(\d*)", version_label)
            if m:
                dg_major, dg_minor, dg_patch = (
                    int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
                )
                # Data Grid 8.3.x before 8.3.7 has cgroups v1-only Xmx script
                if dg_major == 8 and (dg_minor < 3 or (dg_minor == 3 and dg_patch < 7)):
                    if not any("Data Grid" in f.message for f in report.findings):
                        report.findings.append(Finding("Middleware (skopeo)", "HIGH",
                            f"Red Hat Data Grid {version_label} — Xmx script is cgroups v1-only",
                            "Upgrade to Data Grid 8.3.7+ or 8.4.6+. "
                            "Pre-8.3.7 uses host memory for Xmx calculation, not container memory.",
                            f"Label: com.redhat.component={rh_comp}"))
                # DG 8.3.7+ works but uses 25% instead of intended 50%
                elif dg_major == 8 and dg_minor == 3 and dg_patch >= 7:
                    report.findings.append(Finding("Middleware (skopeo)", "LOW",
                        f"Red Hat Data Grid {version_label} — heap defaults to 25% of container",
                        "Data Grid 8.3.7+ is cgroups v2 compatible but uses 25% heap "
                        "(upstream default). Upgrade to 8.4.6+ for 50% heap, or set "
                        "MaxRAMPercentage explicitly.",
                        f"Label: com.redhat.component={rh_comp}"))

    # ─────────────────────────────────────────────────────────────────────
    # Level 3: pod exec cgroups v1 runtime detection
    # ─────────────────────────────────────────────────────────────────────
    def _exec_check_all(self):
        """Execute a cgroups v1 detection script inside running pods.

        Picks one running pod per unique image and runs a shell script
        that checks for cgroups v1 paths, file references, and
        environment variables inside the container.
        """
        # Only check images that have a running pod with a regular container
        exec_targets = {
            img: pod_info
            for img, pod_info in self._image_pod_map.items()
            if img in self.image_reports
        }

        if not exec_targets:
            logger.info("Exec check: no running pods available for exec")
            return

        total = len(exec_targets)
        logger.info(f"Exec check: {total} images to inspect via pod exec "
                     f"({_EXEC_MAX_WORKERS} workers, {_EXEC_TIMEOUT}s timeout)")

        with ThreadPoolExecutor(max_workers=_EXEC_MAX_WORKERS) as executor:
            futures = {
                executor.submit(self._exec_check_one, img, ns, pod_name, container):
                img for img, (ns, pod_name, container) in exec_targets.items()
            }
            done = 0
            for future in as_completed(futures):
                done += 1
                img = futures[future]
                self._report_progress("exec_check", done, total,
                    f"Pod exec ({done}/{total}): {img[:60]}")
                try:
                    future.result()
                except Exception as e:
                    logger.debug(f"Exec check failed for {img}: {e}")

        exec_ok = sum(1 for img in exec_targets
                      if any(f.category.startswith("Runtime Check")
                             for f in self.image_reports[img].findings))
        self.cluster_info["exec_checked"] = str(total)
        logger.info(f"Exec check: completed {total} pods, {exec_ok} with findings")

        # Remove "Insufficient Metadata" findings for images where exec ran
        # successfully and found no cgroups v1 issues (runtime confirmed OK).
        cleared = 0
        for img in exec_targets:
            report = self.image_reports[img]
            if not report.exec_checked:
                continue
            has_runtime_finding = any(
                f.category.startswith("Runtime Check") for f in report.findings)
            if not has_runtime_finding:
                before = len(report.findings)
                report.findings = [
                    f for f in report.findings
                    if f.category != "Insufficient Metadata (skopeo)"
                ]
                if len(report.findings) < before:
                    cleared += 1
        if cleared:
            logger.info(f"Exec check: cleared {cleared} 'Insufficient Metadata' findings (runtime confirmed OK)")

    def _exec_check_one(self, image: str, namespace: str, pod_name: str, container: str):
        """Execute the cgroups v1 check script inside a single pod container."""
        report = self.image_reports[image]

        try:
            output = k8s_stream(
                self.v1.connect_get_namespaced_pod_exec,
                pod_name, namespace,
                container=container,
                command=["/bin/sh", "-c", _EXEC_CHECK_SCRIPT],
                stderr=True, stdin=False, stdout=True, tty=False,
                _request_timeout=_EXEC_TIMEOUT,
            )
        except ApiException as e:
            if e.status == 403:
                logger.debug(f"Exec forbidden for {namespace}/{pod_name}: {e.reason}")
            else:
                logger.debug(f"Exec failed for {namespace}/{pod_name}: {e}")
            return
        except Exception as e:
            logger.debug(f"Exec error for {namespace}/{pod_name}/{container}: {e}")
            return

        if not output or "===DONE===" not in output:
            logger.debug(f"Exec incomplete output for {namespace}/{pod_name}/{container}")
            return

        report.exec_checked = True
        self._parse_exec_output(report, output, namespace, pod_name, container)

    def _parse_exec_output(self, report: ImageReport, output: str,
                           namespace: str, pod_name: str, container: str):
        """Parse the output of the cgroups v1 exec check script."""
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

        # Check for v1 file references inside the application
        v1_refs = sections.get("V1REFS", [])
        if v1_refs:
            # Deduplicate file paths
            unique_refs = sorted(set(v1_refs))
            report.findings.append(Finding(
                "Runtime Check (exec)", "HIGH",
                f"Application files reference cgroups v1 paths inside the container",
                "Update the application to use cgroups v2 unified hierarchy paths. "
                "Example: /sys/fs/cgroup/memory.max instead of "
                "/sys/fs/cgroup/memory/memory.limit_in_bytes",
                f"Files found in {pod_ref}: {', '.join(unique_refs[:10])}"
                + (f" (+{len(unique_refs) - 10} more)" if len(unique_refs) > 10 else ""),
            ))

        # Check for cgroups v1 references in environment variables
        env_refs = sections.get("ENVREF", [])
        cgroup_v1_env = [e for e in env_refs if any(
            v1 in e for v1 in ["/sys/fs/cgroup/memory/", "/sys/fs/cgroup/cpu/",
                                "memory.limit_in_bytes", "cpu.cfs_quota_us",
                                "cpu.shares", "cpuacct."]
        )]
        if cgroup_v1_env:
            report.findings.append(Finding(
                "Runtime Check (exec)", "MEDIUM",
                "Environment variables reference cgroups v1 paths",
                "Update environment variables to use cgroups v2 paths.",
                f"Pod {pod_ref}: {'; '.join(cgroup_v1_env[:5])}",
            ))

    # ─────────────────────────────────────────────────────────────────────
    # Report generation
    # ─────────────────────────────────────────────────────────────────────
    def _apply_init_container_severity_downgrade(self):
        """Downgrade finding severity for images that appear ONLY in initContainers.

        InitContainers run once and exit before the main containers start.
        A legacy busybox or alpine used only as initContainer (e.g., for chown/chmod)
        is less risky than the same image running as a long-lived container.
        Severity is reduced by one level and a note is added.
        """
        severity_downgrade = {
            "CRITICAL": "HIGH",
            "HIGH": "MEDIUM",
            "MEDIUM": "LOW",
            "LOW": "INFO",
        }
        for report in self.image_reports.values():
            if not report.only_in_init:
                continue
            for finding in report.findings:
                original = finding.severity
                downgraded = severity_downgrade.get(original)
                if downgraded:
                    finding.severity = downgraded
                    finding.message = f"[initContainer only] {finding.message}"
                    finding.details = (
                        f"{finding.details} "
                        f"(severity reduced from {original} to {downgraded} "
                        f"because this image is only used in initContainers)"
                    ).strip()

    def get_summary(self) -> dict:
        """Return scan summary as a dictionary."""
        by_severity = defaultdict(int)
        for r in self.image_reports.values():
            by_severity[r.max_severity] += 1

        init_only = sum(1 for r in self.image_reports.values() if r.only_in_init)

        summary = {
            "generated_at": datetime.now().isoformat(),
            "cluster_info": self.cluster_info,
            "total_images": len(self.image_reports),
            "init_only_images": init_only,
            "inspected_via_skopeo": sum(1 for r in self.image_reports.values() if r.inspected),
            "skopeo_errors": sum(1 for r in self.image_reports.values() if r.inspection_error),
            "exec_checked": int(self.cluster_info.get("exec_checked", 0)),
            "by_severity": dict(by_severity),
        }

        # Add OCP cgroups context based on detected cluster version
        ocp_ver = self.cluster_info.get("openshift_version", "")
        summary["cgroups_context"] = self._get_cgroups_context(ocp_ver)

        return summary

    @staticmethod
    def _get_cgroups_context(ocp_version: str) -> dict:
        """Provide cgroups v1/v2 context based on the detected OCP version.

        Based on: developers.redhat.com/articles/2025/11/27/
        how-does-cgroups-v2-impact-java-net-and-nodejs-openshift-4
        """
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
