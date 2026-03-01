/* cgroups v2 Checker - Dashboard & Scan Interaction */
"use strict";

document.addEventListener("DOMContentLoaded", function () {
  const btnStartScan = document.getElementById("btn-start-scan");
  const scanOptionsPanel = document.getElementById("scan-options-panel");
  const scanProgressPanel = document.getElementById("scan-progress-panel");
  const scanOptionsForm = document.getElementById("scan-options-form");
  const btnCancelOptions = document.getElementById("btn-cancel-options");

  if (!btnStartScan) return;

  // Show scan options panel
  btnStartScan.addEventListener("click", function () {
    scanOptionsPanel.style.display = "";
    btnStartScan.disabled = true;
  });

  // Cancel options
  if (btnCancelOptions) {
    btnCancelOptions.addEventListener("click", function () {
      scanOptionsPanel.style.display = "none";
      btnStartScan.disabled = false;
    });
  }

  // Submit scan
  if (scanOptionsForm) {
    scanOptionsForm.addEventListener("submit", function (e) {
      e.preventDefault();
      startScan();
    });
  }

  function startScan() {
    const nsField = document.getElementById("opt-namespaces").value.trim();
    const exField = document.getElementById("opt-exclude").value.trim();
    const inspectImages = document.getElementById("opt-inspect").checked;

    const body = { inspect_images: inspectImages };
    if (nsField) body.namespaces = nsField.split(",").map(function (s) { return s.trim(); });
    if (exField) body.exclude_namespaces = exField.split(",").map(function (s) { return s.trim(); });

    scanOptionsPanel.style.display = "none";
    scanProgressPanel.style.display = "";

    fetch("/api/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
      .then(function (res) { return res.json(); })
      .then(function (data) {
        if (data.error) {
          showProgressError(data.error);
          return;
        }
        pollScanProgress(data.scan_id);
      })
      .catch(function (err) {
        showProgressError("Failed to start scan: " + err.message);
      });
  }

  function pollScanProgress(scanId) {
    const interval = setInterval(function () {
      fetch("/api/scan/" + scanId)
        .then(function (res) { return res.json(); })
        .then(function (data) {
          updateProgressUI(data);
          if (data.status === "completed") {
            clearInterval(interval);
            setTimeout(function () { window.location.reload(); }, 1000);
          } else if (data.status === "failed") {
            clearInterval(interval);
            showProgressError(data.error || "Scan failed.");
          }
        })
        .catch(function () { /* retry on next poll */ });
    }, 1500);
  }

  function updateProgressUI(data) {
    var pct = 0;
    if (data.total > 0) pct = Math.round((data.progress / data.total) * 100);

    var stageLabels = {
      initializing: "Initializing...",
      connecting: "Connecting to cluster...",
      collecting: "Collecting pod images...",
      analyzing: "Analyzing images (name/tag)...",
      skopeo: "Remote inspection via skopeo...",
      saving: "Saving report...",
      done: "Scan complete!",
    };

    var title = stageLabels[data.stage] || data.stage;
    document.getElementById("scan-progress-title").textContent = title;
    document.getElementById("scan-progress-detail").textContent = data.detail || "";
    document.getElementById("scan-progress-pct").textContent = pct + "%";
    document.getElementById("scan-progress-indicator").style.width = pct + "%";

    if (data.status === "completed") {
      document.getElementById("scan-progress-title").innerHTML =
        '<i class="fas fa-check-circle pf-v6-u-success-color-100"></i> Scan complete! Reloading...';
    }
  }

  function showProgressError(msg) {
    document.getElementById("scan-progress-title").innerHTML =
      '<i class="fas fa-exclamation-circle pf-v6-u-danger-color-100"></i> ' + msg;
    btnStartScan.disabled = false;
  }

  // Delete report buttons
  document.querySelectorAll(".btn-delete-report").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var id = btn.dataset.reportId;
      if (!confirm("Delete report " + id + "?")) return;
      fetch("/api/reports/" + id, { method: "DELETE" })
        .then(function (res) { return res.json(); })
        .then(function () { window.location.reload(); })
        .catch(function (err) { alert("Delete failed: " + err.message); });
    });
  });
});
