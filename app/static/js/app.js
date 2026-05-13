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

  function _isRegex(s) {
    return /[.*+?^${}()|\[\]\\]/.test(s);
  }

  function _splitField(fieldId) {
    var val = document.getElementById(fieldId).value.trim();
    if (!val) return { names: [], patterns: [] };
    var names = [];
    var patterns = [];
    val.split(",").forEach(function (s) {
      s = s.trim();
      if (!s) return;
      if (_isRegex(s)) { patterns.push(s); } else { names.push(s); }
    });
    return { names: names, patterns: patterns };
  }

  function startScan() {
    var include = _splitField("opt-include");
    var exclude = _splitField("opt-exclude");
    var inspectImages = document.getElementById("opt-inspect").checked;
    var execCheck = document.getElementById("opt-exec-check").checked;

    var body = { inspect_images: inspectImages, exec_check: execCheck };
    if (include.names.length) body.namespaces = include.names;
    if (include.patterns.length) body.namespace_patterns = include.patterns;
    if (exclude.names.length) body.exclude_namespaces = exclude.names;
    if (exclude.patterns.length) body.exclude_patterns = exclude.patterns;

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
          if (typeof showFlashMessage === 'function') showFlashMessage(data.error, 'danger');
          return;
        }
        if (typeof showFlashMessage === 'function') showFlashMessage('Scan started. Monitoring progress...', 'info');
        pollScanProgress(data.scan_id);
      })
      .catch(function (err) {
        showProgressError("Failed to start scan: " + err.message);
      });
  }

  function pollScanProgress(scanId) {
    var errorCount = 0;
    var maxErrors = 40; // ~60 seconds of 404s before checking for completed report
    var startTime = Date.now();

    var interval = setInterval(function () {
      fetch("/api/scan/" + scanId)
        .then(function (res) {
          if (res.status === 404) {
            errorCount++;
            // Show informational message after a few retries
            if (errorCount >= 3) {
              document.getElementById("scan-progress-detail").textContent =
                "Waiting for scan state... (attempt " + errorCount + "/" + maxErrors + ")";
            }
            // After many 404s, check if scan completed but state was lost
            if (errorCount >= maxErrors) {
              clearInterval(interval);
              checkForCompletedReport(scanId);
            }
            return null;
          }
          errorCount = 0; // reset counter on success
          return res.json();
        })
        .then(function (data) {
          if (!data) return;
          // Attach elapsed seconds for the UI
          data._elapsed = Math.round((Date.now() - startTime) / 1000);
          updateProgressUI(data);
          if (data.status === "completed") {
            clearInterval(interval);
            if (typeof showFlashMessage === 'function') showFlashMessage('Scan completed successfully!', 'success');
            setTimeout(function () { window.location.reload(); }, 1500);
          } else if (data.status === "failed") {
            clearInterval(interval);
            if (typeof showFlashMessage === 'function') showFlashMessage(data.error || 'Scan failed.', 'danger');
            showProgressError(data.error || "Scan failed.");
          }
        })
        .catch(function () {
          errorCount++;
          if (errorCount >= maxErrors) {
            clearInterval(interval);
            checkForCompletedReport(scanId);
          }
        });
    }, 1500);
  }

  function checkForCompletedReport(scanId) {
    // The scan may have completed but the polling state was lost (e.g. pod restart).
    // Check if a report was saved with this scan ID.
    fetch("/api/reports/" + scanId)
      .then(function (res) {
        if (res.status === 200) {
          window.location.href = "/reports/" + scanId;
        } else {
          showProgressError("Lost connection to scan. Check the reports list or try again.");
          setTimeout(function () { window.location.reload(); }, 3000);
        }
      })
      .catch(function () {
        showProgressError("Lost connection to scan. Check the reports list or try again.");
        setTimeout(function () { window.location.reload(); }, 3000);
      });
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
      java_verify: "Verifying Java versions via pod exec...",
      exec_check: "Runtime check via pod exec...",
      saving: "Saving report...",
      done: "Scan complete!",
    };

    var title = stageLabels[data.stage] || data.stage;

    // Append elapsed time
    if (data._elapsed && data.status !== "completed") {
      var mins = Math.floor(data._elapsed / 60);
      var secs = data._elapsed % 60;
      var timeStr = mins > 0 ? mins + "m " + secs + "s" : secs + "s";
      title += " (" + timeStr + ")";
    }

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
        .then(function () {
          if (typeof showFlashMessage === 'function') showFlashMessage('Report deleted.', 'success');
          setTimeout(function () { window.location.reload(); }, 500);
        })
        .catch(function (err) {
          if (typeof showFlashMessage === 'function') showFlashMessage('Delete failed: ' + err.message, 'danger');
        });
    });
  });
});
