"""REST API blueprint for scan operations."""

import json
import logging
import os
import threading
import uuid
from datetime import datetime
from typing import Dict

from flask import Blueprint, current_app, jsonify, request

from app.scanner import CGroupsV2Scanner

api_bp = Blueprint("api", __name__)
logger = logging.getLogger(__name__)

# In-memory scan state (single-instance, fits the use case)
_scans: Dict[str, dict] = {}
_scan_lock = threading.Lock()


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
    """Get scan progress."""
    with _scan_lock:
        state = _scans.get(scan_id)
    if not state:
        return jsonify({"error": "Scan not found."}), 404
    return jsonify(state)


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
# Background scan runner
# ─────────────────────────────────────────────────────────────────────────────
def _run_scan(app, scan_id, namespaces, exclude_namespaces, inspect_images):
    """Execute scan in background thread."""
    with app.app_context():
        state = _scans[scan_id]

        def progress_cb(stage, current, total, detail):
            state["stage"] = stage
            state["progress"] = current
            state["total"] = total
            state["detail"] = detail

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

            state["stage"] = "connecting"
            scanner.connect()

            state["stage"] = "collecting"
            scanner.collect_images()

            state["stage"] = "analyzing"
            scanner.analyze()

            state["stage"] = "saving"
            report = scanner.get_full_report()

            report_dir = app.config["REPORT_DIR"]
            filepath = os.path.join(report_dir, f"{scan_id}.json")
            with open(filepath, "w") as f:
                json.dump(report, f, indent=2, default=str)

            state["status"] = "completed"
            state["stage"] = "done"
            state["finished_at"] = datetime.now().isoformat()
            logger.info(f"Scan {scan_id} completed: {report['total_images']} images analyzed.")

        except Exception as e:
            logger.exception(f"Scan {scan_id} failed: {e}")
            state["status"] = "failed"
            state["error"] = str(e)
            state["finished_at"] = datetime.now().isoformat()
