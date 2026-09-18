from __future__ import annotations

import asyncio
import json
import secrets
import uuid
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated, Any, Literal

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field
from sqlalchemy import text

from pe_review_agent.config import Settings
from pe_review_agent.db import Database
from pe_review_agent.gerrit import GerritEventStream, GerritRestClient
from pe_review_agent.jobs import JobStore, ProjectReviewStartMode
from pe_review_agent.llm import LlmClient
from pe_review_agent.repos import RepositoryManager
from pe_review_agent.retry import PermanentError, TransientError

from .store import ControlStore

_PACKAGE_ROOT = Path(__file__).resolve().parent
_TEMPLATES = Jinja2Templates(directory=str(_PACKAGE_ROOT / "templates"))
_BASIC = HTTPBasic(auto_error=False)


class ProjectCreate(BaseModel):
    project: str = Field(min_length=1, max_length=512)
    enabled: bool = True
    review_start_mode: Literal["FROM_NOW", "INCLUDE_OPEN"] = "FROM_NOW"


class ProjectToggle(BaseModel):
    project: str = Field(min_length=1, max_length=512)
    enabled: bool


class ProjectReviewStartUpdate(BaseModel):
    project: str = Field(min_length=1, max_length=512)
    review_start_mode: Literal["FROM_NOW", "INCLUDE_OPEN"]


class ServiceToggle(BaseModel):
    enabled: bool


class RuntimeConfigUpdate(BaseModel):
    gerrit: dict[str, Any] = Field(default_factory=dict)
    llm: dict[str, Any] = Field(default_factory=dict)
    review: dict[str, Any] = Field(default_factory=dict)


class ProjectProbe(BaseModel):
    project: str = Field(min_length=1, max_length=512)


