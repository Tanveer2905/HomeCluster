"""Plugin loader and registry (SPEC §7).

Every module in this package that defines ``TYPE`` becomes a job type.  User
plugins dropped into ``<data>/plugins/*.py`` are loaded on top of the built-ins
(a user module may replace a built-in by reusing its ``TYPE``).
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import logging
import pkgutil
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Dict, List, Optional, Sequence

log = logging.getLogger("coordinator.plugins")

#: Built-in plugin module names, imported in this order.
BUILTINS: Sequence[str] = (
    "python_map",
    "shell",
    "media",
    "hash_search",
    "monte_carlo_pi",
    "prime_range",
    "sensor",
    "benchmark",
    "custom",
    "service",
    "llm_shard",
    "llm_head",
    "wasm",
)


def _source_hash(module: ModuleType) -> str:
    path = getattr(module, "__file__", None)
    try:
        data = Path(str(path)).read_bytes() if path else b""
    except OSError:
        data = b""
    return hashlib.sha1(data or str(getattr(module, "TYPE", "")).encode("utf-8")).hexdigest()[:12]


class Plugin:
    """A loaded plugin module with defaults filled in."""

    def __init__(self, module: ModuleType) -> None:
        self.module = module
        self.type: str = str(getattr(module, "TYPE"))
        self.description: str = str(getattr(module, "DESCRIPTION", "") or "")
        self.params: List[Dict[str, Any]] = list(getattr(module, "PARAMS", []) or [])
        self.default_options: Dict[str, Any] = dict(
            getattr(module, "DEFAULT_OPTIONS", {}) or {}
        )
        #: internal types (driven by a manager, not the New job form)
        self.hidden: bool = bool(getattr(module, "HIDDEN", False))
        #: same inputs -> same result, so results may be reused (SPEC 9.8)
        self.cacheable: bool = bool(getattr(module, "CACHE", False))
        #: results are exact, so a second device can check them (SPEC 9.9)
        self.verifiable: bool = bool(getattr(module, "VERIFY", False))
        #: changes whenever the plugin's code changes: stored job results of an
        #: older version are not reused
        self.version: str = _source_hash(module)
        split = getattr(module, "split", None)
        if not callable(split):
            raise TypeError("plugin %r has no split()" % self.type)
        self._split: Callable[..., Any] = split
        self._reduce: Optional[Callable[..., Any]] = getattr(module, "reduce", None)
        # automatic task sizing (SPEC §9.6): work() says how much there is,
        # piece() builds a task for units [start, end)
        self._work: Optional[Callable[..., Any]] = getattr(module, "work", None)
        self._piece: Optional[Callable[..., Any]] = getattr(module, "piece", None)
        self._speed_key: Optional[Callable[..., Any]] = getattr(module, "speed_key", None)

    @property
    def dynamic(self) -> bool:
        return callable(self._work) and callable(self._piece)

    def work(self, params: Dict[str, Any], options: Dict[str, Any], ctx: Any) -> Optional[Dict[str, Any]]:
        """``{"units", "per_unit"?, "grain"?, "state"?}`` or None (fixed split)."""
        if not self.dynamic:
            return None
        info = self._work(self.normalize_params(params), options, ctx)  # type: ignore[misc]
        return info if isinstance(info, dict) else None

    def piece(self, params: Dict[str, Any], options: Dict[str, Any], ctx: Any, state: Any,
              start: int, end: int) -> Dict[str, Any]:
        payload = self._piece(self.normalize_params(params), options, ctx, state, start, end)  # type: ignore[misc]
        if not isinstance(payload, dict):
            raise TypeError("piece() must return a dict")
        return payload

    def speed_key(self, params: Dict[str, Any]) -> str:
        """Name of the kind of work, for the speed memory."""
        if callable(self._speed_key):
            try:
                key = self._speed_key(params)
                if key:
                    return str(key)
            except Exception:
                log.debug("speed_key failed for %s", self.type, exc_info=True)
        return self.type

    # ------------------------------------------------------------------ api
    def normalize_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Coerce list-typed params (``files``) that arrived as a bare string.

        A CLI ``--file inputs=path`` or a hand-written jobspec easily sends a
        single file id where the plugin expects a list; iterating a 64-char
        sha256 string would otherwise fan out into 64 bogus tasks.
        """
        out = dict(params or {})
        for spec in self.params:
            name = spec.get("name")
            if not name or spec.get("type") != "files":
                continue
            value = out.get(str(name))
            if value is None or value == "":
                continue
            if isinstance(value, str):
                out[str(name)] = [value]
            elif isinstance(value, (list, tuple)):
                out[str(name)] = [str(v) for v in value if v not in (None, "")]
        return out

    def split(self, params: Dict[str, Any], options: Dict[str, Any], ctx: Any) -> List[Dict[str, Any]]:
        payloads = self._split(self.normalize_params(params), options, ctx)
        if payloads is None:
            return []
        if isinstance(payloads, dict):
            payloads = [payloads]
        out: List[Dict[str, Any]] = []
        for item in payloads:
            if not isinstance(item, dict):
                raise TypeError("split() must return dicts, got %r" % type(item))
            out.append(item)
        return out

    def reduce(self, params: Dict[str, Any], results: List[Dict[str, Any]], ctx: Any) -> Any:
        if self._reduce is None:
            return [r.get("result") for r in results]
        return self._reduce(params, results, ctx)

    def describe(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "description": self.description,
            "params": self.params,
            "default_options": self.default_options,
            "hidden": self.hidden,
            "cacheable": self.cacheable,
            "verifiable": self.verifiable,
        }

    def defaults(self) -> Dict[str, Any]:
        """Default values for every declared param."""
        out: Dict[str, Any] = {}
        for spec in self.params:
            name = spec.get("name")
            if name and "default" in spec:
                out[str(name)] = spec["default"]
        return out


