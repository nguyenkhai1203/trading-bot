import pytest
import numpy as np
import os
import json
from neural_brain import NeuralBrain

class TestNeuralBrain:
    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset NeuralBrain singleton before each test."""
        NeuralBrain._instance = None
        yield

    @pytest.fixture
    def brain(self, tmp_path):
        """Create a NeuralBrain instance with a temporary weights path."""
        # Use a custom path to avoid overwriting real weights
        custom_path = str(tmp_path / "test_brain_weights.json")
        nb = NeuralBrain(input_size=10, hidden_size=8)
        nb.model_path = custom_path
        return nb

    def test_singleton_pattern(self):
        """Verify that NeuralBrain is a singleton."""
        b1 = NeuralBrain(input_size=10)
        b2 = NeuralBrain(input_size=10)
        assert b1 is b2

    def test_initialization(self, brain):
        """Check if weights are initialized with correct shapes."""
        assert brain.weights['W1'].shape == (10, 8)
        assert brain.weights['b1'].shape == (1, 8)
        assert brain.weights['W2'].shape == (8, 1)
        assert brain.weights['b2'].shape == (1, 1)

    def test_predict_output_range(self, brain):
        """Forward pass should return a float between 0 and 1."""
        input_data = np.random.randn(10).tolist()
        score = brain.predict(input_data)
        assert 0.0 <= score <= 1.0
        assert isinstance(score, float)

    def test_save_and_load_model(self, brain):
        """Verify that weights can be saved and reloaded exactly."""
        # Set specific weights
        brain.weights['W1'] = np.ones((10, 8)) * 0.5
        brain.save_model()
        
        assert os.path.exists(brain.model_path)
        
        # Reset and reload
        brain.weights['W1'] = np.zeros((10, 8))
        brain.load_model()
        
        assert np.all(brain.weights['W1'] == 0.5)
        assert brain.is_trained is True

    def test_load_model_shape_mismatch(self, brain, tmp_path):
        """Verify that mismatching weights trigger a reset."""
        # Save a model with 5 inputs
        bad_weights = {
            'W1': np.ones((5, 8)).tolist(),
            'b1': np.zeros((1, 8)).tolist(),
            'W2': np.ones((8, 1)).tolist(),
            'b2': np.zeros((1, 1)).tolist()
        }
        with open(brain.model_path, 'w') as f:
            json.dump(bad_weights, f)
            
        # Try to load into a 10-input brain
        brain.load_model()
        
        # Should remain on random weights (not the 5-input ones)
        assert brain.weights['W1'].shape == (10, 8)

    def test_train_loop(self, brain):
        """Verify that training decreases error for simple patterns."""
        inputs = [
            [1.0] * 10,
            [0.0] * 10
        ]
        targets = [1.0, 0.0] # Pattern: All 1s -> 1.0, All 0s -> 0.0
        
        initial_error = brain.train(inputs, targets, epochs=1)
        
        # Train for multiple epochs
        for _ in range(50):
            last_error = brain.train(inputs, targets, epochs=1)
            
        assert last_error < initial_error
