import pytest
import numpy as np
import os
import json
from src.neural_brain import NeuralBrain

class TestNeuralBrain:
    """
    Test suite for NeuralBrain (MLP).
    Covers: Forward pass, Training convergence, and Weight persistence.
    """

    @pytest.fixture
    def brain(self, tmp_path):
        # Redirect weight file to tmp for isolation
        with patch("src.neural_brain.os.path.join", return_value=str(tmp_path / "test_brain.json")):
            # Clear singleton for clean test
            NeuralBrain._instance = None
            return NeuralBrain(input_size=10, hidden_size=8)

    def test_brain_predict_not_null(self, brain):
        """Verify forward pass returns a valid float between 0 and 1."""
        x = [0.5] * 10
        score = brain.predict(x)
        assert isinstance(score, float)
        assert 0.0 <= score <= 1.0

    def test_brain_train_convergence(self, brain):
        """Verify that training on simple data reduces loss."""
        # Simple pattern: if first feature > 0.5 then 1, else 0
        inputs = np.random.rand(100, 10)
        targets = [1.0 if x[0] > 0.5 else 0.0 for x in inputs]
        
        # Initial loss
        initial_loss = brain.train(inputs, targets, epochs=1)
        
        # Train more
        final_loss = brain.train(inputs, targets, epochs=50)
        
        # Loss should decrease
        assert final_loss < initial_loss
        
        # Test a prediction
        test_in = [0.9] + [0.1]*9
        score = brain.predict(test_in)
        assert score > 0.5

    def test_brain_save_load(self, brain, tmp_path):
        """Verify weights are preserved after save/load."""
        test_path = str(tmp_path / "persistence.json")
        with patch.object(brain, 'model_path', test_path):
            # 1. Modify weights from default
            brain.weights['W1'] += 1.0
            
            # 2. Save
            brain.save_model()
            assert os.path.exists(test_path)
            
            # 3. Create NEW brain and load
            NeuralBrain._instance = None
            new_brain = NeuralBrain(input_size=10, hidden_size=8)
            with patch.object(new_brain, 'model_path', test_path):
                new_brain.load_model()
                
                # Compare bit-by-bit
                np.testing.assert_array_almost_equal(new_brain.weights['W1'], brain.weights['W1'])
                assert new_brain.is_trained is True

from unittest.mock import patch
