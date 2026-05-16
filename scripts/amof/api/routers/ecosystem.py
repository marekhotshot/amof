import sys
import uuid
import json
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException, BackgroundTasks, Depends

from amof.cli import get_available_ecosystems
from amof.state import get_all_tickets, get_active_ticket
from amof.api.command_builder import (
    get_workspace_root,
    build_director_gmd_dev_local_proof_command,
    build_install_command,
    build_spin_deploy_command,
    build_spin_destroy_command,
    build_ticket_start_command,
    build_ticket_list_command,
    build_ticket_switch_command,
    build_ticket_end_command,
    build_validate_command,
    build_status_command,
    build_sync_command,
    build_push_command,
    build_archive_command,
    build_discard_command,
)
from amof.api.dependencies import get_ecosystem_manager, get_queue_dispatcher, get_run_manager, require_operator_user, require_step_up_user
from amof.api.services.branch_inventory import build_branch_truth
from amof.api.services.runner import execute_action, run_subprocess_task
from amof.api.services.ticket_save import build_ticket_save_options, run_ticket_save
from amof.api.models.ecosystem import CreateEcosystemRequest, EcosystemReadModel
from amof.api.models.action import (
    DirectorGmdDevLocalProofRequest,
    TicketStartRequest,
    TicketSwitchRequest,
    TicketEndRequest,
    TicketSaveRequest,
    RunRequest,
    ActionResponse,
)
from amof.api.run_manager import RUN_STATUS_QUEUED
from amof.api.routers.release import build_lifecycle_environment_index
from amof.queue import QueueDispatcher

router = APIRouter(prefix="/ecosystems", tags=["ecosystems"])

@router.get("", response_model=Dict[str, List[EcosystemReadModel]])
def list_ecosystems(eco_mgr = Depends(get_ecosystem_manager), run_mgr = Depends(get_run_manager)):
    ecosystems = get_available_ecosystems()
    result = []
    for name in ecosystems:
        try:
            data = eco_mgr.get_ecosystem_summary(name)
            repos = []
            for repo in (data.get("repos") or []):
                if isinstance(repo, dict):
                    repos.append(repo)
                elif isinstance(repo, str):
                    repos.append(repo)
                else:
                    repos.append(str(repo))
            
            # Get active tasks for this ecosystem
            active_tasks = 0
            for run_data in run_mgr.list_runs(ecosystem=name):
                if run_data.status in [RUN_STATUS_QUEUED, "running"]:
                    active_tasks += 1

            branch_truth = build_branch_truth(name)
            result.append(EcosystemReadModel(
                name=data["name"],
                path=data.get("path", ""),
                repos=repos,
                kb_files_count=data.get("kb_files_count", 0),
                journal_files_count=data.get("journal_files_count", 0),
                last_index_time=data.get("last_index_time", "N/A"),
                active_tasks=active_tasks,
                branch_summary=branch_truth["branch_summary"],
                repo_statuses=branch_truth["repo_statuses"],
                available_branches=branch_truth["available_branches"],
                repo_branch_inventory=branch_truth["repo_branch_inventory"],
            ))
        except ValueError:
            continue
    return {"ecosystems": result}

@router.post("", status_code=201)
def create_ecosystem(req: CreateEcosystemRequest, eco_mgr = Depends(get_ecosystem_manager)):
    try:
        res = eco_mgr.create_ecosystem(req.name.strip(), req.repos, req.description)
        return res
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/preview")
def preview_ecosystem(req: CreateEcosystemRequest, eco_mgr = Depends(get_ecosystem_manager)):
    """Generate and return the YAML manifest without saving it."""
    # Simplified version for MVP: Just build the YAML string in memory
    manifest_content = f"name: {req.name.strip()}\n"
    if req.description:
        manifest_content += f"description: {repr(req.description.strip())}\n"
    else:
        manifest_content += f"description: 'Generated ecosystem'\n"
    
    manifest_content += "repos:\n"
    for repo in req.repos:
        if isinstance(repo, str):
            repo_name = repo
            repo_url = ''
        elif isinstance(repo, dict):
            repo_name = repo.get('name', 'unnamed')
            repo_url = repo.get('url', '')
        else:
            repo_name = getattr(repo, 'name', 'unnamed')
            repo_url = getattr(repo, 'url', '')
        manifest_content += f"  - name: {repo_name}\n"
        manifest_content += f"    url: '{repo_url}'\n"
        
    return {"yaml_content": manifest_content}

