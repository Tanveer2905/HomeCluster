"""Per-device roles, limits and the Balanced safety profile (coordinator/policy.py)."""

from __future__ import annotations

from typing import Any, Dict

import pytest

from coordinator import policy
from coordinator.policy import BALANCED, Safety


class FakeState:
    def __init__(self, settings: Dict[str, Any] = None) -> None:
        self.settings = settings or {}

    def setting(self, dotted: str, default: Any = None) -> Any:
        return self.settings.get(dotted, default)


def device(**fields: Any) -> Dict[str, Any]:
    base = {"id": "d1", "name": "phone", "status": "online", "removed": 0, "paused": 0,
            "slots": 8, "info": {"cpu_count": 8, "mem_mb": 4096}, "settings": {},
            "battery": 90, "charging": 1, "temp": 30.0, "mem_free_mb": 2000}
    base.update(fields)
    return base


def test_roles_default_on_and_master_switch() -> None:
    dev = device()
    assert policy.roles(dev) == {"jobs": True, "ai": True, "storage": True}
    dev["settings"] = {"roles": {"ai": False}}
    assert policy.role_on(dev, "jobs") and not policy.role_on(dev, "ai")
    dev["paused"] = 1
    assert not any(policy.role_on(dev, r) for r in policy.ROLES)


def test_limits_are_clamped_to_balanced() -> None:
    dev = device(settings={"cpu_pct": 500, "mem_pct": 95, "gpu_pct": 100})
    assert policy.limits(dev) == {"cpu_pct": 100, "mem_pct": 70, "gpu_pct": 90}
    dev["settings"] = {"cpu_pct": 1, "mem_pct": 1, "gpu_pct": -5}
    assert policy.limits(dev) == {"cpu_pct": 10, "mem_pct": 10, "gpu_pct": 0}
    assert policy.clean_settings({"roles": {"jobs": 0}, "cpu_pct": "50"}) == {"roles": {"jobs": False}, "cpu_pct": 50}
    with pytest.raises(ValueError):
        policy.clean_settings({"roles": {"mining": True}})
    with pytest.raises(ValueError):
        policy.clean_settings({"cpu_pct": "lots"})


def test_cores_leave_one_spare_and_follow_the_cpu_limit() -> None:
    dev = device()
    assert policy.core_allowance(dev) == 7          # 8 cores, one always spare
    dev["settings"] = {"cpu_pct": 50}
    assert policy.core_allowance(dev) == 4
    assert policy.job_slots(dev) == 4
    dev["slots"] = 2                                 # never more than the worker offers
    assert policy.job_slots(dev) == 2
    single = device(info={"cpu_count": 1})
    assert policy.core_allowance(single) == 1


def test_usable_memory_keeps_a_reserve_and_a_ceiling() -> None:
    dev = device(mem_free_mb=2000)                   # reserve 409 (10 % of 4 GB)
    assert policy.usable_mem_mb(dev) == min(2000 - 409, 1400)
    dev["settings"] = {"mem_pct": 30}
    assert policy.usable_mem_mb(dev) == 600
    dev["mem_free_mb"] = 100
    assert policy.usable_mem_mb(dev) == 0


def test_gpu_budget_follows_the_users_share() -> None:
    dev = device(info={"cpu_count": 12, "gpus": [{"name": "RTX", "vram_mb": 4096, "vk_index": 0}],
                       "llama_gpu": "win-vulkan-x64"},
                 gpu={"vram_total_mb": 4000, "vram_free_mb": 3500, "temp": 50})
    assert policy.gpu_budget_mb(dev) == int(min(3500, 4000 * 0.8) - BALANCED["gpu_reserve_mb"])
    dev["settings"] = {"gpu_pct": 50}
    assert policy.gpu_budget_mb(dev) == 2000 - 300
    dev["settings"] = {"gpu_pct": 0}
    assert policy.gpu_budget_mb(dev) == 0
    dev["info"].pop("llama_gpu")                     # no GPU build: no GPU
    dev["settings"] = {}
    assert policy.gpu_budget_mb(dev) == 0


def test_battery_temperature_states_with_hysteresis() -> None:
    safety = Safety(FakeState())
    dev = device(temp=39.0)
    assert safety.check(dev)["jobs"] and safety.check(dev)["ai_start"]
    dev["temp"] = 40.5
    status = safety.check(dev)
    assert status["temp_state"] == "warm" and not status["jobs"] and not status["ai_start"]
    assert status["ai_keep"] and not status["shed"]            # running models may stay
    dev["temp"] = 43.0
    status = safety.check(dev)
    assert status["temp_state"] == "hot" and status["shed"] and not status["ai_keep"]
    dev["temp"] = 38.0
    assert safety.check(dev)["temp_state"] == "warm"          # not cool enough yet
    dev["temp"] = 36.0
    assert safety.check(dev)["temp_state"] == "ok"


def test_models_need_charging_or_half_a_battery() -> None:
    safety = Safety(FakeState())
    dev = device(charging=0, battery=45)
    status = safety.check(dev)
    assert status["jobs"] and not status["ai_start"] and status["ai_keep"]
    assert any("50%" in n for n in status["notes"])
    dev["battery"] = 25                                         # unplugged and under 30 %: models leave
    assert not safety.check(dev)["ai_keep"]
    dev["charging"] = 1
    assert safety.check(dev)["ai_start"]


def test_cluster_settings_can_only_tighten() -> None:
    safety = Safety(FakeState({"scheduler.max_temp": 45.0, "scheduler.min_battery": 10}))
    limits = safety.thresholds(device())
    assert limits["pause_temp"] == 40.0 and limits["min_battery"] == 20
    safety = Safety(FakeState({"scheduler.max_temp": 38.0}))
    limits = safety.thresholds(device())
    assert limits["pause_temp"] == 38.0 and limits["resume_temp"] <= 37.0


def test_gpu_temperature_states() -> None:
    safety = Safety(FakeState())
    dev = device(info={"cpu_count": 12, "gpus": [{"name": "RTX", "vk_index": 0}], "llama_gpu": "win-vulkan-x64"},
                 gpu={"vram_total_mb": 4000, "vram_free_mb": 3500, "temp": 60})
    assert safety.check(dev)["gpu_start"]
    dev["gpu"]["temp"] = 81
    status = safety.check(dev)
    assert not status["gpu_start"] and status["gpu_keep"]
    dev["gpu"]["temp"] = 86
    assert not safety.check(dev)["gpu_keep"]
    dev["gpu"]["temp"] = 75
    assert not safety.check(dev)["gpu_start"]                  # still cooling
    dev["gpu"]["temp"] = 72
    assert safety.check(dev)["gpu_start"]
    dev["gpu"]["temp"] = None                                   # no reading: we cannot protect it
    status = safety.check(dev)
    assert not status["gpu_start"] and not status["gpu_keep"]


def test_offline_and_switched_off_devices_do_nothing() -> None:
    safety = Safety(FakeState())
    status = safety.check(device(status="offline"))
    assert not (status["jobs"] or status["ai_start"] or status["storage"])
    status = safety.check(device(paused=1))
    assert not (status["jobs"] or status["ai_start"] or status["ai_keep"] or status["storage"])
    assert "switched off by you" in status["notes"]
