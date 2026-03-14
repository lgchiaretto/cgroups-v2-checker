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

JAVA_SAFE_VERSIONS = "JDK 15+, JDK 11.0.19+, JDK 8u372+"
JAVA_DETAIL = (
    "JDK < 15 unpatched does not recognize cgroups v2 for memory/CPU limits. "
    "This causes: containers not respecting memory limits (unexpected OOMKill), "
    "incorrect availableProcessors() calculation, and wrong heap sizing."
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

    @property
    def max_severity(self) -> str:
        order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
        if not self.findings:
            if not self.inspected:
                return "UNKNOWN"
            return "OK"
        return min(self.findings, key=lambda f: order.get(f.severity, 99)).severity

    @property
    def severity_sort_key(self) -> int:
        order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4, "UNKNOWN": 5, "OK": 6}
        return order.get(self.max_severity, 99)

    def to_dict(self) -> dict:
        return {
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

        self.v1 = client.CoreV1Api()

        version_api = client.VersionApi()
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
            custom = client.CustomObjectsApi()
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
            custom = client.CustomObjectsApi()
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
                    (major == 11 and (minor > 0 or patch >= 19)) or
                    (major == 8 and patch >= 372))
            if not safe:
                sev = "HIGH" if major < 11 else "MEDIUM"
                report.findings.append(Finding("Java Runtime", sev,
                    f"Java {major} detected — may not recognize cgroups v2 limits",
                    f"{JAVA_DETAIL} Safe versions: {JAVA_SAFE_VERSIONS}",
                    f"Tag version: {m.group(0)}"))

        # .NET
        m = re.search(r"(?:dotnet|aspnet|dotnet-runtime)[:\-/](\d+)\.(\d+)", img)
        if m and int(m.group(1)) < 6:
            report.findings.append(Finding(".NET Runtime", "MEDIUM",
                f".NET {m.group(1)}.{m.group(2)} — limited cgroups v2 support",
                "Migrate to .NET 6+."))

        # Node.js
        m = re.search(r"node[:\-_](\d+)", img)
        if m and int(m.group(1)) < 16:
            report.findings.append(Finding("Node.js Runtime", "LOW",
                f"Node.js {m.group(1)} — may have cgroups v2 metrics issues",
                "Consider Node.js 16+."))

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

        # Auth file priority:
        # 1. Explicit auth file from config (global)
        # 2. Dynamically built auth from ImagePullSecrets
        # 3. ServiceAccount token for internal registry
        if self.skopeo_auth_file:
            cmd.extend(["--authfile", self.skopeo_auth_file])
        elif self._registry_auths:
            tmp_auth_file = self._build_auth_file_for_image(image)
            if tmp_auth_file:
                cmd.extend(["--authfile", tmp_auth_file])

        if self._sa_token and is_internal:
            cmd.extend(["--creds", f"serviceaccount:{self._sa_token}"])

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
                (major == 11 and (minor > 0 or patch >= 19)) or
                (major == 8 and patch >= 372))
        if not safe:
            if not any(f.category == "Java Runtime (skopeo)" for f in report.findings):
                sev = "HIGH" if major < 11 else "MEDIUM"
                report.findings.append(Finding("Java Runtime (skopeo)", sev,
                    f"Java {ver} via JAVA_VERSION ENV — may not support cgroups v2",
                    f"Safe versions: {JAVA_SAFE_VERSIONS}",
                    f"ENV JAVA_VERSION={ver}"))

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

        return {
            "generated_at": datetime.now().isoformat(),
            "cluster_info": self.cluster_info,
            "total_images": len(self.image_reports),
            "init_only_images": init_only,
            "inspected_via_skopeo": sum(1 for r in self.image_reports.values() if r.inspected),
            "skopeo_errors": sum(1 for r in self.image_reports.values() if r.inspection_error),

            "by_severity": dict(by_severity),
        }

    def get_full_report(self) -> dict:
        """Return the full report as a serializable dictionary."""
        summary = self.get_summary()
        sorted_reports = sorted(self.image_reports.values(), key=lambda r: r.severity_sort_key)
        summary["images"] = [r.to_dict() for r in sorted_reports]
        return summary
