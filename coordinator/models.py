"""Pydantic request models (SPEC §5, §8, §11)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(extra="allow")


# --------------------------------------------------------------------- auth
class LoginRequest(BaseModel):
    pin: str


# --------------------------------------------------------------------- jobs
class JobOptions(_Base):
    timeout: Optional[float] = None
    max_attempts: Optional[int] = None
    requires: Optional[List[str]] = None
    targets: Optional[Union[str, List[str]]] = None
    chunk_size: Optional[int] = None
    fail_fast: Optional[bool] = None
    #: reuse stored results: true / false / "refresh" (SPEC 9.8)
    cache: Optional[Union[bool, str]] = None
    #: second-device checks: false, or the share of tasks to check (SPEC 9.9)
    verify: Optional[Union[bool, float]] = None


class JobSpec(_Base):
    name: Optional[str] = None
    type: str
    params: Dict[str, Any] = Field(default_factory=dict)
    priority: int = 0
    depends_on: List[str] = Field(default_factory=list)
    options: Dict[str, Any] = Field(default_factory=dict)


class PipelineJobSpec(JobSpec):
    key: Optional[str] = None


class PipelineRequest(_Base):
    name: Optional[str] = None
    jobs: List[Dict[str, Any]]


# ------------------------------------------------------------------ devices
class DevicePatch(_Base):
    name: Optional[str] = None
    paused: Optional[bool] = None
    slots: Optional[int] = None
    settings: Optional[Dict[str, Any]] = None


class NotifyRequest(_Base):
    title: str = "PhoneCluster"
    body: str = ""


# ----------------------------------------------------------------- terminal
class TerminalOpen(_Base):
    device_id: str


# ---------------------------------------------------------------- schedules
class ScheduleCreate(_Base):
    name: str
    cron: str
    job_spec: Dict[str, Any]
    enabled: bool = True


class SchedulePatch(_Base):
    name: Optional[str] = None
    cron: Optional[str] = None
    job_spec: Optional[Dict[str, Any]] = None
    enabled: Optional[bool] = None


# ---------------------------------------------------------------- templates
class TemplateCreate(_Base):
    name: str
    job_spec: Dict[str, Any]


# ----------------------------------------------------------------- settings
class SettingsPatch(_Base):
    scheduler: Optional[Dict[str, Any]] = None
    storage: Optional[Dict[str, Any]] = None
    notifications: Optional[Dict[str, Any]] = None
    llm: Optional[Dict[str, Any]] = None
    services: Optional[Dict[str, Any]] = None
    cache: Optional[Dict[str, Any]] = None
    verify: Optional[Dict[str, Any]] = None
    transfers: Optional[Dict[str, Any]] = None
    pin: Optional[str] = None


class RerunRequest(_Base):
    #: ignore stored results and compute everything again
    fresh: bool = False


class TrustPatch(_Base):
    #: forgive past wrong answers and let the device take jobs again
    reset: bool = True


# ------------------------------------------------------------------ storage
class StoragePutRequest(_Base):
    file_id: Optional[str] = None
    name: Optional[str] = None
    replication: Optional[int] = None
    scheme: Optional[str] = None


# ------------------------------------------------------------- worker (§8)
class RegisterRequest(_Base):
    #: a one-time code from the PC's Join page (SPEC 3); the PIN only works on the PC itself
    join_code: Optional[str] = None
    pin: Optional[str] = None
    name: str = "device"
    platform: str = "termux"
    caps: List[str] = Field(default_factory=list)
    info: Dict[str, Any] = Field(default_factory=dict)
    slots: int = 1
    device_id: Optional[str] = None
    token: Optional[str] = None


class WorkerStatus(_Base):
    battery: Optional[int] = None
    charging: Optional[bool] = None
    temp: Optional[float] = None
    free_slots: Optional[int] = None
    running: List[str] = Field(default_factory=list)
    storage_free_mb: Optional[int] = None
    mem_free_mb: Optional[int] = None
    #: discrete GPU: {name, vram_total_mb, vram_free_mb, temp, util}
    gpu: Optional[Dict[str, Any]] = None


class PollRequest(_Base):
    status: WorkerStatus = Field(default_factory=WorkerStatus)


class TaskLogRequest(_Base):
    lines: List[str] = Field(default_factory=list)
    ts: Optional[float] = None


class TaskCheckpointRequest(_Base):
    state: Any = None
    attempt: Optional[int] = None


# ----------------------------------------------------------------- services
class ServiceReadyRequest(_Base):
    name: str
    meta: Dict[str, Any] = Field(default_factory=dict)


class ServicePartialRequest(_Base):
    data: Any = None


class ServiceDoneRequest(_Base):
    ok: bool = True
    result: Any = None
    error: Optional[str] = None
    traceback: Optional[str] = None
    outputs: List[Dict[str, Any]] = Field(default_factory=list)
    ms: Optional[int] = None


class TaskDoneRequest(_Base):
    ok: bool = True
    result: Any = None
    outputs: List[Dict[str, Any]] = Field(default_factory=list)
    error: Optional[str] = None
    traceback: Optional[str] = None
    duration: Optional[float] = None
    #: time inside the task's own code (no interpreter start-up, no pauses)
    work_duration: Optional[float] = None
    log_lines: Optional[int] = None
    attempt: Optional[int] = None
    #: which impl kind ran (python / js / shell / wasm): checks compare like with like
    impl: Optional[str] = None


class ChunkAckRequest(_Base):
    chunk_id: str
    ok: bool = True
    error: Optional[str] = None


class ScrubReport(_Base):
    """A device re-hashed stored pieces (SPEC §10): which ones were bad or gone."""
    checked: List[str] = Field(default_factory=list)
    bad: List[str] = Field(default_factory=list)
    missing: List[str] = Field(default_factory=list)


class PeerFetchRequest(_Base):
    """A worker wants a whole input file and asks where to get it (SPEC §8.3)."""
    file_id: str
    size: Optional[int] = None


class PeerReadyRequest(_Base):
    """A device opened its temporary sending port (or could not)."""
    offer_id: str
    port: Optional[int] = None
    host: Optional[str] = None
    ok: bool = True
    error: Optional[str] = None


class PeerResultRequest(_Base):
    """How a peer download went: wrong bytes count against the sender."""
    offer_id: str
    ok: bool = True
    bytes: Optional[int] = None
    seconds: Optional[float] = None
    error: Optional[str] = None


class BenchRequest(_Base):
    score: float = 0.0
    loop_iters: Optional[int] = None
    hash_mb_s: Optional[float] = None
    kind: Optional[str] = None


class TermOutputRequest(_Base):
    data: str = ""


class TermClosedRequest(_Base):
    exit_code: Optional[int] = None
