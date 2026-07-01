# pyrefly: ignore [missing-import]
import google.generativeai as genai
import os
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(tags=["Copilot"])

class CopilotRequest(BaseModel):
    question: str
    context: dict = {}

@router.post("/api/copilot")
async def copilot_answer(req: CopilotRequest):
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise HTTPException(status_code=503, detail="GEMINI_API_KEY not configured")
    
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-1.5-flash")
        
        prompt = f"""You are a mission control AI assistant for AstraNav-LRIS, 
a lunar resource intelligence system analyzing Chandrayaan-2 data for 
ISRO's south polar region exploration.

Current mission context:
{req.context}

User question: {req.question}

Answer concisely and technically, referencing the specific data values 
provided in the context. Keep response under 100 words."""

        response = model.generate_content(prompt)
        return {"answer": response.text, "source": "gemini"}
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
