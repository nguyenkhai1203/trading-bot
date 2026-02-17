import numpy as np
import os
import json
import time

class NeuralBrain:
    """
    Lightweight Neural Network (MLP) for Trading Decisions.
    
    Architecture:
    - Input Layer: Matches feature count (e.g., RSI, MACD, Vol, State) ~ 15-20 nodes
    - Hidden Layer: 8-12 nodes (ReLU activation)
    - Output Layer: 1 node (Sigmoid activation -> 0.0 to 1.0 confidence)
    
    Why Numpy?
    - Zero dependency (no PyTorch/TensorFlow needed for inference)
    - Extreme speed (<1ms inference)
    - Easy to serialize (JSON weights)
    """
    
    def __init__(self, input_size, hidden_size=12, output_size=1, learning_rate=0.01):
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
    
    def save_model(self):
        """Serialize weights to JSON."""
        serializable_weights = {k: v.tolist() for k, v in self.weights.items()}
        try:
            with open(self.model_path, 'w') as f:
                json.dump(serializable_weights, f)
            print("🧠 Brain saved successfully.")
        except Exception as e:
            print(f"❌ Failed to save brain: {e}")

    def load_model(self):
        """Load weights from JSON."""
        if not os.path.exists(self.model_path):
            # print("🧠 No existing brain found. Created new random brain.")
            return
            
        try:
            with open(self.model_path, 'r') as f:
                data = json.load(f)
                
            # Load and Validate
            loaded_W1 = np.array(data['W1'])
            
            if loaded_W1.shape != (self.input_size, self.hidden_size):
                print(f"⚠️ Shape mismatch: Loaded {loaded_W1.shape} != Expected {(self.input_size, self.hidden_size)}")
                raise ValueError("Shape mismatch - Re-initializing")
                
            self.weights['W1'] = loaded_W1
            self.weights['b1'] = np.array(data['b1'])
            self.weights['W2'] = np.array(data['W2'])
            self.weights['b2'] = np.array(data['b2'])
            self.is_trained = True
            print("🧠 Brain loaded successfully.")
        except Exception as e:
            print(f"⚠️ Failed to load brain ({e}). Using random weights.")

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
                # dL/dy_hat = error
                # dy_hat/dz2 = y_hat * (1 - y_hat) [Sigmoid derivative]
                delta2 = error * (y_hat * (1 - y_hat)) # (1, 1)
                
                # dL/dW2 = a1.T * delta2
                dW2 = np.dot(a1.T, delta2)
                db2 = np.sum(delta2, axis=0, keepdims=True)
                
                # dL/da1 = delta2 * W2.T
                # da1/dz1 = 1 if z1 > 0 else 0 [ReLU derivative]
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
