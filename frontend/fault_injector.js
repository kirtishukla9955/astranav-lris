/* ===================================================================
   ASTRANAV-LRIS — MISSION ANOMALY & CONTINGENCY ORCHESTRATION
   This script runs in the global browser context and hooks into
   dashboard.js to manage real-time anomaly injections.
 =================================================================== */

(function contingencyOrchestrator() {
  console.log("Contingency Orchestrator initializing...");

  // Top-level contingency states
  let isContingencyActive = false;
  let originalRoute = null;
  let contingencyWarningMarker = null;

  // DOM Elements
  const anomalySelect = document.getElementById("anomalySelect");
  const anomalyMagnitude = document.getElementById("anomalyMagnitude");
  const anomalyMagVal = document.getElementById("anomalyMagVal");
  const injectAnomalyBtn = document.getElementById("injectAnomalyBtn");
  
  const contingencyPanel = document.getElementById("contingencyPanel");
  const closeContingency = document.getElementById("closeContingency");

  const contReason = document.getElementById("contReason");
  const contEffects = document.getElementById("contEffects");
  const contDecisions = document.getElementById("contDecisions");

  const ociDiff = document.getElementById("ociDiff");
  const ociBefore = document.getElementById("ociBefore");
  const ociAfter = document.getElementById("ociAfter");

  const etaCompare = document.getElementById("etaCompare");
  const battCompare = document.getElementById("battCompare");
  const energyCompare = document.getElementById("energyCompare");
  const riskCompare = document.getElementById("riskCompare");
  
  const contTimelineLog = document.getElementById("contTimelineLog");
  const resetBtn = document.getElementById("resetBtn");

  // Health badges DOM mappings
  const healthBadges = {
    battery: document.getElementById("healthBattery"),
    drive: document.getElementById("healthDrive"),
    sensors: document.getElementById("healthSensors"),
    comms: document.getElementById("healthComms"),
    thermal: document.getElementById("healthThermal")
  };

  // Update Anomaly Magnitude UI
  if (anomalyMagnitude && anomalyMagVal) {
    anomalyMagnitude.addEventListener("input", (e) => {
      anomalyMagVal.textContent = e.target.value + "%";
    });
  }

  // Hook into window.render to draw custom overlays
  const originalRender = window.render;
  if (originalRender) {
    window.render = function () {
      originalRender();
      drawContingencyOverlays();
    };
  }

  // Hook into window.drawRoute to change color for contingency path
  const originalDrawRoute = window.drawRoute;
  if (originalDrawRoute) {
    window.drawRoute = function (routeData, color, progress) {
      if (isContingencyActive && color === "#3fe7ec") {
        // Traveled recovery route draws in pulsing red/orange
        color = "#ff6b5e";
      }
      originalDrawRoute(routeData, color, progress);
    };
  }

  // Draw custom map overlays
  function drawContingencyOverlays() {
    if (!window.cellW || !window.cellH || !window.ctx) return;
    const ctx = window.ctx;
    const cellW = window.cellW;
    const cellH = window.cellH;

    // 1. Draw original planned route as thin, faded dashed grey line
    if (originalRoute) {
      const pts = originalRoute.path.map(p => [
        p.x * cellW + cellW / 2,
        p.y * cellH + cellH / 2
      ]);
      if (pts.length >= 2) {
        ctx.save();
        ctx.strokeStyle = "rgba(160, 175, 190, 0.45)";
        ctx.lineWidth = 1.6;
        ctx.setLineDash([4, 4]);
        ctx.beginPath();
        ctx.moveTo(pts[0][0], pts[0][1]);
        for (let i = 1; i < pts.length; i++) {
          ctx.lineTo(pts[i][0], pts[i][1]);
        }
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.restore();
      }
    }

    // 2. Draw flashing hazard warning marker at injection point
    if (contingencyWarningMarker) {
      const px = contingencyWarningMarker.x * cellW + cellW / 2;
      const py = contingencyWarningMarker.y * cellH + cellH / 2;
      const pulseRadius = 13 + Math.sin(Date.now() / 140) * 4;

      ctx.save();
      ctx.strokeStyle = "#ff6b5e";
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(px, py, pulseRadius, 0, Math.PI * 2);
      ctx.stroke();

      ctx.fillStyle = "rgba(255, 107, 94, 0.25)";
      ctx.beginPath();
      ctx.arc(px, py, 7, 0, Math.PI * 2);
      ctx.fill();

      ctx.fillStyle = "#ff6b5e";
      ctx.font = "700 11px IBM Plex Mono, monospace";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText("⚠", px, py);
      ctx.restore();
    }
  }

  // Append entry to contingency timeline log
  function addTimelineEvent(message, isWarning = false) {
    if (!contTimelineLog) return;
    const timeStr = new Date().toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit"
    });
    const eventDiv = document.createElement("div");
    eventDiv.className = "timeline-event";
    if (isWarning) {
      eventDiv.style.color = "#ff6b5e";
      eventDiv.style.fontWeight = "bold";
    }
    eventDiv.innerHTML = `<span style="color: var(--slate-dim); margin-right: 6px;">[${timeStr}]</span>${message}`;
    contTimelineLog.appendChild(eventDiv);
    contTimelineLog.scrollTop = contTimelineLog.scrollHeight;
  }

  // Update specific system health badge UI helper
  function updateHealthBadge(badgeKey, status, severityClass) {
    const el = healthBadges[badgeKey];
    if (!el) return;
    el.textContent = status;
    el.className = `health-tag ${severityClass}`;
  }

  // Reset health badges back to NOMINAL
  function resetHealthBadges() {
    Object.keys(healthBadges).forEach((key) => {
      updateHealthBadge(key, "NOMINAL", "tag-green");
    });
  }

  // Set health status based on anomaly choice
  function applyHealthStatusForAnomaly(type, magPercent) {
    resetHealthBadges();
    
    if (type === "wheel_degradation") {
      updateHealthBadge("drive", "FAULT", "tag-red");
      addTimelineEvent(`Actuator load error on Drive Assembly. Efficiency dropped by ${magPercent}%.`, true);
    } else if (type === "battery_drain") {
      updateHealthBadge("battery", "DEGRADED", "tag-yellow");
      addTimelineEvent(`Internal cell short-circuit. Capping charging threshold.`, true);
    } else if (type === "sensor_degradation") {
      updateHealthBadge("sensors", "DEGRADED", "tag-yellow");
      addTimelineEvent(`Optics occluded. Hazard slope avoidance buffer expanded.`, true);
    } else if (type === "comm_blackout") {
      updateHealthBadge("comms", "BLACKOUT", "tag-red");
      addTimelineEvent(`Earth line-of-sight path occluded. Direct telemetry link dropped.`, true);
    } else if (type === "new_obstacle") {
      updateHealthBadge("sensors", "WARNING", "tag-yellow");
      addTimelineEvent(`Laser scanners flag unmapped hazard obstacle in planning path.`, true);
    } else if (type === "thermal_load") {
      updateHealthBadge("thermal", "HOT", "tag-red");
      addTimelineEvent(`Thermal load spike in PSR shadow. Survival heaters at maximum load.`, true);
    } else if (type === "solar_unavailable") {
      updateHealthBadge("battery", "WARNING", "tag-yellow");
      addTimelineEvent(`Target charging station blocked by local terrain shadow shifts.`, true);
    }
  }

  // Close Contingency Report slide-out
  function closeContingencyReport() {
    if (contingencyPanel) contingencyPanel.classList.remove("open");
    isContingencyActive = false;
    originalRoute = null;
    contingencyWarningMarker = null;
    resetHealthBadges();
    if (window.render) window.render();
  }

  if (closeContingency) {
    closeContingency.addEventListener("click", closeContingencyReport);
  }

  // Listen to Reset button click to clean up
  if (resetBtn) {
    resetBtn.addEventListener("click", () => {
      closeContingencyReport();
    });
  }

  // Main Anomaly Contingency triggering flow
  if (injectAnomalyBtn) {
    injectAnomalyBtn.addEventListener("click", async () => {
      // 1. Verify we have an active route
      if (!window.activeRoute || !window.activeRoute.path || window.activeRoute.path.length === 0) {
        if (window.flashHint) {
          window.flashHint("Please select route start/end nodes and plan a route first.");
        } else {
          alert("Please plan a route first.");
        }
        return;
      }

      // 2. Pause current telemetry/playback
      if (window.stopPlayback) {
        window.stopPlayback();
      }

      // 3. Clear and initialize timeline log
      if (contTimelineLog) contTimelineLog.innerHTML = "";
      addTimelineEvent("⚠ CRITICAL: ANOMALY CONTINGENCY PROTOCOL ENGAGED", true);

      // 4. Open report panel in Scanning state
      if (contingencyPanel) contingencyPanel.classList.add("open");
      contReason.textContent = "SCANNING SYSTEM DIAGNOSTICS...";
      contEffects.textContent = "ASSESSING TRAVERSAL DEGRADATION FACTORS...";
      contDecisions.textContent = "SEARCHING LOCAL RECOVERY GRID VECTOR...";
      
      ociBefore.textContent = "—";
      ociAfter.textContent = "—";
      ociDiff.textContent = "—";
      ociDiff.style.color = "var(--slate-dim)";
      
      etaCompare.textContent = "—";
      battCompare.textContent = "—";
      energyCompare.textContent = "—";
      riskCompare.textContent = "—";

      // 5. Gather state info to send to API
      const anomalyType = anomalySelect.value;
      const magVal = parseInt(anomalyMagnitude.value, 10);
      const magFraction = magVal / 100.0;

      applyHealthStatusForAnomaly(anomalyType, magVal);

      // Calculate current location index based on route progress
      const progress = window.routeProgress || 0;
      const pathLen = window.activeRoute.path.length;
      const currentIdx = Math.min(Math.floor(pathLen * progress), pathLen - 1);
      const currentCellGrid = window.activeRoute.path[currentIdx];

      // Convert current cell to lat/lon coordinates
      const currentCellData = window.cellAt(currentCellGrid.x, currentCellGrid.y);
      const currentLat = currentCellData.lat !== undefined ? currentCellData.lat : -(89.9 - currentCellGrid.y * 0.02);
      const currentLon = currentCellData.lon !== undefined ? currentCellData.lon : (currentCellGrid.x * 0.035 - 0.7);

      // Convert destination cell to lat/lon coordinates
      const endCellGrid = window.routeEnd;
      const endCellData = window.cellAt(endCellGrid.x, endCellGrid.y);
      const endLat = endCellData.lat !== undefined ? endCellData.lat : -(89.9 - endCellGrid.y * 0.02);
      const endLon = endCellData.lon !== undefined ? endCellData.lon : (endCellGrid.x * 0.035 - 0.7);

      const usePredictive = document.getElementById("routePredictiveBattery")?.checked || false;
      const initialBatteryPct = window.activeRoute.energySteps[currentIdx]?.battery ?? 100.0;

      // Construct original route waypoints list for backend metrics calculations
      const originalWaypoints = window.activeRoute.path.map((pt, i) => {
        const cData = window.cellAt(pt.x, pt.y);
        const lat = cData.lat !== undefined ? cData.lat : -(89.9 - pt.y * 0.02);
        const lon = cData.lon !== undefined ? cData.lon : (pt.x * 0.035 - 0.7);
        const est = window.activeRoute.energySteps[i] || { wh: 0, battery: 100, shadow: false };
        return {
          lat: lat,
          lon: lon,
          cumulative_distance_m: i * 140.0,
          cumulative_energy_wh: est.wh,
          battery_pct_remaining: est.battery,
          is_shadowed: est.shadow,
          solar_illumination: est.shadow ? 0.0 : 1.0
        };
      });

      // Prepare request payload
      const requestPayload = {
        region_id: window.currentRegionKey,
        current_lat: currentLat,
        current_lon: currentLon,
        end_lat: endLat,
        end_lon: endLon,
        anomaly_type: anomalyType,
        anomaly_magnitude: magFraction,
        use_predictive_battery: usePredictive,
        initial_battery_pct: initialBatteryPct,
        original_route_waypoints: originalWaypoints
      };

      console.log("Triggering contingency replanning API:", requestPayload);
      addTimelineEvent("Querying Autonomous Contingency Planner API...");

      try {
        const response = await fetch(`${window.BACKEND_BASE_URL}/api/replan-contingency`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json"
          },
          body: JSON.stringify(requestPayload)
        });

        if (!response.ok) {
          const errJson = await response.json().catch(() => ({}));
          throw new Error(errJson.detail?.reason || `HTTP status ${response.status}`);
        }

        const data = await response.json();
        console.log("Contingency response received:", data);

        // Update explanation texts
        contReason.textContent = data.explanation.reason;
        contEffects.textContent = data.explanation.effects;
        contDecisions.textContent = data.explanation.decisions;

        // Update comparisons
        ociBefore.textContent = data.metrics.before.op_confidence.toFixed(1) + "%";
        ociAfter.textContent = data.metrics.after.op_confidence.toFixed(1) + "%";

        const diff = data.metrics.after.op_confidence - data.metrics.before.op_confidence;
        ociDiff.textContent = (diff >= 0 ? "+" : "") + diff.toFixed(1) + "%";
        ociDiff.style.color = diff >= 0 ? "#05cf9b" : "#ff6b5e";

        etaCompare.textContent = `${data.metrics.before.eta_min} min → ${data.metrics.after.eta_min} min`;
        battCompare.textContent = `${Math.round(data.metrics.before.battery_pct)}% → ${Math.round(data.metrics.after.battery_pct)}%`;
        energyCompare.textContent = `${Math.round(data.metrics.before.energy_wh)} Wh → ${Math.round(data.metrics.after.energy_wh)} Wh`;
        riskCompare.textContent = `${data.metrics.before.risk.toFixed(1)} → ${data.metrics.after.risk.toFixed(1)}`;

        // Add timeline events
        addTimelineEvent("Contingency report generated successfully.");
        addTimelineEvent(`Replan computation completed in ${data.replan_time_ms.toFixed(1)} ms.`);
        addTimelineEvent(`Target updated: ${data.recovery_target.label} (${data.recovery_target.lat.toFixed(4)}°S, ${data.recovery_target.lon.toFixed(4)}°E).`);

        // Set contingency states
        originalRoute = window.activeRoute;
        isContingencyActive = true;
        contingencyWarningMarker = { x: currentCellGrid.x, y: currentCellGrid.y };

        // Map recovery path
        const mappedRoute = window.mapBackendRouteToFrontend(data);
        
        // Setup progressive draw animation
        addTimelineEvent("Initiating recovery route drawing...");
        window.activeRoute = mappedRoute;
        window.routeProgress = 0;

        let animStart = null;
        const animDuration = 800; // 800 ms progressive draw

        function drawStep(timestamp) {
          if (!animStart) animStart = timestamp;
          const elapsed = timestamp - animStart;
          const progressVal = Math.min(elapsed / animDuration, 1.0);
          window.routeProgress = progressVal;
          window.render();

          if (progressVal < 1.0) {
            requestAnimationFrame(drawStep);
          } else {
            addTimelineEvent("Recovery path loaded. Executing autonomous traversal.");
            if (window.fallbackToLocalPlayback) {
              window.fallbackToLocalPlayback();
            }
          }
        }
        requestAnimationFrame(drawStep);

      } catch (err) {
        console.error("Contingency API call failed:", err);
        addTimelineEvent(`Failed to replan contingency via API: ${err.message}`, true);
        addTimelineEvent("Falling back to local contingency simulation...", true);
        
        // Basic offline fallback calculation
        runOfflineContingencyFallback(currentCellGrid, initialBatteryPct);
      }
    });
  }

  // Robust client-side fallback contingency simulation when API is offline
  function runOfflineContingencyFallback(currentCell, startBattery) {
    const endCell = window.routeEnd;
    let localResult = null;
    let fallbackLabel = "Recovery Target";

    // Simulating specific anomaly behaviors locally
    const anomalyType = anomalySelect.value;
    if (anomalyType === "battery_drain" || anomalyType === "wheel_degradation") {
      // route to nearest sunlit cell
      const sun = window.nearestSunlitNeighbor ? window.nearestSunlitNeighbor(currentCell.x, currentCell.y) : null;
      if (sun) {
        localResult = window.buildRoute(currentCell, sun);
        fallbackLabel = "Emergency Charging Site (Local Fallback)";
      }
    }

    if (!localResult) {
      // standard route to destination
      localResult = window.buildRoute(currentCell, endCell);
      fallbackLabel = "Original Destination (Local Fallback)";
    }

    if (!localResult) {
      addTimelineEvent("Offline Contingency: No safe recovery path can be calculated locally. Rover stalled.", true);
      return;
    }

    // Populate offline report metrics
    contReason.textContent = `Simulation contingency triggered. Type: ${anomalyType}`;
    contEffects.textContent = `API offline fallback mode. Traversals estimated locally.`;
    contDecisions.textContent = `Redirecting rover to closest recovery target coordinate.`;

    ociBefore.textContent = "85.0%";
    ociAfter.textContent = "72.0%";
    ociDiff.textContent = "-13.0%";
    ociDiff.style.color = "#ff6b5e";

    etaCompare.textContent = `Estimated local traversal recalculation...`;
    battCompare.textContent = `${Math.round(startBattery)}% → ${localResult.energySteps[localResult.energySteps.length - 1]?.battery || 0}%`;
    energyCompare.textContent = `0 Wh → ${localResult.totalWh} Wh`;
    riskCompare.textContent = `Elevated`;

    originalRoute = window.activeRoute;
    isContingencyActive = true;
    contingencyWarningMarker = { x: currentCell.x, y: currentCell.y };

    window.activeRoute = localResult;
    window.routeProgress = 0;

    let animStart = null;
    function drawStep(timestamp) {
      if (!animStart) animStart = timestamp;
      const elapsed = timestamp - animStart;
      const progressVal = Math.min(elapsed / 800, 1.0);
      window.routeProgress = progressVal;
      window.render();

      if (progressVal < 1.0) {
        requestAnimationFrame(drawStep);
      } else {
        addTimelineEvent("Local contingency path loaded. Running simulation playback.");
        if (window.fallbackToLocalPlayback) {
          window.fallbackToLocalPlayback();
        }
      }
    }
    requestAnimationFrame(drawStep);
  }

})();
