import pytest
from src.domain.services.strategy_service import StrategyService

def test_strategy_service_v2_veto_extreme():
    # Extreme BEAR (BMS < 0.15)
    score_long, score_short = StrategyService.apply_bms_weighting(
        score_long=9.0, score_short=2.0, bms_score=0.10, bms_zone='RED', w_btc=0.5, w_alt=0.5
    )
    # Long should be killed completely despite high score
    assert score_long == 0.0
    # Short should be boosted
    assert score_short > 2.0

    # Extreme BULL (BMS > 0.85)
    score_long, score_short = StrategyService.apply_bms_weighting(
        score_long=2.0, score_short=9.0, bms_score=0.90, bms_zone='GREEN', w_btc=0.5, w_alt=0.5
    )
    # Short should be killed
    assert score_short == 0.0
    # Long should be boosted
    assert score_long > 2.0

def test_strategy_service_v2_veto_stricter():
    # RED zone, score not high enough (needs > 7.0)
    score_long, score_short = StrategyService.apply_bms_weighting(
        score_long=6.0, score_short=2.0, bms_score=0.30, bms_zone='RED', w_btc=0.5, w_alt=0.5
    )
    assert score_long == 0.0

    # RED zone, score VERY high
    score_long, score_short = StrategyService.apply_bms_weighting(
        score_long=9.0, score_short=2.0, bms_score=0.30, bms_zone='RED', w_btc=0.5, w_alt=0.5
    )
    # Allowed but penalized
    assert score_long == 9.0 * 0.6
