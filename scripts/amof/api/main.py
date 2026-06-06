"""FastAPI server for AMOF Control Plane."""

from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from amof.api.dependencies import queue_dispatcher
from amof.api.routers import (
    agents_catalog,
    auth,
    deployments,
    ecosystem,
    generated_build,
    gateway,
    intake,
    logs,
    models,
    repo_adoption,
    release,
    run,
    runpod,
    settings,
    users,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    queue_dispatcher.start()
    try:
        yield
    finally:
        queue_dispatcher.stop()


app = FastAPI(title="AMOF Control Plane API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

api_v1 = APIRouter(prefix="/api/v1", tags=["api"])
api_v1.include_router(auth.router)
api_v1.include_router(users.router)
api_v1.include_router(ecosystem.router)
api_v1.include_router(generated_build.router)
api_v1.include_router(gateway.router)
api_v1.include_router(release.router)
api_v1.include_router(deployments.router)
api_v1.include_router(agents_catalog.router)
api_v1.include_router(intake.router)
api_v1.include_router(run.router)
api_v1.include_router(logs.router)
api_v1.include_router(models.router)
api_v1.include_router(runpod.router)
api_v1.include_router(settings.router)

app.include_router(api_v1)


# Internal-control mirror of the v1 surface.
#
# `amof.api.auth.resolve_internal_control_user` only honors the
# `x-amof-internal-control-credential` header when the request path starts
# with `INTERNAL_CONTROL_PATH_PREFIX = "/api/v1/control"`. Without a parallel
# mount under that prefix, orchestrator clients (Cursor, internal AMOF
# agents) cannot drive any step-up-gated lifecycle action (build, deploy,
# promote, intake/amof/commit, release/bump, release/promote, release/
# environments mutations) — `require_step_up_user` short-circuits to 401.
#
# Mounting the same routers a second time under `/api/v1/control` exposes
# exactly the same handlers and dependency graph; auth is the only behavior
# difference (the credential header is recognised on this prefix only).
# The public `/api/v1/...` surface keeps its existing user-auth semantics.
api_v1_control = APIRouter(prefix="/api/v1/control", tags=["api", "control"])
api_v1_control.include_router(auth.router)
api_v1_control.include_router(users.router)
api_v1_control.include_router(ecosystem.router)
api_v1_control.include_router(generated_build.router)
api_v1_control.include_router(generated_build.control_router)
api_v1_control.include_router(gateway.router)
api_v1_control.include_router(release.router)
api_v1_control.include_router(deployments.router)
api_v1_control.include_router(agents_catalog.router)
api_v1_control.include_router(intake.router)
api_v1_control.include_router(repo_adoption.router)
api_v1_control.include_router(run.router)
api_v1_control.include_router(logs.router)
api_v1_control.include_router(models.router)
api_v1_control.include_router(runpod.router)
api_v1_control.include_router(settings.router)

app.include_router(api_v1_control)

@app.get("/health")
@app.get("/ready")
def health_check():
    return {"status": "ok"}