@router.get("/{name}", response_model=EcosystemReadModel)
def get_ecosystem(name: str, eco_mgr = Depends(get_ecosystem_manager), run_mgr = Depends(get_run_manager)):
    try:
        data = eco_mgr.get_ecosystem_summary(name)
        # Parse repositories ensuring they match union type
        repos = []
        for repo in (data.get("repos") or []):
            if isinstance(repo, dict):
                repos.append(repo)
            elif isinstance(repo, str):
                repos.append(repo)
            else:
                repos.append(str(repo))

        active_tasks = 0
        for run_data in run_mgr.list_runs(ecosystem=name):
            if run_data.status in [RUN_STATUS_QUEUED, "running"]:
                active_tasks += 1

        branch_truth = build_branch_truth(name)
        return EcosystemReadModel(
            name=data["name"],
            path=data["path"],
            repos=repos,
            kb_files_count=data.get("kb_files_count", 0),
            journal_files_count=data.get("journal_files_count", 0),
            last_index_time=data.get("last_index_time", "N/A"),
            active_tasks=active_tasks,
            branch_summary=branch_truth["branch_summary"],
            repo_statuses=branch_truth["repo_statuses"],
            available_branches=branch_truth["available_branches"],
            repo_branch_inventory=branch_truth["repo_branch_inventory"],
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

@router.delete("/{name}")
def delete_ecosystem(name: str, eco_mgr = Depends(get_ecosystem_manager)):
    try:
        return eco_mgr.delete_ecosystem(name)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

@router.get("/{name}/tickets")
def list_tickets(name: str):
    """Return structured ticket data for the ecosystem."""
    ecosystems = get_available_ecosystems()
    if name not in ecosystems:
        raise HTTPException(status_code=404, detail=f"Ecosystem {name} not found")
    tickets = get_all_tickets(ecosystem=name)
    active = get_active_ticket(ecosystem=name)
    try:
        environment_index = build_lifecycle_environment_index(name)
    except Exception:
        environment_index = {}
    result = []
    
    # Add synthetic main ticket
    from amof.state import get_effective_repos
    from amof.manifest import load_manifest
    
    try:
        manifest = load_manifest(name)
        base_repos = get_effective_repos(manifest)
        result.append({
            "ticket_id": "main",
            "active": active is None,
            "repos": {r.get("name"): r.get("branch", "main") for r in base_repos},
            "created_at": None,
            "protected": True,
        })
    except Exception:
        pass

    for ticket_id, info in sorted(tickets.items()):
        repos = info.get("repos", {})
        stage_id = str(info.get("stage_id") or "").strip() or None
        environment_id = str(info.get("environment_id") or "").strip() or None
        linked_environment = None
        if stage_id or environment_id:
            linked_environment = environment_index.get(
                environment_id or stage_id or ""
            ) or environment_index.get(stage_id or "")
        result.append({
            "ticket_id": ticket_id,
            "active": ticket_id == active,
            "repos": {rname: branch for rname, branch in sorted(repos.items())},
            "created_at": info.get("created_at"),
            "stage_id": stage_id,
            "environment_id": environment_id,
            "repo_selections": info.get("repo_selections") if isinstance(info.get("repo_selections"), list) else None,
            "linked_environment": linked_environment,
        })
    return {"tickets": result, "active_ticket": active}


# Nested actions endpoints
@router.post("/{name}/actions/install")
def action_install(name: str, background_tasks: BackgroundTasks, run_mgr = Depends(get_run_manager)):
    return execute_action(run_mgr, name, "install", build_install_command, background_tasks)

@router.post("/{name}/actions/spin/deploy")
def action_spin_deploy(name: str, background_tasks: BackgroundTasks, run_mgr = Depends(get_run_manager)):
    return execute_action(run_mgr, name, "spin_deploy", build_spin_deploy_command, background_tasks)

@router.post("/{name}/actions/spin/destroy")
def action_spin_destroy(name: str, background_tasks: BackgroundTasks, run_mgr = Depends(get_run_manager)):
    return execute_action(run_mgr, name, "spin_destroy", build_spin_destroy_command, background_tasks)

@router.post("/{name}/actions/validate")
def action_validate(name: str, background_tasks: BackgroundTasks, run_mgr = Depends(get_run_manager)):
    return execute_action(run_mgr, name, "validate", build_validate_command, background_tasks)

@router.post("/{name}/actions/ticket-start")
def action_ticket_start(name: str, req: TicketStartRequest, background_tasks: BackgroundTasks, run_mgr = Depends(get_run_manager)):
    repos_str = ",".join(req.repos) if req.repos else None
    repo_selections = None
    if req.repo_selections:
        repo_selections = json.dumps(
            [selection.model_dump() for selection in req.repo_selections],
            separators=(",", ":"),
        )
    return execute_action(
        run_mgr,
        name,
        "ticket_start",
        build_ticket_start_command,
        background_tasks,
        req.ticket_id,
        repos_str,
        req.stage_id,
        req.environment_id,
        repo_selections,
    )


@router.post("/{name}/actions/ticket-list")
def action_ticket_list(name: str, background_tasks: BackgroundTasks, run_mgr = Depends(get_run_manager)):
    return execute_action(run_mgr, name, "ticket_list", build_ticket_list_command, background_tasks)


@router.post("/{name}/actions/ticket-switch")
def action_ticket_switch(name: str, req: TicketSwitchRequest, background_tasks: BackgroundTasks, run_mgr = Depends(get_run_manager)):
    return execute_action(
        run_mgr,
        name,
        "ticket_switch",
        build_ticket_switch_command,
        background_tasks,
        req.ticket_id,
        request_id=req.request_id,
    )


@router.post("/{name}/actions/ticket-end")
def action_ticket_end(name: str, req: TicketEndRequest, background_tasks: BackgroundTasks, run_mgr = Depends(get_run_manager)):
    return execute_action(
        run_mgr, name, "ticket_end", build_ticket_end_command, background_tasks,
        req.ticket_id, bool(req.cleanup), bool(req.cleanup_local),
    )


@router.get("/{name}/ticket-save/options")
def ticket_save_options(
    name: str,
    ticket_id: str,
    current_user=Depends(require_operator_user),
):
    del current_user
    ecosystems = get_available_ecosystems()
    if name not in ecosystems:
        raise HTTPException(status_code=404, detail=f"Ecosystem {name} not found")
    return build_ticket_save_options(name, ticket_id)


@router.post("/{name}/actions/ticket-save")
def action_ticket_save(
    name: str,
    req: TicketSaveRequest,
    background_tasks: BackgroundTasks,
    run_mgr = Depends(get_run_manager),
    current_user=Depends(require_step_up_user),
):
    del current_user
    ecosystems = get_available_ecosystems()
    if name not in ecosystems:
        raise HTTPException(status_code=404, detail=f"Ecosystem {name} not found")
    preview = build_ticket_save_options(name, req.ticket_id)
    selected_option = next((entry for entry in preview["options"] if entry["id"] == req.option_id), None)
    if selected_option is None:
        raise HTTPException(status_code=400, detail=f"Unsupported save strategy: {req.option_id}")
    run_id = run_mgr.create_run(
        name,
        "ticket_save",
        ["ticket-save", name, req.ticket_id, req.option_id],
        queue_payload={
            "kind": "inline",
            "ecosystem": name,
            "ticket_id": req.ticket_id,
            "option_id": req.option_id,
        },
    )
    background_tasks.add_task(
        run_ticket_save,
        run_mgr,
        run_id,
        name,
        req.ticket_id,
        req.option_id,
        req.expected_current_tag,
    )
    return {
        "task_id": run_id,
        "run_id": run_id,
        "status": RUN_STATUS_QUEUED,
        "ticket_id": req.ticket_id,
        "target_tag": selected_option["tag"],
        "control_repo": preview.get("control_repo"),
    }


@router.post("/{name}/actions/status")
def action_status(name: str, background_tasks: BackgroundTasks, run_mgr = Depends(get_run_manager)):
    return execute_action(run_mgr, name, "status", build_status_command, background_tasks)

@router.post("/{name}/actions/sync")
def action_sync(name: str, background_tasks: BackgroundTasks, run_mgr = Depends(get_run_manager)):
    return execute_action(run_mgr, name, "sync", build_sync_command, background_tasks)

@router.post("/{name}/actions/push")
def action_push(name: str, background_tasks: BackgroundTasks, run_mgr = Depends(get_run_manager)):
    return execute_action(run_mgr, name, "push", build_push_command, background_tasks)

@router.post("/{name}/actions/archive")
def action_archive(name: str, background_tasks: BackgroundTasks, run_mgr = Depends(get_run_manager)):
    return execute_action(run_mgr, name, "archive", build_archive_command, background_tasks)

@router.post("/{name}/actions/discard")
def action_discard(name: str, background_tasks: BackgroundTasks, run_mgr = Depends(get_run_manager)):
    return execute_action(run_mgr, name, "discard", build_discard_command, background_tasks)


@router.post("/{name}/actions/director/gmd-dev-local-proof")
def action_director_gmd_dev_local_proof(
    name: str,
    req: DirectorGmdDevLocalProofRequest,
    dispatcher: QueueDispatcher = Depends(get_queue_dispatcher),
    current_user=Depends(require_step_up_user),
):
    del current_user
    ecosystems = get_available_ecosystems()
    if name not in ecosystems:
        raise HTTPException(status_code=404, detail=f"Ecosystem {name} not found")
    if name != "gmd":
        raise HTTPException(status_code=400, detail="director.gmd_dev_local_proof.v1 is scoped to ecosystem gmd.")
    environment_id = str(req.environment_id or "dev").strip() or "dev"
    if environment_id != "dev":
        raise HTTPException(status_code=400, detail="director.gmd_dev_local_proof.v1 is scoped to environment dev.")

    root = get_workspace_root()
    ticket_id = str(req.ticket_id or "UP8-GOLDEN-GMD-DEV-CONTROL-API-001").strip()
    input_path = str(
        req.input_path
        or "ecosystems/amof-platform/audit/2026-04-25-ultra-plan-8-wave4-control-api-action-input-gmd-dev.json"
    )
    output_path = str(
        req.output_path
        or "ecosystems/amof-platform/audit/2026-04-25-ultra-plan-8-wave4-control-api-action-result-gmd-dev.json"
    )
    cmd, cwd = build_director_gmd_dev_local_proof_command(
        root,
        name,
        input_path=input_path,
        output_path=output_path,
        local_port=req.local_port,
    )
    event_payload = {
        "action": "gmd-dev-local-proof",
        "director_action": "director.gmd_dev_local_proof.v1",
        "environment_id": "dev",
        "stage_id": "dev",
        "target_environment": "dev",
        "ticket_id": ticket_id,
        "input_path": input_path,
        "result_path": output_path,
        "local_only": True,
        "cloud_dev": False,
        "release_promote": False,
        "request_id": req.request_id,
    }
    run_id = dispatcher.enqueue_subprocess(
        name,
        "director-action/gmd-dev-local-proof",
        cmd,
        cwd=str(cwd),
        event_payload=event_payload,
    )
    return {
        "task_id": run_id,
        "run_id": run_id,
        "status": RUN_STATUS_QUEUED,
        "action": "director.gmd_dev_local_proof.v1",
        "target_environment": "dev",
        "input_path": input_path,
        "result_path": output_path,
        "request_id": req.request_id,
    }


@router.post("/{name}/actions/index")
def index_ecosystem(name: str, background_tasks: BackgroundTasks, run_mgr = Depends(get_run_manager)):
    ecosystems = get_available_ecosystems()
    if name not in ecosystems:
        raise HTTPException(status_code=404, detail=f"Ecosystem {name} not found")
    root = get_workspace_root()
    cmd = [
        sys.executable, str(root / "scripts" / "amof.py"), "-e", name,
        "agent", "Index the codebase", "--index"
    ]
    run_id = run_mgr.create_run(name, "index", cmd)
    background_tasks.add_task(run_subprocess_task, run_mgr, run_id, cmd, str(root))
    return {"task_id": run_id, "run_id": run_id, "status": RUN_STATUS_QUEUED}

@router.post("/{name}/actions/agent", response_model=ActionResponse)
def run_agent(name: str, req: RunRequest, dispatcher: QueueDispatcher = Depends(get_queue_dispatcher)):
    ecosystems = get_available_ecosystems()
    if name not in ecosystems:
        raise HTTPException(status_code=404, detail=f"Ecosystem {name} not found")
    run_id, session_id = dispatcher.enqueue_agent(
        name,
        prompt=req.prompt,
        mode=req.mode or "execute",
        runtime_profile=req.runtime_profile,
        session_id=req.session_id,
        agent_id=req.agent_id,
    )
    return {"task_id": run_id, "run_id": run_id, "session_id": session_id, "status": RUN_STATUS_QUEUED}
