"""AMOF command implementations.

Keep package import side effects minimal so public CLI startup does not
automatically import every command module.
"""

from __future__ import annotations

from importlib import import_module

_COMMAND_EXPORTS: dict[str, tuple[str, str]] = {
    "cmd_sync": ("sync", "cmd_sync"),
    "cmd_status": ("status", "cmd_status"),
    "cmd_context": ("context", "cmd_context"),
    "cmd_operational_context": ("operational_context", "cmd_operational_context"),
    "cmd_install": ("install", "cmd_install"),
    "cmd_paths": ("paths", "cmd_paths"),
    "cmd_workspace": ("workspace", "cmd_workspace"),
    "cmd_workspace_list": ("workspace", "cmd_workspace_list"),
    "cmd_workspace_registry_list": ("workspace", "cmd_workspace_registry_list"),
    "cmd_workspace_register": ("workspace", "cmd_workspace_register"),
    "cmd_workspace_show": ("workspace", "cmd_workspace_show"),
    "cmd_workspace_materialize_run": ("workspace", "cmd_workspace_materialize_run"),
    "cmd_workspace_materialize_from_intake": ("workspace", "cmd_workspace_materialize_from_intake"),
    "cmd_open": ("workspace", "cmd_open"),
    "cmd_push": ("workspace", "cmd_push"),
    "cmd_ticket_start": ("ticket", "cmd_ticket_start"),
    "cmd_ticket_list": ("ticket", "cmd_ticket_list"),
    "cmd_ticket_switch": ("ticket", "cmd_ticket_switch"),
    "cmd_ticket_end": ("ticket", "cmd_ticket_end"),
    "cmd_ticket_env_upsert": ("ticket", "cmd_ticket_env_upsert"),
    "cmd_discard": ("discard", "cmd_discard"),
    "cmd_archive": ("archive", "cmd_archive"),
    "cmd_archive_list": ("archive", "cmd_archive_list"),
    "cmd_ecosystem": ("ecosystem", "cmd_ecosystem"),
    "cmd_actor": ("actor", "cmd_actor"),
    "cmd_add_repo": ("repo", "cmd_add_repo"),
    "cmd_init": ("init", "cmd_init"),
    "cmd_repo_promote": ("repo", "cmd_repo_promote"),
    "cmd_repo_cleanup": ("repo", "cmd_repo_cleanup"),
    "cmd_check": ("check", "cmd_check"),
    "cmd_pr": ("pr", "cmd_pr"),
    "cmd_jira": ("jira", "cmd_jira"),
    "cmd_kb": ("kb", "cmd_kb"),
    "cmd_profile": ("profile", "cmd_profile"),
    "cmd_agent": ("agent_cmd", "cmd_agent"),
    "cmd_manifest": ("manifest_cmd", "cmd_manifest"),
    "cmd_release": ("release", "cmd_release"),
    "cmd_release_status": ("release", "cmd_release_status"),
    "cmd_release_log": ("release", "cmd_release_log"),
    "cmd_promote_main": ("promote_main", "cmd_promote_main"),
    "cmd_promote_main_revert": ("promote_main", "cmd_promote_main_revert"),
    "cmd_director": ("director", "cmd_director"),
    "cmd_director_action": ("director_action", "cmd_director_action"),
    "cmd_spin": ("spin", "cmd_spin"),
    "cmd_troubleshoot": ("troubleshoot", "cmd_troubleshoot"),
    "cmd_doctor": ("doctor", "cmd_doctor"),
    "cmd_bootstrap": ("bootstrap", "cmd_bootstrap"),
    "cmd_help": ("help_cmd", "cmd_help"),
    "cmd_uninstall": ("uninstall", "cmd_uninstall"),
    "cmd_shell": ("shell", "cmd_shell"),
}

__all__ = list(_COMMAND_EXPORTS)


def __getattr__(name: str):
    if name not in _COMMAND_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr_name = _COMMAND_EXPORTS[name]
    module = import_module(f".{module_name}", __name__)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value
