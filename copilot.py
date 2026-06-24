from schemas import ExplainRequest, ExplainResponse
from cost_grid import CostGrid

def explain_routing_decision(request: ExplainRequest, grid: CostGrid) -> ExplainResponse:
    """
    Explain routing or scoring decisions based on underlying metrics.
    For this MVP, we use rule-based templating. A full version could pass these metrics to an LLM.
    """
    query = request.question.lower()
    ctx = request.context
    
    answer = "I'm sorry, I couldn't understand the context of your question. Could you clarify if you're asking about pitstops, LMRS, hazards, or energy?"
    
    if "pitstop" in query or "solar" in query or "why" in query and "added" in query:
        answer = (
            "The solar charging pitstop was added because the predicted continuous dark-dwell time "
            "exceeded the maximum budget of 30 minutes. To prevent thermal and battery failure in extreme cold, "
            "the pathfinder dynamically routed to the nearest sunlit cell to recharge before continuing."
        )
    elif "lmrs" in query or "score" in query:
        answer = (
            "The Lunar Mining Readiness Score (LMRS) is a composite index. A low score usually indicates "
            "deep ice (>2m), high distance (>2km), poor Earth line-of-sight (crater shadows), or extreme "
            "thermal risk from long shadow exposure. High scores indicate easily accessible surface ice in sunlit areas."
        )
    elif "hazard" in query or "corridor" in query or "cost" in query:
        if ctx.lat is not None and ctx.lon is not None:
            # Check cost at point
            x, y = int(ctx.lon * 10000), int(ctx.lat * 10000)
            
            # bounds check
            x = max(0, min(grid.width - 1, x))
            y = max(0, min(grid.height - 1, y))
            
            cost = grid.get_traversal_cost(x, y)
            is_shadow = grid.is_in_shadow(x, y)
            
            if cost == float('inf'):
                answer = "This specific corridor is classified as hazardous because it contains a hard obstacle (boulder) or a slope > 30°."
            elif cost > 1.5:
                answer = f"This corridor has an elevated traversal cost due to a significant slope (~{int((cost-1)*100)}° penalty factor)."
            elif is_shadow:
                answer = "This corridor is hazardous due to permanent shadow, incurring a massive routing cost penalty to avoid thermal risk."
            else:
                answer = "This corridor appears relatively safe, with standard traversal costs."
        else:
            answer = "Hazardous corridors are typically marked by slopes > 30°, large boulders, or deep shadows requiring excessive heating energy."
    elif "energy" in query or "battery" in query:
         answer = (
            "Energy consumption is dynamically predicted using our machine learning battery model. "
            "It considers base traversal, slope resistance, and a heavy heating penalty (50 Wh/m) "
            "when traversing shadowed, extremely cold regions (-248°C)."
         )
         
    return ExplainResponse(answer=answer)
