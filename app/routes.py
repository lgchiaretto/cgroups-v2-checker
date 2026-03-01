"""Web routes blueprint."""

import json
import os
from datetime import datetime

from flask import (
    Blueprint, current_app, render_template, send_from_directory,
)

web_bp = Blueprint("web", __name__)


@web_bp.route("/")
def index():
    """Dashboard with list of past reports."""
    reports = _list_reports()
    return render_template("dashboard.html", reports=reports)


@web_bp.route("/reports/<report_id>")
def view_report(report_id: str):
    """View a specific report."""
    report_dir = current_app.config["REPORT_DIR"]
    filepath = os.path.join(report_dir, f"{report_id}.json")
    if not os.path.isfile(filepath):
        return render_template("error.html", message="Report not found."), 404
    with open(filepath) as f:
        data = json.load(f)
    return render_template("report.html", report=data, report_id=report_id)


@web_bp.route("/reports/<report_id>/download")
def download_report(report_id: str):
    """Download report JSON."""
    report_dir = current_app.config["REPORT_DIR"]
    filename = f"{report_id}.json"
    if not os.path.isfile(os.path.join(report_dir, filename)):
        return render_template("error.html", message="Report not found."), 404
    return send_from_directory(
        report_dir, filename, as_attachment=True,
        download_name=f"cgroups-v2-report-{report_id}.json",
    )


def _list_reports() -> list:
    """List saved reports sorted by date descending."""
    report_dir = current_app.config["REPORT_DIR"]
    reports = []
    if not os.path.isdir(report_dir):
        return reports
    for filename in os.listdir(report_dir):
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
                "cluster_info": data.get("cluster_info", {}),
            })
        except (json.JSONDecodeError, IOError):
            continue
    reports.sort(key=lambda r: r["generated_at"], reverse=True)
    return reports