class Registry:
    """Maps ``TYPE`` -> :class:`Plugin`."""

    def __init__(self) -> None:
        self._plugins: Dict[str, Plugin] = {}

    # --------------------------------------------------------------- loading
    def add_module(self, module: ModuleType) -> Optional[Plugin]:
        if not hasattr(module, "TYPE"):
            return None
        try:
            plugin = Plugin(module)
        except Exception:
            log.exception("plugin %s is invalid", getattr(module, "__name__", module))
            return None
        self._plugins[plugin.type] = plugin
        return plugin

    def load_builtins(self) -> None:
        package = __name__
        names = list(BUILTINS)
        for info in pkgutil.iter_modules(__path__):  # type: ignore[name-defined]
            if info.name not in names and not info.name.startswith("_"):
                names.append(info.name)
        for name in names:
            try:
                module = importlib.import_module("%s.%s" % (package, name))
            except Exception:
                log.exception("could not import built-in plugin %s", name)
                continue
            self.add_module(module)

    def load_dir(self, directory: Path) -> None:
        """Import every ``*.py`` in ``directory`` as a user plugin."""
        directory = Path(directory)
        if not directory.is_dir():
            return
        for path in sorted(directory.glob("*.py")):
            if path.name.startswith("_"):
                continue
            mod_name = "coordinator_user_plugins_%s" % path.stem
            try:
                spec = importlib.util.spec_from_file_location(mod_name, path)
                if spec is None or spec.loader is None:
                    continue
                module = importlib.util.module_from_spec(spec)
                sys.modules[mod_name] = module
                spec.loader.exec_module(module)
            except Exception:
                log.exception("could not load user plugin %s", path)
                continue
            plugin = self.add_module(module)
            if plugin is not None:
                log.info("loaded user plugin %s from %s", plugin.type, path.name)

    # ---------------------------------------------------------------- access
    def get(self, type_name: str) -> Optional[Plugin]:
        return self._plugins.get(str(type_name))

    def require(self, type_name: str) -> Plugin:
        plugin = self.get(type_name)
        if plugin is None:
            raise KeyError("unknown job type %r" % (type_name,))
        return plugin

    def types(self) -> List[str]:
        return sorted(self._plugins)

    def describe_all(self) -> List[Dict[str, Any]]:
        return [self._plugins[t].describe() for t in self.types()]

    def __contains__(self, type_name: object) -> bool:
        return str(type_name) in self._plugins

    def __len__(self) -> int:
        return len(self._plugins)


def build_registry(user_dir: Optional[Path] = None) -> Registry:
    """Load the built-ins plus optional user plugins."""
    registry = Registry()
    registry.load_builtins()
    if user_dir is not None:
        registry.load_dir(user_dir)
    return registry
