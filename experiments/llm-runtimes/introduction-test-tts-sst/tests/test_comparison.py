"""Offline scoring checks for the real-model comparison runner."""

from comparison_prompts import SCENARIOS
from compare_interviews import evaluate


def test_ten_distinct_scenarios_cover_offered_and_declined_story() -> None:
    assert len(SCENARIOS) == 10
    assert len({scenario["name"] for scenario in SCENARIOS}) == 10
    assert sum(scenario["story"] is not None for scenario in SCENARIOS) == 4


def test_correct_facts_pass_and_missing_style_details_fail() -> None:
    scenario = SCENARIOS[0]
    config = {"username": "Ada", "style": scenario["style"], "ai_name": "Bob"}
    assert all(evaluate(config, scenario).values())
    config["style"] = "medieval"
    assert not evaluate(config, scenario)["style_details_preserved"]
    config["story"] = "No story"
    assert not evaluate(config, scenario)["story_correct"]


def test_offered_story_requires_its_details() -> None:
    scenario = SCENARIOS[2]
    config = {"username": "Mira", "style": scenario["style"], "ai_name": "Nimbus"}
    assert not evaluate(config, scenario)["story_correct"]
    config["story"] = scenario["story"]
    assert all(evaluate(config, scenario).values())