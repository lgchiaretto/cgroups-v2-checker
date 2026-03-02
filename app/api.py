"""REST API blueprint for scan operations."""

import base64
import json
import logging
import os
import threading
import uuid
from datetime import datetime
from typing import Dict, List

from flask import Blueprint, current_app, jsonify, request

from app.scanner import CGroupsV2Scanner

api_bp = Blueprint("api", __name__)
logger = logging.getLogger(__name__)

# In-memory scan state (primary store when running single-worker)
_scans: Dict[str, dict] = {}
_scan_lock = threading.Lock()

# ── File-based scan state (cross-worker / crash resilience) ──────────────────

_SCAN_STATE_SUBDIR = "scan_state"


def _scan_state_dir(report_dir: str) -> str:
    """Return directory for scan state files."""
    d = os.path.join(os.path.dirname(report_dir), _SCAN_STATE_SUBDIR)
    os.makedirs(d, exist_ok=True)
    return d


def _persist_scan_state(report_dir: str, scan_id: str, state: dict) -> None:
    """Write scan state to disk so any worker / restart can read it."""
    path = os.path.join(_scan_state_dir(report_dir), f"{scan_id}.json")
    try:
        with open(path, "w") as f:
            json.dump(state, f, default=str)
    except IOError:
        pass


def _load_scan_state(report_dir: str, scan_id: str):
    """Load scan state from disk (returns dict or None)."""
    path = os.path.join(_scan_state_dir(report_dir), f"{scan_id}.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


@api_bp.route("/scan", methods=["POST"])
def start_scan():
    """Start a new cluster scan."""
    # Prevent concurrent scans
    with _scan_lock:
        running = [s for s in _scans.values() if s["status"] == "running"]
        if running:
            return jsonify({"error": "A scan is already running.", "scan_id": running[0]["id"]}), 409

    body = request.get_json(silent=True) or {}
    namespaces = body.get("namespaces")  # list or None (all)
    exclude_namespaces = body.get("exclude_namespaces")
    inspect_images = body.get("inspect_images", True)

    scan_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]

    scan_state = {
        "id": scan_id,
        "status": "running",
        "stage": "initializing",
        "progress": 0,
        "total": 0,
        "detail": "",
        "started_at": datetime.now().isoformat(),
        "finished_at": None,
        "error": None,
    }
    with _scan_lock:
        _scans[scan_id] = scan_state

    # Persist to disk for cross-worker visibility
    _persist_scan_state(current_app.config["REPORT_DIR"], scan_id, scan_state)

    # Run scan in background thread
    app = current_app._get_current_object()
    t = threading.Thread(
        target=_run_scan,
        args=(app, scan_id, namespaces, exclude_namespaces, inspect_images),
        daemon=True,
    )
    t.start()

    return jsonify({"scan_id": scan_id, "status": "running"}), 202


@api_bp.route("/scan/<scan_id>", methods=["GET"])
def scan_status(scan_id: str):
    """Get scan progress (in-memory first, then disk fallback)."""
    with _scan_lock:
        state = _scans.get(scan_id)
    if state:
        return jsonify(state)

    # Fallback: read from disk (handles multi-worker & pod restart)
    state = _load_scan_state(current_app.config["REPORT_DIR"], scan_id)
    if state:
        return jsonify(state)

    return jsonify({"error": "Scan not found."}), 404


@api_bp.route("/reports", methods=["GET"])
def list_reports():
    """List all saved reports."""
    report_dir = current_app.config["REPORT_DIR"]
    reports = []
    if not os.path.isdir(report_dir):
        return jsonify(reports)
    for filename in sorted(os.listdir(report_dir), reverse=True):
        if not filename.endswith(".json"):
            continue
        filepath = os.path.join(report_dir, filename)
        try:
            with open(filepath) as f:
                data = json.load(f)
            reports.append({
                "id": filename.replace(".json", ""),
                "generated_at": data.get("generated_at", ""),
                "total_images": data.get("total_images", 0),
                "by_severity": data.get("by_severity", {}),
            })
        except (json.JSONDecodeError, IOError):
            continue
    return jsonify(reports)


@api_bp.route("/reports/<report_id>", methods=["GET"])
def get_report(report_id: str):
    """Get a specific report."""
    report_dir = current_app.config["REPORT_DIR"]
    filepath = os.path.join(report_dir, f"{report_id}.json")
    if not os.path.isfile(filepath):
        return jsonify({"error": "Report not found."}), 404
    with open(filepath) as f:
        data = json.load(f)
    return jsonify(data)


@api_bp.route("/reports/<report_id>", methods=["DELETE"])
def delete_report(report_id: str):
    """Delete a report."""
    report_dir = current_app.config["REPORT_DIR"]
    filepath = os.path.join(report_dir, f"{report_id}.json")
    if not os.path.isfile(filepath):
        return jsonify({"error": "Report not found."}), 404
    os.remove(filepath)
    return jsonify({"deleted": report_id})


# ─────────────────────────────────────────────────────────────────────────────
# Registry Credentials Management (IN-MEMORY ONLY — not persisted to disk)
# Credentials are lost when the pod restarts.
# ─────────────────────────────────────────────────────────────────────────────

_registries: List[dict] = []       # [{"registry": ..., "username": ..., "password": ...}]
_registries_lock = threading.Lock()


