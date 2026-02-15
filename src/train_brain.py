
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

def main():
    print("🧠 Starting Neural Brain Training...")
    
    # 1. Load Data
    inputs, targets = load_training_data()
    
    if len(inputs) < 10:
        print(f"⚠️ Not enough training data. Found {len(inputs)} samples, need at least 10.")
        return
    
    print(f"📊 Dataset size: {len(inputs)} samples")
    
    # 2. Split Data (80% Train, 20% Test)
    split_idx = int(len(inputs) * 0.8)
    indices = np.arange(len(inputs))
    np.random.shuffle(indices)
    
    train_idx = indices[:split_idx]
    test_idx = indices[split_idx:]
    
    X_train = np.array(inputs)[train_idx]
    y_train = np.array(targets)[train_idx]
    
    X_test = np.array(inputs)[test_idx]
    y_test = np.array(targets)[test_idx]
    
    # 3. Init Brain
    brain = NeuralBrain(input_size=12)
    print("🧠 Brain initialized.")
    
    # Evaluate before training
    print("\n--- Pre-Training Evaluation ---")
    evaluate(brain, X_test, y_test)
    
    # 4. Train
    print(f"\n💪 Training on {len(X_train)} samples for 100 epochs...")
    final_loss = brain.train(X_train.tolist(), y_train.tolist(), epochs=100)
    print(f"✅ Training Complete. Final Loss: {final_loss:.4f}")
    
    # 5. Evaluate after training
    print("\n--- Post-Training Evaluation ---")
    evaluate(brain, X_test, y_test)
    
    # 6. Save
    brain.save_model()
    print("💾 Brain saved successfully!")

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

if __name__ == "__main__":
    main()
