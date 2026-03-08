import numpy as np
import os
import json
import time
import threading

class NeuralBrain:
    """
    Lightweight Neural Network (MLP) for Trading Decisions.
    Implemented as a Singleton to prevent redundant disk I/O when loading weights.
    """
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super(NeuralBrain, cls).__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self, input_size, hidden_size=12, output_size=1, learning_rate=0.01):
        if self._initialized:
            return
            
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.output_size = output_size
        self.learning_rate = learning_rate
        
        # Initialize Weights (He Initialization for ReLU)
        self.weights = {
            'W1': np.random.randn(self.input_size, self.hidden_size) * np.sqrt(2. / self.input_size),
            'b1': np.zeros((1, self.hidden_size)),
            'W2': np.random.randn(self.hidden_size, self.output_size) * np.sqrt(2. / self.hidden_size),
            'b2': np.zeros((1, self.output_size))
        }
        
        self.model_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'brain_weights.json')
        self.is_trained = False
        self.load_model()
        
        self._initialized = True

    def relu(self, z):
        return np.maximum(0, z)
    
    def sigmoid(self, z):
        # Clip z to prevent overflow
        return 1 / (1 + np.exp(-np.clip(z, -500, 500)))
    
    def predict(self, feature_vector):
        """
        Forward pass.
        Args:
            feature_vector: list or np.array of normalized features
        Returns:
            confidence: float (0.0 to 1.0)
        """
        x = np.array(feature_vector).reshape(1, -1)
        
        # Layer 1
        z1 = np.dot(x, self.weights['W1']) + self.weights['b1']
        a1 = self.relu(z1)
        
        # Layer 2 (Output)
        z2 = np.dot(a1, self.weights['W2']) + self.weights['b2']
        output = self.sigmoid(z2)
        
        return float(output[0][0])
    
    async def save_model_to_db(self, env='LIVE', stats=None):
        """Async save to database."""
        try:
            from src.infrastructure.repository.database import DataManager
            weights = {
                'W1': self.weights['W1'].tolist(),
                'b1': self.weights['b1'].tolist(),
                'W2': self.weights['W2'].tolist(),
                'b2': self.weights['b2'].tolist()
            }
            weights_json = json.dumps(weights)
            
            db = await DataManager.get_instance()
            accuracy = stats.get('accuracy', 0) if stats else 0
            mse = stats.get('mse', 0) if stats else 0
            samples = stats.get('samples', 0) if stats else 0
            
            await db.save_ai_model('neural_brain', env, weights_json, accuracy, mse, samples)
            print(f"💾 [BRAIN] Weights saved to database ({env})")
        except Exception as e:
            print(f"❌ [BRAIN] Failed to save to DB: {e}")

    def save_model(self):
        """Legacy synchronous save to file (fallback and unit tests)."""
        try:
            serializable_weights = {k: v.tolist() for k, v in self.weights.items()}
            with open(self.model_path, 'w') as f:
                json.dump(serializable_weights, f)
            print(f"🧠 [BRAIN] Weights saved to local file: {self.model_path}")
        except Exception as e:
            print(f"❌ [BRAIN] Failed to save to file: {e}")

    def load_model(self):
        """Synchronous load from file (as starting point)."""
        if os.path.exists(self.model_path):
            try:
                with open(self.model_path, 'r') as f:
                    data = json.load(f)
                
                # Load and Validate shapes
                for k in ['W1', 'b1', 'W2', 'b2']:
                    if k in data:
                        loaded_val = np.array(data[k])
                        if loaded_val.shape == self.weights[k].shape:
                            self.weights[k] = loaded_val
                        else:
                            print(f"⚠️ [BRAIN] Shape mismatch for {k}, using random.")
                
                self.is_trained = True
                print("🧠 [BRAIN] Weights loaded from file.")
            except Exception as e:
                print(f"⚠️ [BRAIN] Error loading weights: {e}")
        else:
            # print("🆕 [BRAIN] No weights file found, using random initialization.")
            pass

    async def sync_from_db(self, env='LIVE'):
        """Async update weights from database."""
        try:
            from src.infrastructure.repository.database import DataManager
            db = await DataManager.get_instance()
            model_data = await db.get_ai_model('neural_brain', env)
            
            if model_data and model_data.get('weights'):
                data = model_data['weights']
                for k in ['W1', 'b1', 'W2', 'b2']:
                    if k in data:
                        loaded_val = np.array(data[k])
                        if loaded_val.shape == self.weights[k].shape:
                            self.weights[k] = loaded_val
                self.is_trained = True
                print(f"🧠 [BRAIN] Weights synchronized from database ({env}).")
        except Exception as e:
            print(f"⚠️ [BRAIN] Sync error: {e}")

    def train(self, inputs, targets, epochs=1):
        """
        Train using simple SGD (Backpropagation).
        inputs: list of feature vectors
        targets: list of floats (1.0 for WIN, 0.0 for LOSS)
        """
        inputs = np.array(inputs)
        targets = np.array(targets).reshape(-1, 1)
        
        for _ in range(epochs):
            total_error = 0
            for i in range(len(inputs)):
                x = inputs[i].reshape(1, -1) # (1, input_size)
                y = targets[i].reshape(1, 1) # (1, 1)
                
                # Forward
                z1 = np.dot(x, self.weights['W1']) + self.weights['b1']
                a1 = self.relu(z1)
                z2 = np.dot(a1, self.weights['W2']) + self.weights['b2']
                y_hat = self.sigmoid(z2)
                
                # Error
                error = y_hat - y # (1, 1)
                total_error += np.sum(error**2)
                
                # Backward (Chain Rule)
                delta2 = error * (y_hat * (1 - y_hat)) # (1, 1)
                dW2 = np.dot(a1.T, delta2)
                db2 = np.sum(delta2, axis=0, keepdims=True)
                
                delta1 = np.dot(delta2, self.weights['W2'].T) * (z1 > 0)
                dW1 = np.dot(x.T, delta1)
                db1 = np.sum(delta1, axis=0, keepdims=True)
                
                # Update
                self.weights['W1'] -= self.learning_rate * dW1
                self.weights['b1'] -= self.learning_rate * db1
                self.weights['W2'] -= self.learning_rate * dW2
                self.weights['b2'] -= self.learning_rate * db2
                
        return total_error / len(inputs)

if __name__ == "__main__":
    # Test Brain
    print("🧠 Testing Neural Brain...")
    brain = NeuralBrain(input_size=10)
    
    # Fake Input (Normalized)
    inputs = [0.5, 0.2, 0.8, -0.1, 0.0, 0.9, 0.4, 0.3, 0.1, 0.5]
    
    t0 = time.time()
    score = brain.predict(inputs)
    t1 = time.time()
    
    print(f"Input: {inputs}")
    print(f"Output Score: {score:.4f}")
    print(f"Inference Time: {(t1-t0)*1000:.3f}ms")
    
    # Save/Load Test
    brain.save_model()
    brain.load_model()
