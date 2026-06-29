// optimizer.js - Budget-Constrained Mission Optimizer

/**
 * Finds the optimal ice site based on an energy budget.
 * Iterates through all ice candidates in the region and ranks them
 * by efficiency (ice yield per Wh).
 * 
 * @param {Object} region - The current region object
 * @param {Number} budgetWh - The maximum allowed energy cost in Wh
 * @returns {Object} { recommended, runnersUp, rejected, minBudget }
 */
function findOptimalSite(region, budgetWh) {
  const eligible = [];
  const rejected = [];
  let minReachableWh = Infinity;

  // 1. Enumerate all ice candidates in the region
  for (let y = 0; y < region.rows; y++) {
    for (let x = 0; x < region.cols; x++) {
      const cell = region.cells[y][x];
      if (cell.ice) {
        // 2. Compute exact LMRS and routing constraints
        const result = computeLMRS(x, y);
        
        // Track the absolute cheapest reachable site across the board
        if (result.reachable && result.energyWh < minReachableWh) {
          minReachableWh = result.energyWh;
        }

        if (!result.reachable) {
          rejected.push({
            cell: cell,
            result: result,
            reason: "Unreachable — no safe corridor"
          });
        } else if (result.energyWh > budgetWh) {
          rejected.push({
            cell: cell,
            result: result,
            reason: `Exceeds budget by ${Math.round(result.energyWh - budgetWh)} Wh`
          });
        } else {
          // 3. Compute Value-per-Budget Efficiency
          // (m3 of ice per watt-hour)
          const efficiency = result.ice.volume_m3 / result.energyWh;
          eligible.push({
            cell: cell,
            result: result,
            efficiency: efficiency
          });
        }
      }
    }
  }

  // Sort eligible sites: Highest efficiency first. Tie-breaker: Highest LMRS.
  eligible.sort((a, b) => {
    if (Math.abs(a.efficiency - b.efficiency) < 0.1) {
      if (a.result.LMRS !== b.result.LMRS) {
        return b.result.LMRS - a.result.LMRS;
      }
      return a.result.energyWh - b.result.energyWh; // Cheapest wins tie
    }
    return b.efficiency - a.efficiency;
  });

  // Sort rejected sites by cheapest first so runners-up make sense
  rejected.sort((a, b) => a.result.energyWh - b.result.energyWh);

  const best = eligible.length > 0 ? eligible[0] : null;
  const runnersUp = [];

  // Generate the explanation string for the best site
  if (best) {
    if (eligible.length > 1) {
      const runnerUp = eligible[1];
      if (runnerUp.result.ice.volume_m3 > best.result.ice.volume_m3) {
        best.explanation = `Within your ${budgetWh} Wh budget, this site delivers the highest ice yield per Wh. The next-best candidate has more ice but is less energy-efficient.`;
      } else {
        best.explanation = `Within your ${budgetWh} Wh budget, this site delivers the highest ice yield per Wh.`;
      }
    } else if (rejected.length > 0) {
      // Find a rejected site that had more ice
      const betterIceRejected = rejected.find(r => r.result.ice && r.result.ice.volume_m3 > best.result.ice.volume_m3);
      if (betterIceRejected) {
        best.explanation = `Within your ${budgetWh} Wh budget, this site delivers the highest ice yield per Wh. Another candidate has more ice but would exceed your budget by ${Math.round(betterIceRejected.result.energyWh - budgetWh)} Wh.`;
      } else {
        best.explanation = `This is the only site that fits within your ${budgetWh} Wh budget.`;
      }
    } else {
      best.explanation = `This site delivers the highest ice yield per Wh within budget.`;
    }

    // Populate runners up from eligible
    for (let i = 1; i < Math.min(4, eligible.length); i++) {
      const r = eligible[i];
      const percentLess = Math.round((1 - (r.efficiency / best.efficiency)) * 100);
      runnersUp.push({
        ...r,
        reason: `${percentLess}% less efficient than recommended`
      });
    }
  }

  // Fill remaining runner-up slots with closest rejected sites
  let rejIndex = 0;
  while (runnersUp.length < 3 && rejIndex < rejected.length) {
    runnersUp.push(rejected[rejIndex]);
    rejIndex++;
  }

  return {
    recommended: best,
    runnersUp: runnersUp,
    rejected: rejected,
    minBudget: minReachableWh === Infinity ? null : Math.round(minReachableWh)
  };
}
