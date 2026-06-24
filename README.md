# AstraNav-LRIS Backend

Backend API for Lunar Resource Intelligence & Autonomous Navigation System (AstraNav-LRIS).
Built for ISRO's Bharatiya Antariksh Hackathon 2026.

## Scope
Provides features 2 through 9: Multi-Objective Terrain Hazard Masking, Shadow-Hopping Pathfinder, Lunar Mining Readiness Score (LMRS), Multi-Site Comparison, Multi-Rover Swarm View, Voice/Chat Copilot, Predictive Battery Model, and Confidence Overlay.

## Tech Stack
* Python 3.12
* FastAPI (async)
* Pydantic v2
* scikit-learn, numpy, scipy
* pytest

## Running Locally

1. Create a virtual environment and install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Generate synthetic data and train the predictive battery model:
   ```bash
   python battery_model.py
   ```

3. Run the FastAPI development server:
   ```bash
   uvicorn main:app --reload
   ```

4. View the Swagger UI documentation at:
   `http://localhost:8000/docs`

## Tests
Run the test suite with `pytest`:
```bash
pytest test_astranav.py
```

*Note: All inputs dependent on Member 1's signal processing (Ice Volume, Hazard Map, Confidence signal noise) are currently mocked and marked with `# MOCK DATA`. They can be cleanly replaced with actual API integrations later.*
