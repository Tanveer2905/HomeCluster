"""PhoneCluster — Python SDK and CLI for the LAN phone cluster.

    from phonecluster import Cluster
    c = Cluster("http://192.168.1.10:8765", pin="482913")
    job = c.map(square, range(1, 201), chunk_size=20).wait()
    print(job.result())

``url`` and ``pin`` fall back to the ``PC_URL`` / ``PC_PIN`` environment
variables and then to ``data/cli.json`` (written by ``pc login``).
Stdlib only — no dependencies, Python 3.8+.
"""

from .client import (
    Cluster,
    ClusterError,
    Files,
    Job,
    JobFailed,
    PhoneClusterError,
    Pipeline,
    Schedules,
    Storage,
    Templates,
    WebSocket,
    config_path,
    load_config,
    project_root,
    save_config,
    ws_decode_frame,
    ws_encode_frame,
)

__version__ = "0.1.0"

__all__ = [
    "Cluster",
    "Job",
    "Pipeline",
    "Files",
    "Storage",
    "Schedules",
    "Templates",
    "PhoneClusterError",
    "ClusterError",
    "JobFailed",
    "WebSocket",
    "ws_encode_frame",
    "ws_decode_frame",
    "project_root",
    "config_path",
    "load_config",
    "save_config",
    "__version__",
]