def _load_registries() -> List[dict]:
    """Return current in-memory registry credentials."""
    with _registries_lock:
        return list(_registries)


def _save_registries(registries: List[dict]) -> None:
    """Replace in-memory registry credentials."""
    with _registries_lock:
        _registries.clear()
        _registries.extend(registries)


def _build_registry_auths(registries: List[dict]) -> Dict[str, dict]:
    """Convert stored registries into docker-config-style auth dict.

    Returns a dict of {registry_host: {"auth": base64(user:pass)}} that
    can be merged directly into the scanner's _registry_auths.
    """
    auths: Dict[str, dict] = {}
    for entry in registries:
        registry = entry.get("registry", "").strip()
        username = entry.get("username", "")
        password = entry.get("password", "")
        if not registry or not username:
            continue
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        auths[registry] = {"auth": token}
    return auths


@api_bp.route("/registries", methods=["GET"])
def list_registries():
    """List configured registry credentials (passwords masked)."""
    registries = _load_registries()
    # Return without exposing passwords
    safe = []
    for r in registries:
        safe.append({
            "registry": r.get("registry", ""),
            "username": r.get("username", ""),
        })
    return jsonify({"registries": safe})


@api_bp.route("/registries", methods=["POST"])
def add_registry():
    """Add or update a registry credential."""
    body = request.get_json(silent=True) or {}
    registry = body.get("registry", "").strip().lower()
    username = body.get("username", "").strip()
    password = body.get("password", "")

    if not registry or not username or not password:
        return jsonify({"error": "registry, username, and password are required."}), 400

    # Remove protocol prefix if user included it
    for prefix in ("https://", "http://"):
        if registry.startswith(prefix):
            registry = registry[len(prefix):]
    registry = registry.rstrip("/")

    registries = _load_registries()
    # Update existing or add new
    found = False
    for r in registries:
        if r["registry"].lower() == registry:
            r["username"] = username
            r["password"] = password
            found = True
            break
    if not found:
        registries.append({"registry": registry, "username": username, "password": password})

    _save_registries(registries)
    logger.info(f"Registry credential saved for {registry} (user: {username})")
    return jsonify({"saved": registry}), 201


@api_bp.route("/registries/<path:registry_host>", methods=["DELETE"])
def delete_registry(registry_host: str):
    """Remove a registry credential."""
    registry_host = registry_host.strip().lower()
    registries = _load_registries()
    before = len(registries)
    registries = [r for r in registries if r.get("registry", "").lower() != registry_host]
    if len(registries) == before:
        return jsonify({"error": "Registry not found."}), 404
    _save_registries(registries)
    logger.info(f"Registry credential removed for {registry_host}")
    return jsonify({"deleted": registry_host})


# ─────────────────────────────────────────────────────────────────────────────
# Background scan runner
# ─────────────────────────────────────────────────────────────────────────────
def _run_scan(app, scan_id, namespaces, exclude_namespaces, inspect_images):
    """Execute scan in background thread."""
    with app.app_context():
        state = _scans[scan_id]
        report_dir = app.config["REPORT_DIR"]

        def progress_cb(stage, current, total, detail):
            state["stage"] = stage
            state["progress"] = current
            state["total"] = total
            state["detail"] = detail
            _persist_scan_state(report_dir, scan_id, state)

        try:
            scanner = CGroupsV2Scanner(
                namespaces=namespaces,
                exclude_namespaces=exclude_namespaces,
                skip_system_ns=app.config["SKIP_SYSTEM_NAMESPACES"],
                inspect_images=inspect_images,
                skopeo_tls_verify=app.config["SKOPEO_TLS_VERIFY"],
                skopeo_auth_file=app.config["SKOPEO_AUTH_FILE"],
                max_workers=app.config["SKOPEO_MAX_WORKERS"],
                progress_callback=progress_cb,
                use_image_pull_secrets=app.config["USE_IMAGE_PULL_SECRETS"],
            )

            # Pre-load user-configured registry credentials
            user_registries = _load_registries()
            if user_registries:
                user_auths = _build_registry_auths(user_registries)
                scanner._registry_auths.update(user_auths)
                logger.info(f"Loaded {len(user_auths)} user-configured registry credential(s)")

            state["stage"] = "connecting"
            _persist_scan_state(report_dir, scan_id, state)
            scanner.connect()

            state["stage"] = "collecting"
            _persist_scan_state(report_dir, scan_id, state)
            scanner.collect_images()

            state["stage"] = "analyzing"
            _persist_scan_state(report_dir, scan_id, state)
            scanner.analyze()

            state["stage"] = "saving"
            _persist_scan_state(report_dir, scan_id, state)
            report = scanner.get_full_report()

            filepath = os.path.join(report_dir, f"{scan_id}.json")
            with open(filepath, "w") as f:
                json.dump(report, f, indent=2, default=str)

            state["status"] = "completed"
            state["stage"] = "done"
            state["finished_at"] = datetime.now().isoformat()
            _persist_scan_state(report_dir, scan_id, state)
            logger.info(f"Scan {scan_id} completed: {report['total_images']} images analyzed.")

        except Exception as e:
            logger.exception(f"Scan {scan_id} failed: {e}")
            state["status"] = "failed"
            state["error"] = str(e)
            state["finished_at"] = datetime.now().isoformat()
            _persist_scan_state(report_dir, scan_id, state)
