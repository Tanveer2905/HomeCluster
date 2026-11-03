"""Who may do what: per-device roles, user limits and the safety profile (SPEC 9.3).

Every device stays connected, but the user decides what it is used for and
how hard it may work:

* **roles**   ``jobs`` (normal tasks), ``ai`` (models and services) and
  ``storage`` (pooled storage chunks).  ``paused`` is the master switch: a
  paused device is used for nothing.
* **limits**  ``cpu_pct`` (share of the cores for jobs and model threads),
  ``mem_pct`` (share of the free memory a model may take) and ``gpu_pct``
  (share of the graphics memory a model may take; 0 keeps the GPU out).

Limits are capped by the **Balanced** safety profile, which also decides when
a device must rest.  Thermal states use hysteresis so a device that hit a
limit stays out until it has really cooled down:

* battery 40 C: no new work; 43 C: running work and models leave the device;
  back to normal at 36 C;
* models start only on a charging device or one above 50 % battery, and leave
  an unplugged device that drops under 30 %; normal tasks stop under 20 %;
* at most 70 % of the free memory, one core always left idle;
* GPU 80 C: no new models on it; 85 C: models move off it; back at 72 C.

Per-device overrides (``min_battery``, ``max_temp``, ``resume_temp``) and the
cluster-wide ``scheduler.*`` settings can only make these stricter.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

#: the profile the user chose (docs/DEVIATIONS.md 25)
BALANCED: Dict[str, float] = {
    "pause_temp": 40.0,
    "resume_temp": 36.0,
    "unload_temp": 43.0,
    "min_battery": 20,
    "ai_min_battery": 50,
    "ai_stop_battery": 30,
    "mem_pct_max": 70,
    "spare_cores": 1,
    "gpu_pct_max": 90,
    "gpu_pause_temp": 80.0,
    "gpu_resume_temp": 72.0,
    "gpu_unload_temp": 85.0,
    "gpu_reserve_mb": 300,
}

ROLES = ("jobs", "ai", "storage")
DEFAULT_LIMITS: Dict[str, int] = {"cpu_pct": 100, "mem_pct": 70, "gpu_pct": 80}
LIMIT_RANGE: Dict[str, tuple] = {
    "cpu_pct": (10, 100),
    "mem_pct": (10, int(BALANCED["mem_pct_max"])),
    "gpu_pct": (0, int(BALANCED["gpu_pct_max"])),
}
SETTING_KEYS = ("roles", "cpu_pct", "mem_pct", "gpu_pct", "min_battery", "max_temp", "resume_temp")


def _num(value: Any) -> Optional[float]:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def settings_of(device: Dict[str, Any]) -> Dict[str, Any]:
    settings = device.get("settings")
    return settings if isinstance(settings, dict) else {}


def roles(device: Dict[str, Any]) -> Dict[str, bool]:
    """``{jobs, ai, storage}``; anything not switched off is on."""
    raw = settings_of(device).get("roles")
    raw = raw if isinstance(raw, dict) else {}
    return {role: raw.get(role) is not False for role in ROLES}


def role_on(device: Dict[str, Any], role: str) -> bool:
    """The master switch (``paused``) and the role switch together."""
    if device.get("paused"):
        return False
    return roles(device).get(role, True)


def clamp_limit(name: str, value: Any) -> int:
    low, high = LIMIT_RANGE[name]
    number = _num(value)
    if number is None:
        number = DEFAULT_LIMITS[name]
    return int(max(low, min(high, round(number))))


def limits(device: Dict[str, Any]) -> Dict[str, int]:
    settings = settings_of(device)
    return {name: clamp_limit(name, settings.get(name)) for name in DEFAULT_LIMITS}


def clean_settings(patch: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a ``PATCH /api/devices/{id}`` settings block."""
    out: Dict[str, Any] = {}
    for key, value in (patch or {}).items():
        if key == "roles":
            if not isinstance(value, dict):
                raise ValueError("roles must be an object like {\"jobs\": true, \"ai\": false}")
            unknown = set(value) - set(ROLES)
            if unknown:
                raise ValueError("unknown role(s): %s" % ", ".join(sorted(unknown)))
            out["roles"] = {k: bool(v) for k, v in value.items()}
        elif key in LIMIT_RANGE:
            if _num(value) is None:
                raise ValueError("%s must be a number" % key)
            out[key] = clamp_limit(key, value)
        else:
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# capacity
# ---------------------------------------------------------------------------

def cores(device: Dict[str, Any]) -> int:
    info = device.get("info") or {}
    try:
        return max(1, int(info.get("cpu_count") or 1))
    except (TypeError, ValueError):
        return 1


def core_allowance(device: Dict[str, Any]) -> int:
    """Cores the cluster may keep busy: the CPU limit, always one spare."""
    n = cores(device)
    allowed = int(round(n * limits(device)["cpu_pct"] / 100.0))
    if n > 1:
        allowed = min(allowed, n - int(BALANCED["spare_cores"]))
    return max(1, allowed)


