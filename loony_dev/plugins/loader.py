from __future__ import annotations

import logging
import os
from importlib import import_module
from importlib.metadata import entry_points
from pathlib import Path
from typing import TYPE_CHECKING

from loony_dev.plugins.base import PluginConflictError

if TYPE_CHECKING:
    from loony_dev.agents.base import Agent
    from loony_dev.config import Settings
    from loony_dev.tasks.base import Task

logger = logging.getLogger(__name__)

TASK_GROUP = "loony_dev.task_plugins"
AGENT_GROUP = "loony_dev.agent_plugins"

# Environment variable for local development overrides (comma-separated
# "module:ClassName" paths loaded in addition to entry-point plugins).
_EXTRA_PLUGINS_ENV = "LOONY_DEV_EXTRA_PLUGINS"


def _load_extra_task_plugins() -> list[object]:
    """Load additional task plugins from LOONY_DEV_EXTRA_PLUGINS env var."""
    raw = os.environ.get(_EXTRA_PLUGINS_ENV, "").strip()
    if not raw:
        return []
    plugins = []
    for spec in raw.split(","):
        spec = spec.strip()
        if not spec:
            continue
        try:
            module_path, class_name = spec.rsplit(":", 1)
            module = import_module(module_path)
            cls = getattr(module, class_name)
            plugins.append(cls())
            logger.info("Loaded extra task plugin from env: %s", spec)
        except Exception:
            logger.exception("Failed to load extra task plugin from env: %s", spec)
    return plugins


def _load_extra_agent_plugins() -> list[object]:
    """Load additional agent plugins from LOONY_DEV_EXTRA_PLUGINS env var."""
    raw = os.environ.get(_EXTRA_PLUGINS_ENV, "").strip()
    if not raw:
        return []
    plugins = []
    for spec in raw.split(","):
        spec = spec.strip()
        if not spec:
            continue
        try:
            module_path, class_name = spec.rsplit(":", 1)
            module = import_module(module_path)
            cls = getattr(module, class_name)
            plugins.append(cls())
            logger.info("Loaded extra agent plugin from env: %s", spec)
        except Exception:
            logger.exception("Failed to load extra agent plugin from env: %s", spec)
    return plugins


def load_task_plugins() -> list[type[Task]]:
    """Discover and load all task plugins via entry points.

    Returns task classes sorted by priority (ascending — lower number = higher
    priority). Raises :exc:`PluginConflictError` on name or task-type conflicts.
    Logs and skips individual plugins that raise unexpected errors on load.
    """
    seen_names: dict[str, str] = {}        # plugin name → ep.name
    seen_task_types: dict[str, str] = {}   # task_type → plugin name
    task_classes: list[type[Task]] = []

    eps = list(entry_points(group=TASK_GROUP))
    logger.debug("Found %d task plugin entry point(s) in group '%s'", len(eps), TASK_GROUP)

    for ep in eps:
        try:
            plugin_cls = ep.load()
            plugin = plugin_cls()

            if plugin.name in seen_names:
                raise PluginConflictError(
                    f"Task plugin name '{plugin.name}' is claimed by both "
                    f"'{seen_names[plugin.name]}' and '{ep.name}'"
                )
            seen_names[plugin.name] = ep.name

            for cls in plugin.task_classes:
                task_type = cls.task_type
                if task_type in seen_task_types:
                    raise PluginConflictError(
                        f"Task type '{task_type}' is registered by both "
                        f"'{seen_task_types[task_type]}' and '{plugin.name}'"
                    )
                seen_task_types[task_type] = plugin.name
                task_classes.append(cls)

            logger.info("Loaded task plugin: %s (provides %d task type(s))", ep.name, len(plugin.task_classes))
        except PluginConflictError:
            raise
        except Exception:
            logger.exception("Failed to load task plugin: %s — skipping", ep.name)

    return sorted(task_classes, key=lambda tc: tc.priority)


def load_agent_plugins(work_dir: Path, settings: Settings) -> list[Agent]:
    """Discover and load all agent plugins via entry points.

    Returns a flat list of instantiated :class:`~loony_dev.agents.base.Agent`
    objects ready for use by the orchestrator. Raises :exc:`PluginConflictError`
    on plugin name conflicts. Logs and skips individual plugins that raise
    unexpected errors on load.
    """
    seen_names: dict[str, str] = {}  # plugin name → ep.name
    agents: list[Agent] = []

    eps = list(entry_points(group=AGENT_GROUP))
    logger.debug("Found %d agent plugin entry point(s) in group '%s'", len(eps), AGENT_GROUP)

    for ep in eps:
        try:
            plugin_cls = ep.load()
            plugin = plugin_cls()

            if plugin.name in seen_names:
                raise PluginConflictError(
                    f"Agent plugin name '{plugin.name}' is claimed by both "
                    f"'{seen_names[plugin.name]}' and '{ep.name}'"
                )
            seen_names[plugin.name] = ep.name

            new_agents = plugin.create_agents(work_dir, settings)
            agents.extend(new_agents)
            logger.info("Loaded agent plugin: %s (provides %d agent(s))", ep.name, len(new_agents))
        except PluginConflictError:
            raise
        except Exception:
            logger.exception("Failed to load agent plugin: %s — skipping", ep.name)

    return agents
