"""Tests for the LiteLLM local cost-map pin."""

import os

from preloop.services.litellm_cost_map import (
    _LOCAL_COST_MAP_ENV,
    pin_local_litellm_cost_map,
)


def test_pin_defaults_to_local_map(monkeypatch):
    monkeypatch.delenv(_LOCAL_COST_MAP_ENV, raising=False)
    pin_local_litellm_cost_map()
    assert os.environ[_LOCAL_COST_MAP_ENV] == "true"


def test_pin_does_not_override_operator_choice(monkeypatch):
    monkeypatch.setenv(_LOCAL_COST_MAP_ENV, "false")
    pin_local_litellm_cost_map()
    assert os.environ[_LOCAL_COST_MAP_ENV] == "false"