def create_admin_app(settings: Settings) -> FastAPI:
    _validate_admin_auth(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = Database(settings.database)
        jobs = JobStore(database.sessions)
        control = ControlStore(database.sessions)
        await control.ensure_bootstrap(settings)
        app.state.database = database
        app.state.jobs = jobs
        app.state.control = control
        try:
            yield
        finally:
            await database.close()

    app = FastAPI(
        title="Gerrit AI Reviewer Admin",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.base_settings = settings
    app.mount("/static", StaticFiles(directory=str(_PACKAGE_ROOT / "static")), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; "
            "form-action 'self'"
        )
        return response

    auth_dependency = _auth_dependency(settings)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request) -> JSONResponse:
        try:
            async with request.app.state.database.session() as session:
                await session.execute(text("SELECT 1"))
            return JSONResponse({"status": "ready", "detail": "database reachable"})
        except Exception as exc:
            return JSONResponse(
                {"status": "not_ready", "detail": f"database unavailable: {type(exc).__name__}"},
                status_code=503,
            )

    @app.get("/metrics", include_in_schema=False)
    async def metrics(_: str = Depends(auth_dependency)) -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request, _: str = Depends(auth_dependency)):
        control: ControlStore = request.app.state.control
        snapshot = await control.dashboard_snapshot()
        snapshot["service_enabled"] = await control.service_enabled(
            default=settings.service.enabled
        )
        return _render(request, "dashboard.html", page="dashboard", snapshot=snapshot)

    @app.get("/projects", response_class=HTMLResponse)
    async def projects(request: Request, _: str = Depends(auth_dependency)):
        control: ControlStore = request.app.state.control
        return _render(
            request,
            "projects.html",
            page="projects",
            projects=await control.list_projects(),
        )

    @app.get("/connections", response_class=HTMLResponse)
    async def connections(request: Request, _: str = Depends(auth_dependency)):
        control: ControlStore = request.app.state.control
        runtime = await control.runtime_config(settings)
        effective = await control.effective_settings(settings)
        overrides = await control.runtime_override_sections()
        legacy_snapshots = await control.legacy_runtime_snapshot_sections(settings)
        auth = effective.gerrit.rest_auth
        rest_secret_env = auth.password_env if auth.mode == "basic" else auth.token_env
        return _render(
            request,
            "connections.html",
            page="connections",
            runtime=runtime,
            effective=effective,
            secret_status=_secret_status(effective),
            connection_overrides=overrides,
            legacy_connection_snapshots=legacy_snapshots & {"gerrit", "llm"},
            rest_secret_env=rest_secret_env,
        )

    @app.get("/jobs", response_class=HTMLResponse)
    async def jobs_page(
        request: Request,
        _: str = Depends(auth_dependency),
        state_filter: str | None = Query(default=None, alias="state"),
        project: str | None = None,
    ):
        control: ControlStore = request.app.state.control
        jobs = await control.list_jobs(limit=200, state=state_filter, project=project)
        return _render(
            request,
            "jobs.html",
            page="jobs",
            jobs=jobs,
            projects=await control.list_projects(),
            state_filter=state_filter,
            project_filter=project,
        )

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    async def job_audit_page(
        request: Request,
        job_id: uuid.UUID,
        _: str = Depends(auth_dependency),
    ):
        control: ControlStore = request.app.state.control
        audit = await control.get_job_audit(job_id)
        if audit is None:
            raise HTTPException(status_code=404, detail="job not found")
        return _render(
            request,
            "job_detail.html",
            page="jobs",
            audit=audit,
        )

    @app.get("/logs", response_class=HTMLResponse)
    async def logs_page(
        request: Request,
        _: str = Depends(auth_dependency),
        component: str | None = None,
        level: str | None = None,
        q: str | None = None,
    ):
        entries = await asyncio.to_thread(
            _read_logs,
            settings.admin.log_root,
            component=component,
            level=level,
            query=q,
            limit=500,
        )
        components = await asyncio.to_thread(_log_components, settings.admin.log_root)
        return _render(
            request,
            "logs.html",
            page="logs",
            entries=entries,
            components=components,
            component_filter=component or "",
            level_filter=(level or "").upper(),
            query_filter=q or "",
        )

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request, _: str = Depends(auth_dependency)):
        control: ControlStore = request.app.state.control
        runtime = await control.runtime_config(settings)
        runtime["service_enabled"] = await control.service_enabled(default=settings.service.enabled)
        overrides = await control.runtime_override_sections()
        legacy_snapshots = await control.legacy_runtime_snapshot_sections(settings)
        return _render(
            request,
            "settings.html",
            page="settings",
            runtime=runtime,
            review_override_active="review" in overrides,
            legacy_review_snapshot="review" in legacy_snapshots,
        )

    @app.post("/api/projects")
    async def add_project(
        request: Request,
        payload: ProjectCreate,
        _: str = Depends(auth_dependency),
    ):
        _verify_csrf(request)
        if payload.enabled and not settings.service.enabled:
            raise HTTPException(
                status_code=409,
                detail=(
                    "bootstrap service.enabled=false is the hard kill switch; set it true and "
                    "restart before enabling from the Admin Web"
                ),
            )
        control: ControlStore = request.app.state.control
        project = await control.upsert_project(
            payload.project,
            enabled=payload.enabled,
            review_start_mode=ProjectReviewStartMode(payload.review_start_mode),
        )
        return {
            "ok": True,
            "project": project.project,
            "enabled": project.enabled,
            "review_start_mode": project.review_start_mode.value,
            "review_start_at": project.review_start_at,
        }

    @app.post("/api/projects/toggle")
    async def toggle_project(
        request: Request,
        payload: ProjectToggle,
        _: str = Depends(auth_dependency),
    ):
        _verify_csrf(request)
        control: ControlStore = request.app.state.control
        try:
            project = await control.set_project_enabled(payload.project, payload.enabled)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="project not found") from exc
        return {"ok": True, "project": project.project, "enabled": project.enabled}

    @app.post("/api/projects/review-start")
    async def set_project_review_start(
        request: Request,
        payload: ProjectReviewStartUpdate,
        _: str = Depends(auth_dependency),
    ):
        _verify_csrf(request)
        control: ControlStore = request.app.state.control
        try:
            project = await control.set_project_review_start_mode(
                payload.project,
                ProjectReviewStartMode(payload.review_start_mode),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="project not found") from exc
        return {
            "ok": True,
            "project": project.project,
            "review_start_mode": project.review_start_mode.value,
            "review_start_at": project.review_start_at,
        }

    @app.post("/api/projects/test")
    async def test_project(
        request: Request,
        payload: ProjectProbe,
        _: str = Depends(auth_dependency),
    ):
        _verify_csrf(request)
        control: ControlStore = request.app.state.control
        effective = await control.effective_settings(settings)
        gerrit_settings = effective.gerrit.model_copy(update={"projects": [payload.project]})
        client = GerritRestClient(gerrit_settings)
        try:
            head = await client.project_head(payload.project)
            repositories = RepositoryManager(effective.repos, gerrit_settings)
            revision = await repositories.probe_read_access(payload.project)
            return {
                "ok": True,
                "detail": (
                    f"REST Read + Git/SSH fetch access confirmed; HEAD ref = {head}; "
                    f"revision = {revision[:12]}"
                ),
            }
        except (PermanentError, TransientError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        finally:
            await client.aclose()

    @app.post("/api/service")
    async def toggle_service(
        request: Request,
        payload: ServiceToggle,
        _: str = Depends(auth_dependency),
    ):
        _verify_csrf(request)
        control: ControlStore = request.app.state.control
        await control.set_service_enabled(settings, payload.enabled)
        return {"ok": True, "enabled": payload.enabled}

    @app.put("/api/runtime-config")
    async def update_runtime(
        request: Request,
        payload: RuntimeConfigUpdate,
        _: str = Depends(auth_dependency),
    ):
        _verify_csrf(request)
        control: ControlStore = request.app.state.control
        current = await control.runtime_config(settings)
        updates: dict[str, Any] = {}
        if payload.gerrit:
            updates["gerrit"] = _validated_gerrit_update(payload.gerrit, current["gerrit"])
        if payload.llm:
            updates["llm"] = _validated_llm_update(payload.llm, current["llm"])
        if payload.review:
            updates["review"] = _validated_review_update(payload.review, current["review"])
        if updates:
            await control.replace_runtime_sections(settings, updates)
        return {
            "ok": True,
            "restart_required": True,
            "detail": (
                "Saved. Restart receiver/worker/reconciler to apply connection/review changes."
            ),
        }

    @app.post("/api/runtime-config/reset-connections")
    async def reset_connection_overrides(
        request: Request,
        _: str = Depends(auth_dependency),
    ):
        _verify_csrf(request)
        control: ControlStore = request.app.state.control
        await control.reset_runtime_sections(settings, "gerrit", "llm")
        return {
            "ok": True,
            "restart_required": True,
            "detail": "Connection overrides cleared. config.yaml values will apply after restart.",
        }

    @app.post("/api/runtime-config/reset-review")
    async def reset_review_override(
        request: Request,
        _: str = Depends(auth_dependency),
    ):
        _verify_csrf(request)
        control: ControlStore = request.app.state.control
        await control.reset_runtime_sections(settings, "review")
        return {
            "ok": True,
            "restart_required": True,
            "detail": (
                "Review policy override cleared. config.yaml values will apply after restart."
            ),
        }

    @app.post("/api/connections/gerrit")
    async def test_gerrit(
        request: Request,
        _: str = Depends(auth_dependency),
    ):
        _verify_csrf(request)
        control: ControlStore = request.app.state.control
        effective = await control.effective_settings(settings)
        enabled_projects = await control.enabled_projects(fallback=tuple(effective.gerrit.projects))
        gerrit_settings = effective.gerrit.model_copy(
            update={"projects": list(enabled_projects or effective.gerrit.projects)}
        )
        client = GerritRestClient(gerrit_settings)
        try:
            version = await client.server_version()
            ssh_detail = await _probe_gerrit_ssh(gerrit_settings)
            stream_detail = await _probe_stream_events(gerrit_settings)
            return {
                "ok": True,
                "detail": f"REST Gerrit {version}; {ssh_detail}; {stream_detail}",
            }
        except (PermanentError, TransientError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        finally:
            await client.aclose()

    @app.post("/api/connections/llm")
    async def test_llm(
        request: Request,
        _: str = Depends(auth_dependency),
    ):
        _verify_csrf(request)
        control: ControlStore = request.app.state.control
        effective = await control.effective_settings(settings)
        client = LlmClient(effective.llm)
        try:
            models = await client.check_connection()
            configured = effective.llm.model
            presence = "available" if configured in models else "not listed"
            sample = ", ".join(models[:5]) or "no model ids returned"
            return {
                "ok": True,
                "detail": (
                    f"/models reachable; configured model {configured!r} is {presence}. {sample}"
                ),
            }
        except (PermanentError, TransientError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        finally:
            await client.aclose()

    @app.post("/api/jobs/{job_id}/requeue")
    async def requeue_job(
        request: Request,
        job_id: uuid.UUID,
        _: str = Depends(auth_dependency),
    ):
        _verify_csrf(request)
        jobs: JobStore = request.app.state.jobs
        try:
            job = await jobs.requeue_failed(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "job_id": str(job.id), "state": job.state.value}

    @app.get("/api/logs")
    async def logs_api(
        _: str = Depends(auth_dependency),
        component: str | None = None,
        level: str | None = None,
        q: str | None = None,
        limit: int = Query(default=500, ge=1, le=1000),
    ):
        entries = await asyncio.to_thread(
            _read_logs,
            settings.admin.log_root,
            component=component,
            level=level,
            query=q,
            limit=limit,
        )
        return {"entries": entries}

    return app


async def run_admin(settings: Settings) -> None:
    app = create_admin_app(settings)
    config = uvicorn.Config(
        app,
        host=settings.admin.host,
        port=settings.admin.port,
        log_level="info",
        access_log=True,
        server_header=False,
        log_config=None,
    )
    await uvicorn.Server(config).serve()


def _log_components(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    return sorted(
        {path.name.split(".jsonl", 1)[0] for path in root.glob("*.jsonl*") if path.is_file()}
    )


def _read_logs(
    root: Path,
    *,
    component: str | None,
    level: str | None,
    query: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    if not root.is_dir():
        return []
    wanted_component = component.strip() if component else None
    wanted_level = level.strip().upper() if level else None
    needle = query.strip().lower() if query else None
    candidates: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.jsonl*")):
        if not path.is_file():
            continue
        inferred_component = path.name.split(".jsonl", 1)[0]
        if wanted_component and inferred_component != wanted_component:
            continue
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                lines = deque(handle, maxlen=max(limit * 4, 1000))
        except OSError:
            continue
        for line in lines:
            raw = line.rstrip("\r\n")
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {
                    "ts": "",
                    "level": "UNKNOWN",
                    "component": inferred_component,
                    "logger": "raw",
                    "message": raw,
                }
            if not isinstance(payload, dict):
                continue
            payload.setdefault("component", inferred_component)
            if wanted_level and str(payload.get("level", "")).upper() != wanted_level:
                continue
            searchable = json.dumps(payload, ensure_ascii=False, default=str).lower()
            if needle and needle not in searchable:
                continue
            candidates.append(payload)
    candidates.sort(key=lambda item: str(item.get("ts", "")), reverse=True)
    return candidates[:limit]


def _auth_dependency(settings: Settings):  # type: ignore[no-untyped-def]
    async def authenticate(
        credentials: Annotated[HTTPBasicCredentials | None, Depends(_BASIC)],
    ) -> str:
        if settings.admin.auth_mode == "none":
            return "local-admin"
        password = settings.admin.password
        if password is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"admin password env {settings.admin.password_env!r} is not configured",
            )
        expected_password = password.get_secret_value()
        if credentials is None or not (
            secrets.compare_digest(credentials.username, settings.admin.username)
            and secrets.compare_digest(credentials.password, expected_password)
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid admin credentials",
                headers={"WWW-Authenticate": 'Basic realm="Gerrit AI Reviewer"'},
            )
        return credentials.username

    return authenticate


def _validate_admin_auth(settings: Settings) -> None:
    if settings.admin.auth_mode == "none" and settings.admin.host not in {
        "127.0.0.1",
        "::1",
        "localhost",
    }:
        raise ValueError("admin.auth_mode=none is only allowed on a loopback admin.host")


def _render(request: Request, template: str, **context: Any):
    csrf = request.cookies.get("pe_review_csrf") or secrets.token_urlsafe(32)
    response = _TEMPLATES.TemplateResponse(
        request=request,
        name=template,
        context={"csrf_token": csrf, **context},
    )
    if request.cookies.get("pe_review_csrf") != csrf:
        response.set_cookie(
            "pe_review_csrf",
            csrf,
            httponly=False,
            secure=False,
            samesite="strict",
            max_age=8 * 60 * 60,
        )
    return response


def _verify_csrf(request: Request) -> None:
    cookie = request.cookies.get("pe_review_csrf")
    header = request.headers.get("X-CSRF-Token")
    if not cookie or not header or not secrets.compare_digest(cookie, header):
        raise HTTPException(status_code=403, detail="invalid CSRF token")


def _secret_status(settings: Settings) -> dict[str, bool]:
    return {
        "llm_api_key": settings.llm.api_key is not None,
        "gerrit_rest_secret": settings.gerrit.rest_auth.secret() is not None,
        "ssh_key": settings.gerrit.ssh_key_path.is_file(),
        "known_hosts": bool(
            settings.gerrit.known_hosts_path and settings.gerrit.known_hosts_path.is_file()
        ),
    }


def _validated_gerrit_update(value: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "ssh_host",
        "ssh_port",
        "ssh_user",
        "rest_url",
        "rest_auth_mode",
        "rest_username",
    }
    result = {**current, **{key: item for key, item in value.items() if key in allowed}}
    if not isinstance(result.get("ssh_host"), str) or not result["ssh_host"].strip():
        raise HTTPException(status_code=422, detail="Gerrit SSH host is required")
    try:
        port = int(result.get("ssh_port"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Gerrit SSH port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise HTTPException(status_code=422, detail="Gerrit SSH port is out of range")
    result["ssh_port"] = port
    if result.get("rest_auth_mode") not in {"none", "basic", "bearer"}:
        raise HTTPException(status_code=422, detail="Unsupported Gerrit REST auth mode")
    if result.get("rest_auth_mode") == "basic":
        username = result.get("rest_username")
        if not isinstance(username, str) or not username.strip():
            raise HTTPException(
                status_code=422,
                detail="Gerrit REST username is required for Basic auth",
            )
        result["rest_username"] = username.strip()
    elif result.get("rest_username") == "":
        result["rest_username"] = None
    return result


def _validated_llm_update(value: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    allowed = {"base_url", "model", "temperature", "max_output_tokens"}
    result = {**current, **{key: item for key, item in value.items() if key in allowed}}
    if not isinstance(result.get("base_url"), str) or not result["base_url"].strip():
        raise HTTPException(status_code=422, detail="LLM base URL is required")
    if not isinstance(result.get("model"), str) or not result["model"].strip():
        raise HTTPException(status_code=422, detail="LLM model is required")
    try:
        temperature = float(result.get("temperature"))
        max_tokens = int(result.get("max_output_tokens"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Invalid LLM numeric setting") from exc
    if not 0 <= temperature <= 2:
        raise HTTPException(status_code=422, detail="LLM temperature must be between 0 and 2")
    if max_tokens < 256:
        raise HTTPException(status_code=422, detail="LLM max output tokens must be at least 256")
    result["temperature"] = temperature
    result["max_output_tokens"] = max_tokens
    return result


def _validated_review_update(value: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "policy_version",
        "output_language",
        "max_findings",
        "min_confidence",
        "max_candidate_chunks",
        "max_llm_calls_per_job",
        "max_tool_calls_per_job",
        "verifier_budget_fraction",
    }
    result = {**current, **{key: item for key, item in value.items() if key in allowed}}
    if not isinstance(result.get("policy_version"), str) or not result["policy_version"].strip():
        raise HTTPException(status_code=422, detail="Review policy version is required")
    if result.get("output_language") not in {"ko-KR", "en-US"}:
        raise HTTPException(status_code=422, detail="Review output language must be ko-KR or en-US")
    try:
        max_findings = int(result.get("max_findings"))
        confidence = float(result.get("min_confidence"))
        max_candidate_chunks = int(result.get("max_candidate_chunks"))
        max_llm_calls = int(result.get("max_llm_calls_per_job"))
        max_tool_calls = int(result.get("max_tool_calls_per_job"))
        verifier_fraction = float(result.get("verifier_budget_fraction", 1 / 3))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Invalid review numeric setting") from exc
    if not 0 <= max_findings <= 50:
        raise HTTPException(status_code=422, detail="max_findings must be between 0 and 50")
    if not 0 <= confidence <= 1:
        raise HTTPException(status_code=422, detail="min_confidence must be between 0 and 1")
    if not 1 <= max_candidate_chunks <= 512:
        raise HTTPException(
            status_code=422, detail="max_candidate_chunks must be between 1 and 512"
        )
    if not 1 <= max_llm_calls <= 1_000:
        raise HTTPException(
            status_code=422, detail="max_llm_calls_per_job must be between 1 and 1000"
        )
    if not 0 <= max_tool_calls <= 5_000:
        raise HTTPException(
            status_code=422, detail="max_tool_calls_per_job must be between 0 and 5000"
        )
    if not 0 < verifier_fraction < 1:
        raise HTTPException(
            status_code=422,
            detail="verifier_budget_fraction must be greater than 0 and less than 1",
        )
    result["max_findings"] = max_findings
    result["min_confidence"] = confidence
    result["max_candidate_chunks"] = max_candidate_chunks
    result["max_llm_calls_per_job"] = max_llm_calls
    result["max_tool_calls_per_job"] = max_tool_calls
    result["verifier_budget_fraction"] = verifier_fraction
    result.pop("max_input_tokens_per_job", None)
    return result


async def _probe_gerrit_ssh(settings) -> str:  # type: ignore[no-untyped-def]
    stream = GerritEventStream(settings)
    command = list(stream.ssh_command)
    command[-4:] = ["gerrit", "version"]
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
    except (OSError, TimeoutError) as exc:
        raise TransientError(f"Gerrit SSH probe failed: {exc}") from exc
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()[:500]
        raise PermanentError(f"Gerrit SSH probe failed ({process.returncode}): {detail}")
    version = stdout.decode("utf-8", errors="replace").strip()
    return f"SSH OK ({version or 'version command accepted'})"


async def _probe_stream_events(settings) -> str:  # type: ignore[no-untyped-def]
    command = GerritEventStream(settings).ssh_command
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        try:
            await asyncio.wait_for(process.wait(), timeout=1.5)
        except TimeoutError:
            process.terminate()
            await process.wait()
            return "Stream Events capability OK"
        stderr = b""
        if process.stderr is not None:
            stderr = await process.stderr.read()
        detail = stderr.decode("utf-8", errors="replace").strip()[:500]
        message = (
            f"Stream Events probe exited immediately ({process.returncode}): "
            f"{detail or 'no stderr'}"
        )
        raise PermanentError(message)
    finally:
        if process.returncode is None:
            process.terminate()
            with suppress(Exception):
                await process.wait()
