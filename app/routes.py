"""Web routes blueprint."""

import csv
import io
import json
import os
from datetime import datetime

from flask import (
    Blueprint, Response, current_app, render_template, request, send_from_directory,
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


@web_bp.route("/registries")
def registries_page():
    """Registry credentials management page."""
    return render_template("registries.html")


@web_bp.route("/reports/<report_id>/csv")
def download_csv(report_id: str):
    """Download report as CSV.

    Query params:
      severity - comma-separated severities to include (e.g. CRITICAL,HIGH)
    """
    report_dir = current_app.config["REPORT_DIR"]
    filepath = os.path.join(report_dir, f"{report_id}.json")
    if not os.path.isfile(filepath):
        return render_template("error.html", message="Report not found."), 404
    with open(filepath) as f:
        data = json.load(f)

    severity_filter = request.args.get("severity", "")
    allowed = {s.strip().upper() for s in severity_filter.split(",") if s.strip()} if severity_filter else None

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Image", "Severity", "Pod Count", "Namespaces", "Pods",
        "Containers", "Init Containers", "Only Init", "Inspected",
        "Inspection Error", "Finding Category", "Finding Severity",
        "Finding Message", "Finding Recommendation", "Finding Details",
    ])

    for img in data.get("images", []):
        if allowed and img.get("max_severity") not in allowed:
            continue
        findings = img.get("findings", [])
        if not findings:
            writer.writerow([
                img.get("image", ""),
                img.get("max_severity", ""),
                img.get("pod_count", 0),
                "; ".join(img.get("namespaces", [])),
                "; ".join(img.get("pods", [])),
                "; ".join(img.get("containers", [])),
                "; ".join(img.get("init_containers", [])),
                img.get("only_in_init", False),
                img.get("inspected", False),
                img.get("inspection_error", ""),
                "", "", "", "", "",
            ])
        else:
            for f in findings:
                writer.writerow([
                    img.get("image", ""),
                    img.get("max_severity", ""),
                    img.get("pod_count", 0),
                    "; ".join(img.get("namespaces", [])),
                    "; ".join(img.get("pods", [])),
                    "; ".join(img.get("containers", [])),
                    "; ".join(img.get("init_containers", [])),
                    img.get("only_in_init", False),
                    img.get("inspected", False),
                    img.get("inspection_error", ""),
                    f.get("category", ""),
                    f.get("severity", ""),
                    f.get("message", ""),
                    f.get("recommendation", ""),
                    f.get("details", ""),
                ])

    csv_content = output.getvalue()
    return Response(
        csv_content,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=cgroups-v2-report-{report_id}.csv"},
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
