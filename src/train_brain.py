
import json
import os
import numpy as np
import sys

# Add src to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from neural_brain import NeuralBrain

HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'signal_performance.json')

def load_training_data():
    if not os.path.exists(HISTORY_FILE):
        print("⚠️ No history file found.")
        return [], []
    
    try:
        with open(HISTORY_FILE, 'r') as f:
            data = json.load(f)
    except Exception as e:
        print(f"⚠️ Error loading history: {e}")
        return [], []
        
    trades = data.get('trades', [])
    print(f"📚 Found {len(trades)} total trades in history.")
    
    inputs = []
    targets = []
    
    for t in trades:
        # We need both result and snapshot to train
        if 'result' not in t or 'snapshot' not in t:
            continue
            
        snapshot = t['snapshot']
        if not snapshot:
            continue
            
        # Convert snapshot dict to list (ORDER MATTERS - must match strategy.py)
        # Strategy order: 
        # [RSI_7, RSI_14, RSI_21, MACD, BB_W, P_BB, Vol, ADX, ATR, PnL, Lev, Eq]
        
        # Use explicit key access to ensure order
        try:
            # Handle both list (legacy) and dict (new) snapshots if any
            if isinstance(snapshot, list):
                feature_vector = snapshot
            else:
                feature_vector = [
                    snapshot.get('norm_RSI_7', 0.5),
                    snapshot.get('norm_RSI_14', 0.5),
                    snapshot.get('norm_RSI_21', 0.5),
                    snapshot.get('norm_MACD', 0.5),
                    snapshot.get('norm_BB_Width', 0.5),
                    snapshot.get('norm_Price_in_BB', 0.5),
                    snapshot.get('norm_Volume', 0.0),
                    snapshot.get('norm_ADX', 0.0),
                    snapshot.get('norm_ATR', 0.0),
                    snapshot.get('state_pnl_pct', 0.0),
                    snapshot.get('state_leverage', 0.0),
                    snapshot.get('state_equity_ratio', 1.0)
                ]
                
            # Add Dynamic Context Features (v4.0) - Always 17 features now
            entry = t.get('entry_price', 1)
            side_mult = 1 if t.get('side') == 'BUY' else -1
            
            # SL Distances (Normalized: 0 = -5%, 0.5 = 0%, 1 = +5%)
            sl_orig = t.get('sl_original', t.get('entry_price'))
            sl_final = t.get('sl_final', t.get('entry_price'))
            
            dist_orig = ((sl_orig - entry) / entry) * side_mult if entry else 0
            dist_final = ((sl_final - entry) / entry) * side_mult if entry else 0
            
            norm_sl_orig = np.clip((dist_orig + 0.05) / 0.1, 0, 1)
            norm_sl_final = np.clip((dist_final + 0.05) / 0.1, 0, 1)
            
            dynamic_features = [
                norm_sl_orig,
                norm_sl_final,
                min(t.get('sl_move_count', 0) / 10.0, 1.0),
                1.0 if t.get('sl_tightened') else 0.0,
                min(t.get('max_pnl_pct', 0) / 10.0, 1.0)
            ]
            
            feature_vector.extend(dynamic_features)
                
            # Clean NaNs
            feature_vector = [0.5 if x is None else x for x in feature_vector]
            
            inputs.append(feature_vector)
            
            # Target: WIN = 1.0, LOSS = 0.0
            target = 1.0 if t['result'] == 'WIN' else 0.0
            targets.append(target)
            
        except Exception as e:
            print(f"Skipping bad record: {e}")
            continue
            
    return inputs, targets

    # 6. Save
    brain.save_model()
    print("💾 Brain saved successfully!")
    return brain

def evaluate(brain, X, y):
    if len(X) == 0:
        print("No test data.")
        return
        
    correct = 0
    mse = 0
    
    for i in range(len(X)):
        pred = brain.predict(X[i])
        target = y[i]
        
        # Binary classification accuracy (threshold 0.5)
        pred_class = 1.0 if pred >= 0.5 else 0.0
        if pred_class == target:
            correct += 1
            
        mse += (pred - target) ** 2
        
    acc = correct / len(X) * 100
    avg_mse = mse / len(X)
    print(f"Accuracy: {acc:.1f}% | MSE: {avg_mse:.4f}")
    return acc, avg_mse

def run_nn_training(min_samples=10, epochs=100):
    """
    Function to be called by automated maintenance scripts.
    """
    print("\n" + "🧠" * 30)
    print("🧠 STARTING AUTOMATED NEURAL BRAIN TRAINING")
    print("🧠" * 30 + "\n")
    
    # 1. Load Data
    inputs, targets = load_training_data()
    
    if len(inputs) < min_samples:
        print(f"⚠️ [BRAIN] Not enough training data. Found {len(inputs)} samples, need at least {min_samples}.")
        return False
    
    print(f"📊 [BRAIN] Dataset size: {len(inputs)} samples")
    
    # 2. Split Data
    split_idx = int(len(inputs) * 0.8)
    indices = np.arange(len(inputs))
    np.random.shuffle(indices)
    
    X_train = np.array(inputs)[indices[:split_idx]]
    y_train = np.array(targets)[indices[:split_idx]]
    X_test = np.array(inputs)[indices[split_idx:]]
    y_test = np.array(targets)[indices[split_idx:]]
    
    # 3. Training
    brain = NeuralBrain(input_size=17)
    print("💪 [BRAIN] Training model...")
    final_loss = brain.train(X_train.tolist(), y_train.tolist(), epochs=epochs)
    
    # 4. Evaluation
    print("--- Brain Performance ---")
    acc, mse = evaluate(brain, X_test, y_test)
    
    # 5. Save
    brain.save_model()
    print("✅ [BRAIN] Neural model updated and saved.\n")
    return {"status": "success", "accuracy": acc, "mse": mse, "samples": len(inputs)}

if __name__ == "__main__":
    run_nn_training()
