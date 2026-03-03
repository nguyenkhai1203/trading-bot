import asyncio
import json
import os
import numpy as np
import sys
from datetime import datetime

# Add src to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from neural_brain import NeuralBrain
from database import DataManager

async def load_training_data(env='LIVE'):
    """Fetch training data from ai_training_logs table."""
    db = await DataManager.get_instance(env)
    
    # Join with trades to get the actual result (WIN/LOSS)
    query = """
        SELECT l.snapshot_json, l.entry_confidence, t.status, t.pnl, t.exit_reason
        FROM ai_training_logs l
        JOIN trades t ON l.trade_id = t.id
        WHERE t.status IN ('CLOSED', 'ACTIVE')
    """
    
    inputs = []
    targets = []
    
    async with (await db.get_db()).execute(query) as cursor:
        rows = await cursor.fetchall()
        print(f"📚 Found {len(rows)} training samples in database ({env}).")
        
        for row in rows:
            try:
                snapshot = json.loads(row['snapshot_json'])
                
                # Standard Feature Extraction (consistent with strategy.py)
                feature_vector = [
                    snapshot.get('norm_RSI_7', 0.5),
                    snapshot.get('norm_RSI_14', 0.5),
                    snapshot.get('norm_RSI_21', 0.5),
                    snapshot.get('norm_MACD', 0.5),
                    snapshot.get('norm_BB_Width', 0.5),
                    snapshot.get('norm_Price_in_BB', 0.5),
                    snapshot.get('norm_Volume', 0.5),
                    snapshot.get('norm_ADX', 0.5),
                    snapshot.get('norm_ATR', 0.5),
                    snapshot.get('state_pnl_pct', 0.0),
                    snapshot.get('state_leverage', 0.0),
                    snapshot.get('state_equity_ratio', 1.0)
                ]
                
                # Dynamic Features (consistent with v4.0)
                # These might need normalization logic if not preserved in snapshot
                # For now, we assume primary 12 features + 5 dynamic = 17
                # If snapshot is missing dynamic ones, we use defaults
                dynamic_features = [
                    snapshot.get('norm_sl_orig', 0.5),
                    snapshot.get('norm_sl_final', 0.5),
                    snapshot.get('sl_move_count_norm', 0.0),
                    1.0 if snapshot.get('sl_tightened') else 0.0,
                    snapshot.get('max_pnl_norm', 0.0)
                ]
                feature_vector.extend(dynamic_features)
                
                # Fill NaNs
                feature_vector = [0.5 if (x is None or not np.isfinite(x)) else float(x) for x in feature_vector]
                
                # Target: WIN if PnL > 0, else LOSS
                # Note: For ACTIVE trades, this is technically "ongoing", but usually we only train on CLOSED.
                if row['status'] == 'CLOSED':
                    target = 1.0 if row['pnl'] > 0 else 0.0
                    inputs.append(feature_vector)
                    targets.append(target)
            except Exception as e:
                print(f"⚠️ Skipping bad record: {e}")
                continue
                
    return inputs, targets

async def run_nn_training(min_samples=10, epochs=100, env='LIVE'):
    """Main training entry point."""
    print("\n" + "🧠" * 30)
    print(f"🧠 STARTING NEURAL BRAIN TRAINING ({env})")
    print("🧠" * 30 + "\n")
    
    # 1. Load Data
    inputs, targets = await load_training_data(env)
    
    if len(inputs) < min_samples:
        print(f"⚠️ [BRAIN] Not enough data. Found {len(inputs)}, need {min_samples}.")
        return {"status": "skipped", "reason": "not_enough_data"}
    
    # 2. Split (80/20)
    indices = np.arange(len(inputs))
    np.random.shuffle(indices)
    split = int(len(inputs) * 0.8)
    
    X_train = np.array(inputs)[indices[:split]]
    y_train = np.array(targets)[indices[:split]]
    X_test = np.array(inputs)[indices[split:]]
    y_test = np.array(targets)[indices[split:]]
    
    # 3. Model Init & Training
    brain = NeuralBrain(input_size=17)
    await brain.sync_from_db(env) # Start from latest DB weights
    
    print(f"💪 Training on {len(X_train)} samples...")
    brain.train(X_train.tolist(), y_train.tolist(), epochs=epochs)
    
    # 4. Evaluation
    correct = 0
    mse_total = 0
    for i in range(len(X_test)):
        pred = brain.predict(X_test[i])
        target = y_test[i]
        if (pred >= 0.5) == (target >= 0.5):
            correct += 1
        mse_total += (pred - target) ** 2
    
    accuracy = (correct / len(X_test)) * 100 if len(X_test) > 0 else 0
    mse = mse_total / len(X_test) if len(X_test) > 0 else 0
    
    print(f"📊 Results: Accuracy={accuracy:.1f}%, MSE={mse:.4f}")
    
    # 5. Save to DB
    stats = {
        'accuracy': accuracy,
        'mse': mse,
        'samples': len(inputs)
    }
    await brain.save_model_to_db(env, stats)
    
    return {"status": "success", "accuracy": accuracy, "mse": mse, "samples": len(inputs)}

if __name__ == "__main__":
    try:
        asyncio.run(run_nn_training())
    finally:
        from database import DataManager
        asyncio.run(DataManager.clear_instances())
