import numpy as np
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
import joblib
import os

MODEL_PATH = os.path.join(os.path.dirname(__file__), "battery_model.joblib")

def generate_synthetic_data(n_samples=5000):
    # Features: ambient_temp_k, is_in_shadow, slope_deg, speed_mps, distance_m
    ambient_temp_k = np.random.uniform(40, 400, n_samples)
    is_in_shadow = np.random.randint(0, 2, n_samples)
    slope_deg = np.random.uniform(0, 30, n_samples)
    speed_mps = np.random.uniform(0.01, 0.1, n_samples)
    distance_m = np.random.uniform(1, 10, n_samples)
    
    # Base energy = distance * 20 Wh/m
    energy = distance_m * 20.0
    
    # Shadow penalty (heaters need to work)
    # If in shadow, it's very cold. Let's say being in shadow adds a flat 50 Wh per meter for heating.
    energy += is_in_shadow * distance_m * 50.0
    
    # Temperature penalty (even in sun, if it's cold, need some heat)
    # Say optimal temp is 300K.
    temp_penalty = np.maximum(0, 300 - ambient_temp_k) * distance_m * 0.1
    energy += temp_penalty
    
    # Slope penalty
    # Steeper slopes require more energy to traverse.
    slope_factor = 1.0 + (slope_deg / 20.0)
    energy *= slope_factor
    
    # Add realistic variance (noise)
    energy += np.random.normal(0, distance_m * 2.0, n_samples)
    # Ensure energy is strictly positive
    energy = np.maximum(energy, distance_m * 5.0)
    
    X = np.column_stack((ambient_temp_k, is_in_shadow, slope_deg, speed_mps, distance_m))
    y = energy
    return X, y

def train_and_save_model():
    print("Generating synthetic data and training battery model...")
    X, y = generate_synthetic_data(10000)
    # Using a simple Ridge regression pipeline with scaling for robustness
    model = make_pipeline(StandardScaler(), Ridge(alpha=1.0))
    model.fit(X, y)
    joblib.dump(model, MODEL_PATH)
    print(f"Model saved to {MODEL_PATH}")

# Global model instance
_model = None

def get_model():
    global _model
    if _model is None:
        if not os.path.exists(MODEL_PATH):
            train_and_save_model()
        _model = joblib.load(MODEL_PATH)
    return _model

def predict_energy_wh(ambient_temp_k: float, is_in_shadow: bool, slope_deg: float, speed_mps: float, distance_m: float) -> float:
    """
    Predicts energy consumption in Watt-hours for a route segment.
    """
    model = get_model()
    # Scikit-learn expects 2D array
    features = np.array([[ambient_temp_k, float(is_in_shadow), slope_deg, speed_mps, distance_m]])
    prediction = float(model.predict(features)[0])
    return max(0.0, prediction) # energy consumed must be non-negative

if __name__ == "__main__":
    train_and_save_model()