def job_slots(device: Dict[str, Any]) -> int:
    """Concurrent normal tasks: the core allowance, never more than the worker offers."""
    offered = int(device.get("slots") or 1)
    return max(1, min(offered, core_allowance(device)))


PC_PLATFORMS = ("windows", "linux")


def slots_beside_a_model(device: Dict[str, Any]) -> int:
    """Normal tasks next to a model or service on the same device: a phone
    runs one (the model needs its memory and cores), a PC keeps half."""
    if device.get("platform") in PC_PLATFORMS:
        return max(1, job_slots(device) // 2)
    return 1


def model_threads(device: Dict[str, Any]) -> int:
    return core_allowance(device)


def reserve_mb(device: Dict[str, Any]) -> int:
    """Memory always left to the OS and other apps: 10 % of RAM, at least 256 MB."""
    total = (device.get("info") or {}).get("mem_mb") or 0
    return int(max(256, float(total) * 0.10))


def usable_mem_mb(device: Dict[str, Any]) -> int:
    """Memory a model may use: free minus the reserve, and at most ``mem_pct`` of free."""
    free = device.get("mem_free_mb")
    if free is None:
        total = (device.get("info") or {}).get("mem_mb") or 0
        free = int(float(total) * 0.5)
    free = max(0, int(free))
    cap = free * limits(device)["mem_pct"] / 100.0
    return int(max(0, min(free - reserve_mb(device), cap)))


def gpu_info(device: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The device's discrete GPU (live figures merged over the join-time facts)."""
    info = device.get("info") or {}
    base = None
    for gpu in info.get("gpus") or []:
        if isinstance(gpu, dict):
            base = dict(gpu)
            break
    live = device.get("gpu") if isinstance(device.get("gpu"), dict) else None
    if base is None and live is None:
        return None
    merged = dict(base or {})
    merged.update(live or {})
    return merged


def gpu_runtime(device: Dict[str, Any]) -> Optional[str]:
    """Which llama.cpp GPU build the device has locally (``win-vulkan-x64``), if any."""
    value = (device.get("info") or {}).get("llama_gpu")
    return str(value) if value else None


def gpu_budget_mb(device: Dict[str, Any]) -> int:
    """Graphics memory a model may use: ``gpu_pct`` of the card, within what is free."""
    gpu = gpu_info(device)
    if not gpu or not gpu_runtime(device):
        return 0
    pct = limits(device)["gpu_pct"]
    if pct <= 0:
        return 0
    total = _num(gpu.get("vram_total_mb")) or 0
    free = _num(gpu.get("vram_free_mb"))
    if free is None:
        free = total
    budget = min(free, total * pct / 100.0) - BALANCED["gpu_reserve_mb"]
    return int(max(0, budget))


# ---------------------------------------------------------------------------
# safety (stateful: hysteresis)
# ---------------------------------------------------------------------------

class Safety:
    """Thermal / battery state per device, with hysteresis."""

    def __init__(self, state: Any) -> None:
        self.state = state
        self.warm: Set[str] = set()      # battery/SoC temperature
        self.gpu_warm: Set[str] = set()  # graphics card temperature

    # -- thresholds --------------------------------------------------------
    def _setting(self, key: str, default: Any) -> Any:
        return self.state.setting("scheduler.%s" % key, default)

    def thresholds(self, device: Dict[str, Any]) -> Dict[str, float]:
        """Balanced, made stricter by cluster or per-device settings."""
        settings = settings_of(device)
        pause = BALANCED["pause_temp"]
        resume = BALANCED["resume_temp"]
        min_battery = BALANCED["min_battery"]
        for value in (self._setting("max_temp", None), settings.get("max_temp")):
            if _num(value) is not None:
                pause = min(pause, _num(value))
        for value in (self._setting("resume_temp", None), settings.get("resume_temp")):
            if _num(value) is not None:
                resume = min(resume, _num(value))
        for value in (self._setting("min_battery", None), settings.get("min_battery")):
            if _num(value) is not None:
                min_battery = max(min_battery, _num(value))
        resume = min(resume, pause - 1.0)
        return {
            "pause_temp": pause,
            "resume_temp": resume,
            "unload_temp": max(pause, BALANCED["unload_temp"]),
            "min_battery": min_battery,
            "ai_min_battery": max(min_battery, BALANCED["ai_min_battery"]),
            "ai_stop_battery": max(min_battery, BALANCED["ai_stop_battery"]),
        }

    # -- temperature states --------------------------------------------------
    def temp_state(self, device: Dict[str, Any]) -> str:
        """``ok`` / ``warm`` (no new work) / ``hot`` (work must leave)."""
        limits_ = self.thresholds(device)
        device_id = device["id"]
        temp = _num(device.get("temp"))
        if temp is None:
            self.warm.discard(device_id)
            return "ok"
        if temp >= limits_["unload_temp"]:
            self.warm.add(device_id)
            return "hot"
        if device_id in self.warm:
            if temp <= limits_["resume_temp"]:
                self.warm.discard(device_id)
                return "ok"
            return "warm"
        if temp >= limits_["pause_temp"]:
            self.warm.add(device_id)
            return "warm"
        return "ok"

    def gpu_state(self, device: Dict[str, Any]) -> str:
        gpu = gpu_info(device)
        device_id = device["id"]
        temp = _num((gpu or {}).get("temp"))
        if temp is None:
            self.gpu_warm.discard(device_id)
            # no temperature reading, no GPU use: we could not protect it
            return "unknown" if gpu else "none"
        if temp >= BALANCED["gpu_unload_temp"]:
            self.gpu_warm.add(device_id)
            return "hot"
        if device_id in self.gpu_warm:
            if temp <= BALANCED["gpu_resume_temp"]:
                self.gpu_warm.discard(device_id)
                return "ok"
            return "warm"
        if temp >= BALANCED["gpu_pause_temp"]:
            self.gpu_warm.add(device_id)
            return "warm"
        return "ok"

    def forget(self, device_id: str) -> None:
        self.warm.discard(device_id)
        self.gpu_warm.discard(device_id)

    # -- what the device may do right now ------------------------------------
    def _battery_low(self, device: Dict[str, Any], floor: float) -> bool:
        battery = _num(device.get("battery"))
        return bool(not device.get("charging") and battery is not None and battery < floor)

    def check(self, device: Dict[str, Any]) -> Dict[str, Any]:
        """Everything the scheduler, storage and the model planner ask about a device.

        ``jobs``      may start normal tasks
        ``ai_start``  may start models / services
        ``ai_keep``   may keep running models (False: move them off now)
        ``shed``      running tasks must leave (too hot)
        ``gpu_start`` / ``gpu_keep``  the same for the graphics card
        ``notes``     human readable reasons for anything that is off
        """
        limits_ = self.thresholds(device)
        temp_state = self.temp_state(device)
        gpu_state = self.gpu_state(device)
        notes: List[str] = []
        temp = _num(device.get("temp"))
        if device.get("paused"):
            notes.append("switched off by you")
        role_map = roles(device)
        for role in ROLES:
            if not role_map[role] and not device.get("paused"):
                notes.append("%s off" % {"jobs": "jobs", "ai": "AI models", "storage": "storage"}[role])
        if temp_state == "hot":
            notes.append("too hot (%.1f C): work moved off until %.0f C"
                         % (temp or 0, limits_["resume_temp"]))
        elif temp_state == "warm":
            notes.append("warm (%.1f C): no new work until %.0f C" % (temp or 0, limits_["resume_temp"]))
        battery_low = self._battery_low(device, limits_["min_battery"])
        if battery_low:
            notes.append("battery under %d%% and not charging" % limits_["min_battery"])
        ai_battery = not self._battery_low(device, limits_["ai_min_battery"])
        ai_battery_keep = not self._battery_low(device, limits_["ai_stop_battery"])
        if not ai_battery and not battery_low:
            notes.append("models need charging or %d%% battery" % limits_["ai_min_battery"])
        gpu = gpu_info(device)
        gpu_ok = bool(gpu and gpu_runtime(device) and limits(device)["gpu_pct"] > 0)
        if gpu and gpu_runtime(device):
            if gpu_state == "unknown":
                notes.append("GPU temperature unreadable: GPU not used")
            elif gpu_state == "hot":
                notes.append("GPU too hot (%.0f C): models moved off it" % _num(gpu.get("temp")))
            elif gpu_state == "warm":
                notes.append("GPU warm (%.0f C): no new models on it until %.0f C"
                             % (_num(gpu.get("temp")), BALANCED["gpu_resume_temp"]))
        online = device.get("status") == "online" and not device.get("removed")
        ok_temp = temp_state == "ok"
        return {
            "online": online,
            "temp_state": temp_state,
            "gpu_state": gpu_state,
            "jobs": online and role_on(device, "jobs") and ok_temp and not battery_low,
            "storage": online and role_on(device, "storage"),
            "ai_start": online and role_on(device, "ai") and ok_temp and ai_battery,
            "ai_keep": online and role_on(device, "ai") and temp_state != "hot" and ai_battery_keep,
            "battery_ok": not battery_low,
            "shed": temp_state == "hot",
            "gpu_start": gpu_ok and gpu_state == "ok",
            "gpu_keep": gpu_ok and gpu_state in ("ok", "warm"),
            "notes": notes,
        }

    def view(self, device: Dict[str, Any]) -> Dict[str, Any]:
        """Roles, limits and safety for the dashboard / API."""
        status = self.check(device)
        return {
            "roles": roles(device),
            "limits": limits(device),
            "job_slots": job_slots(device),
            "threads": model_threads(device),
            "usable_mem_mb": usable_mem_mb(device),
            "gpu": gpu_info(device),
            "gpu_budget_mb": gpu_budget_mb(device) if status["gpu_start"] or status["gpu_keep"] else 0,
            "safety": {k: status[k] for k in ("temp_state", "gpu_state", "jobs", "ai_start",
                                              "ai_keep", "storage", "gpu_start", "notes")},
        }
