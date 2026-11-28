"""Local HTTP facade. Durable requests are executed only by the resident controller."""
from datetime import datetime
import json
import os
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator

from smart_home_energy.ac_domain import ACConstraints
from smart_home_energy.assistant_defaults import preferences
from smart_home_energy.config import HemsSettings
from smart_home_energy.domain import TaskConstraints, encode, utcnow
from smart_home_energy.mcp_server import SmartHomeMCPServer, create_mcp_router
from smart_home_energy.repository import Repository


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ACConditions(Input):
    start: datetime
    end: datetime
    target_c: float = 27
    minimum_c: float = 26
    maximum_c: float = 28
    ambient_c: float = 32
    warming_seconds: float = 1800
    device_id: Literal["ac-1"] = "ac-1"

    @model_validator(mode="after")
    def domain_validation(self):
        ACConstraints(**self.model_dump())
        return self


class WasherConditions(Input):
    earliest: datetime
    deadline: datetime
    quiet_start: int | None = Field(default=1380, strict=True)
    quiet_end: int | None = Field(default=420, strict=True)
    device_id: Literal["washer-1"] = "washer-1"

    @model_validator(mode="after")
    def domain_validation(self):
        TaskConstraints(**self.model_dump())
        return self


class PlanPayload(Input):
    task_id: str | None = Field(default=None, min_length=1, max_length=128)
    expected_version: int | None = Field(default=None, ge=1, strict=True)


class ACPayload(PlanPayload):
    device_id: Literal["ac-1"] = "ac-1"
    constraints: ACConditions


class WasherPayload(PlanPayload):
    device_id: Literal["washer-1"] = "washer-1"
    constraints: WasherConditions


class RequestInput(Input):
    request_id: str = Field(pattern=r"^[A-Za-z0-9:_-]{1,128}$")


class PlanRequest(RequestInput):
    @model_validator(mode="after")
    def task_context(self):
        editing = self.kind.startswith("schedule_")
        if editing != (self.payload.task_id is not None and self.payload.expected_version is not None):
            raise ValueError("修改計畫需要任務及版本；新計畫不可指定既有任務")
        if not editing and (self.payload.task_id is not None or self.payload.expected_version is not None):
            raise ValueError("新計畫不可指定既有任務")
        return self


class ACRequest(PlanRequest):
    kind: Literal["create_ac_form", "schedule_ac_form"]
    payload: ACPayload


class WasherRequest(PlanRequest):
    kind: Literal["create_form", "schedule_form"]
    payload: WasherPayload


class TaskPayload(Input):
    task_id: str = Field(min_length=1, max_length=128)
    expected_version: int = Field(ge=1, strict=True)


class DetailsPayload(TaskPayload):
    text: str = Field(min_length=1, max_length=4000)


class ApprovalPayload(TaskPayload):
    max_cost: float | None = Field(default=None, ge=0)


class ConfirmedPayload(TaskPayload):
    confirmed: bool = Field(strict=True)

    @model_validator(mode="after")
    def require_confirmation(self):
        if self.confirmed is not True:
            raise ValueError("操作需要使用者再次確認")
        return self


class ApprovalRequest(RequestInput):
    kind: Literal["approve"]
    payload: ApprovalPayload


class TaskRequest(RequestInput):
    kind: Literal["cancel", "query"]
    payload: TaskPayload


class DetailsRequest(RequestInput):
    kind: Literal["details"]
    payload: DetailsPayload


class ConfirmedRequest(RequestInput):
    kind: Literal["abort", "reset"]
    payload: ConfirmedPayload


class BulbPayload(Input):
    device_id: Literal["bulb-1"] = "bulb-1"
    power: bool | None = Field(default=None, strict=True)
    brightness_percent: float | None = Field(default=None, ge=0, le=100)
    kelvin: float | None = Field(default=None, ge=2700, le=6500)
    duration_seconds: float = Field(default=0.5, ge=0, le=10)

    @model_validator(mode="after")
    def light_target(self):
        if self.power is None and self.brightness_percent is None and self.kelvin is None:
            raise ValueError("燈泡請求須指定開關、亮度或色溫")
        if self.power is True and self.brightness_percent == 0:
            raise ValueError("開燈不能同時指定 0% 亮度")
        return self


class BulbRequest(RequestInput):
    kind: Literal["set_bulb"]
    payload: BulbPayload


class PreferencesRequest(RequestInput):
    kind: Literal["assistant_preferences"]
    payload: dict


class TariffRequest(RequestInput):
    kind: Literal["tariff_settings"]
    payload: dict


class ClockPayload(Input):
    speed: float = Field(ge=0.1, le=100, allow_inf_nan=False)


class ClockRequest(RequestInput):
    kind: Literal["clock"]
    payload: ClockPayload


class RefreshTariffRequest(RequestInput):
    kind: Literal["refresh_tariff_source"]
    payload: dict = Field(default_factory=dict)


class LightingRulePayload(Input):
    device_id: Literal["bulb-1"] = "bulb-1"
    expected_version: int = Field(ge=1)
    enabled: bool = Field(strict=True)
    day_start: int = Field(ge=0, le=1439)
    night_start: int = Field(ge=0, le=1439)
    brightness: float = Field(ge=1, le=100)
    kelvin: float = Field(ge=2700, le=6500)
    presence_enabled: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def rule_valid(self):
        if self.day_start == self.night_start:
            raise ValueError("白天與晚間開始時間不能相同")
        if self.presence_enabled:
            raise ValueError("人體感應功能尚未啟用")
        return self


class LightingRuleRequest(RequestInput):
    kind: Literal["lighting_rule"]
    payload: LightingRulePayload


class LightingResumePayload(Input):
    device_id: Literal["bulb-1"] = "bulb-1"


class LightingResumeRequest(RequestInput):
    kind: Literal["lighting_resume"]
    payload: LightingResumePayload = Field(default_factory=LightingResumePayload)


class CurtainPositionPayload(Input):
    device_id: Literal["curtain-1"] = "curtain-1"
    position: float = Field(ge=0, le=100)
    duration_seconds: float = Field(default=3.0, ge=0, le=120)


class CurtainPositionRequest(RequestInput):
    kind: Literal["curtain_position"]
    payload: CurtainPositionPayload


class CurtainStopPayload(Input):
    device_id: Literal["curtain-1"] = "curtain-1"


class CurtainStopRequest(RequestInput):
    kind: Literal["curtain_stop"]
    payload: CurtainStopPayload = Field(default_factory=CurtainStopPayload)


class CurtainScenePayload(Input):
    device_id: Literal["curtain-1"] = "curtain-1"
    scene: Literal["open", "half", "close"]
    duration_seconds: float = Field(default=2.0, ge=0, le=120)


class CurtainSceneRequest(RequestInput):
    kind: Literal["curtain_scene"]
    payload: CurtainScenePayload


class CurtainScheduleCreatePayload(Input):
    device_id: Literal["curtain-1"] = "curtain-1"
    time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    position: float = Field(ge=0, le=100)
    enabled: bool = Field(default=True, strict=True)


class CurtainScheduleCreateRequest(RequestInput):
    kind: Literal["curtain_schedule_create"]
    payload: CurtainScheduleCreatePayload


class CurtainScheduleTogglePayload(Input):
    device_id: Literal["curtain-1"] = "curtain-1"
    schedule_id: str | None = Field(default=None, min_length=1, max_length=128)
    time: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    enabled: bool | None = Field(default=None, strict=True)

    @model_validator(mode="after")
    def target_present(self):
        if self.schedule_id is None and self.time is None:
            raise ValueError("請指定排程識別碼或時間")
        return self


class CurtainScheduleToggleRequest(RequestInput):
    kind: Literal["curtain_schedule_toggle"]
    payload: CurtainScheduleTogglePayload


class CurtainScheduleDeletePayload(Input):
    device_id: Literal["curtain-1"] = "curtain-1"
    schedule_id: str | None = Field(default=None, min_length=1, max_length=128)
    time: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    delete_all: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def target_present(self):
        if not self.delete_all and self.schedule_id is None and self.time is None:
            raise ValueError("請指定要刪除的排程識別碼、時間或 delete_all")
        return self


class CurtainScheduleDeleteRequest(RequestInput):
    kind: Literal["curtain_schedule_delete"]
    payload: CurtainScheduleDeletePayload


class TVControlPayload(Input):
    device_id: Literal["tv-1"] = "tv-1"
    power: bool | None = Field(default=None, strict=True)
    volume: int | None = Field(default=None, ge=0, le=100, strict=True)
    muted: bool | None = Field(default=None, strict=True)
    input: Literal["HDMI1", "HDMI2", "HDMI3", "HDMI4"] | None = None
    playback: Literal["playing", "paused", "stopped"] | None = None

    @model_validator(mode="after")
    def target_present(self):
        if all(val is None for val in (self.power, self.volume, self.muted, self.input, self.playback)):
            raise ValueError("請至少指定一項電視控制參數")
        return self


class TVControlRequest(RequestInput):
    kind: Literal["tv_control"]
    payload: TVControlPayload


WebRequest = Annotated[
    ACRequest | WasherRequest | ApprovalRequest | TaskRequest | DetailsRequest | ConfirmedRequest
    | BulbRequest | PreferencesRequest | TariffRequest | ClockRequest | RefreshTariffRequest
    | LightingRuleRequest | LightingResumeRequest
    | CurtainPositionRequest | CurtainStopRequest | CurtainSceneRequest
    | CurtainScheduleCreateRequest | CurtainScheduleToggleRequest | CurtainScheduleDeleteRequest
    | TVControlRequest,
    Body(discriminator="kind"),
]


def create_app(repo: Repository | None = None, hems: HemsSettings | None = None,
               static_directory: Path | None = None) -> FastAPI:
    hems = hems or HemsSettings.from_env()
    repo = repo or Repository(hems.database)
    mcp_server = SmartHomeMCPServer(repo, hems)
    runtime_url = os.getenv("HEMS_COPILOT_RUNTIME_URL", "http://127.0.0.1:8503").rstrip("/")
    app = FastAPI(title="智慧客廳本機 API", docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)

    @app.get("/api/home")
    def home():
        from smart_home_energy import curtain, tv
        from smart_home_energy.lighting_runner import lighting_summary
        with repo.transaction():
            clock, real = repo.clock(), utcnow()
            devices = {}
            for identifier in ("ac-1", "washer-1", "bulb-1"):
                state = repo.device(identifier)
                fresh = bool(state and -2 <= (real - datetime.fromisoformat(state["observed_at"])).total_seconds() <= 10)
                entry = {
                    "state": state, "fresh": fresh,
                    "active_task": encode(repo.active_task(identifier)) if identifier != "bulb-1" else None,
                    "latest_task": encode(repo.latest_task(identifier)) if identifier != "bulb-1" else None,
                }
                if identifier == "bulb-1":
                    entry["automation"] = lighting_summary(repo, real)
                devices[identifier] = entry
            devices["curtain-1"] = {
                "state": curtain.get_status(repo=repo),
                "fresh": True,
                "active_task": None,
                "latest_task": None,
                "schedules": curtain.list_schedules_data(repo),
            }
            devices["tv-1"] = {
                "state": tv.get_status(repo=repo),
                "fresh": True,
                "active_task": None,
                "latest_task": None,
            }
            return {"server_time": real.isoformat(), "clock": {"now": clock.now(real).isoformat(),
                    "speed": clock.speed, "clock_id": clock.clock_id}, "devices": devices,
                    "preferences": preferences(repo), "program": encode(hems.program())}

    @app.post("/api/requests", status_code=202)
    def submit(body: WebRequest):
        payload = body.payload.model_dump(mode="json", exclude_none=True)
        # Null quiet periods are meaningful; other absent fields are omitted.
        if isinstance(body, WasherRequest):
            payload["constraints"] = body.payload.constraints.model_dump(mode="json")
        with repo.transaction():
            previous = repo.request(body.request_id)
            if previous:
                if previous["kind"] != body.kind or json.loads(previous["payload"]) != payload:
                    raise HTTPException(409, "請求識別碼已用於其他內容")
            else:
                task_id = payload.get("task_id")
                if task_id:
                    try:
                        task = repo.task(task_id)
                    except ValueError as error:
                        raise HTTPException(404, str(error)) from error
                    if task.version != payload["expected_version"]:
                        raise HTTPException(409, "任務版本已改變，請刷新後重試")
                    if isinstance(body, (ACRequest, WasherRequest)) and task.device_id != payload["device_id"]:
                        raise HTTPException(409, "設備與任務不符")
                    if body.kind == "approve" and task.device_id == "washer-1" and "max_cost" not in payload:
                        raise HTTPException(422, "洗衣計畫需要核准費用上限")
                elif isinstance(body, (ACRequest, WasherRequest, BulbRequest)) and repo.active_task(payload["device_id"]):
                    raise HTTPException(409, "此設備已有未完成任務，請修改目前計畫")
                repo.submit(body.request_id, body.kind, payload)
        return {"request_id": body.request_id}

    @app.get("/api/assistant")
    def assistant_state():
        stored_messages = repo.assistant_messages("home")
        messages = []
        for message in stored_messages:
            if message["role"] != "assistant" or not message["id"].startswith("assistant:"):
                messages.append(message)
                continue
            run_id = message["id"].split(":", 1)[1]
            cards = [row["payload"] for row in repo.assistant_events(run_id, 0) if row["payload"].get("kind") in {"task_card", "light_card"}]
            if cards:
                message["toolCalls"] = [{"id": card["tool_call_id"], "type": "function", "function": {
                    "name": "render_light_card" if card["kind"] == "light_card" else "render_task_card",
                    "arguments": json.dumps({"request_id": card["request_id"]} if card["kind"] == "light_card" else {"task_id": card["task_id"], "version": card["version"], "device_id": card["device_id"]}, separators=(",", ":"))}}
                    for card in cards]
            messages.append(message)
            messages.extend({"id": f"tool:{card['tool_call_id']}", "role": "tool", "toolCallId": card["tool_call_id"],
                             "content": "燈泡動作狀態以設備回報為準。" if card["kind"] == "light_card" else "此為資料庫中已保存的設備計畫；執行仍須依核准卡片操作。"} for card in cards)
        with repo.transaction() as db:
            runs = [dict(row) for row in db.execute("SELECT id,message_id,root_id,status,response,created,updated FROM assistant_runs ORDER BY created DESC LIMIT 100")]
        active = next((r for r in runs if r["status"] in {"pending", "running"}), None)
        return {"thread_id": "home", "messages": messages, "runs": runs,
                "tasks": [encode(task) for task in repo.recent_tasks()],
                "active": bool(active), "active_run_id": active["id"] if active else None}

    @app.post("/api/assistant/ag-ui")
    async def assistant_agui(body: dict):
        from smart_home_energy.assistant_service import queue_run, event_stream
        try:
            run_id, _created = queue_run(repo, body)
        except RuntimeError as error:
            raise HTTPException(409, str(error)) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        last = body.get("lastEventId") or body.get("last_event_id") or 0
        try:
            last = max(0, int(last))
        except (TypeError, ValueError):
            last = 0
        return StreamingResponse(event_stream(repo, run_id, last), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})

    @app.get("/api/assistant/runs/{run_id}/events")
    def assistant_events(run_id: str, after: int = 0):
        from smart_home_energy.assistant_service import event_stream
        if not repo.assistant_run(run_id):
            raise HTTPException(404, "找不到對話工作")
        return StreamingResponse(event_stream(repo, run_id, after), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})

    @app.get("/api/tasks/{task_id}")
    def task_snapshot(task_id: str):
        try:
            return encode(repo.task(task_id))
        except ValueError as error:
            raise HTTPException(404, str(error)) from error

    @app.get("/api/energy")
    def energy():
        from smart_home_energy.tariffs import household_estimate
        with repo.transaction() as db:
            samples = [{"device_id": row[0], **json.loads(row[1])} for row in db.execute("SELECT device_id,data FROM samples ORDER BY id DESC LIMIT 900")]
        return {"incremental_cost": household_estimate(repo, hems.tariff()), "tasks": repo.recent_tasks(), "samples": list(reversed(samples))}

    @app.get("/api/lighting")
    def lighting():
        from smart_home_energy.lighting_runner import lighting_snapshot
        with repo.transaction():
            return lighting_snapshot(repo, utcnow())

    @app.get("/api/settings")
    def settings():
        from smart_home_energy.tariffs import source_snapshot, PLANS
        config = repo.meta("tariff_settings", {"plan": "simulated", "baseline_kwh": {}, "revision": 0})
        source = repo.meta("tariff_source")
        if source:
            source_info = {key: source.get(key) for key in ("source_url", "sha256", "retrieved_at", "effective_from")}
        else:
            bundled = source_snapshot()
            source_info = {key: bundled.get(key) for key in ("source_url", "sha256", "retrieved_at", "effective_from")}
        return {"preferences": preferences(repo), "tariff": config, "tariff_plans": {"simulated": "模擬費率", **PLANS},
                "tariff_source": source_info, "clock": encode(repo.clock()), "active_tasks": [encode(t) for t in repo.active_tasks()]}

    @app.get("/api/diagnostics")
    def diagnostics():
        import os
        import httpx
        try:
            runtime_response = httpx.get(f"{runtime_url}/api/copilotkit/info", timeout=1.5)
            runtime_status = "online" if runtime_response.is_success else f"HTTP {runtime_response.status_code}"
        except httpx.RequestError:
            runtime_status = "offline"
        heartbeat = repo.meta("controller_heartbeat")
        controller_status = "offline"
        if heartbeat:
            try:
                age = (utcnow() - datetime.fromisoformat(heartbeat)).total_seconds()
                controller_status = "online" if -2 <= age <= 10 else "stale"
            except (TypeError, ValueError):
                controller_status = "unknown"
        return {"services": {"api": "online", "copilot_runtime": runtime_status,
                "controller": controller_status, "mcp_server": "online (MCP SDK)",
                "transport": os.getenv("HEMS_TRANSPORT", "mqtt")},
                "mcp_catalog": mcp_server.catalog(),
                "clock": encode(repo.clock()), "requests": repo.records("requests", 50), "agent_runs": repo.records("agent_runs", 50),
                "tool_logs": repo.records("tool_logs", 50), "audit": repo.records("audit", 50), "commands": repo.records("commands", 50)}

    @app.api_route("/api/copilotkit", methods=["GET", "POST", "OPTIONS"])
    @app.api_route("/api/copilotkit/{path:path}", methods=["GET", "POST", "OPTIONS"])
    async def copilot_runtime_proxy(request: Request, path: str = ""):
        """Keep the Vite SPA same-origin while proxying Copilot Runtime and SSE."""
        import httpx
        query = request.url.query
        url = f"{runtime_url}/api/copilotkit/{path}" + (f"?{query}" if query else "")
        headers = {key: value for key, value in request.headers.items() if key.lower() in {"accept", "content-type", "last-event-id", "authorization"}}
        client = httpx.AsyncClient(timeout=httpx.Timeout(connect=2, read=None, write=10, pool=2))
        try:
            upstream = await client.send(client.build_request(request.method, url, headers=headers, content=await request.body()), stream=True)
        except httpx.RequestError as error:
            await client.aclose()
            raise HTTPException(503, "Copilot Runtime 目前無法連線") from error
        async def body_stream():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()
        response_headers = {key: value for key, value in upstream.headers.items() if key.lower() in {"cache-control", "content-type", "x-accel-buffering", "access-control-allow-origin"}}
        return StreamingResponse(body_stream(), status_code=upstream.status_code, headers=response_headers, media_type=upstream.headers.get("content-type"))

    @app.get("/api/requests/{request_id}")
    def request_status(request_id: str):
        row = repo.request(request_id)
        if row is None:
            raise HTTPException(404, "找不到請求")
        result = {"request_id": row["id"], "kind": row["kind"], "status": row["status"], "result": row["result"]}
        if row["kind"] == "set_bulb":
            result["action"] = repo.bulb_action(request_id)
            payload = json.loads(row["payload"])
            result["target"] = {key: payload[key] for key in ("power", "brightness_percent", "kelvin") if payload.get(key) is not None}
        return result

    from smart_home_energy.curtain import create_curtain_router
    from smart_home_energy.tv import create_tv_router
    app.include_router(create_curtain_router(repo))
    app.include_router(create_tv_router(repo))
    app.include_router(create_mcp_router(repo, hems, mcp_server))

    @app.get("/api/{unknown:path}", include_in_schema=False)
    def unknown_api(unknown: str):
        raise HTTPException(404, "找不到 API")

    static_directory = static_directory or Path(__file__).resolve().parents[2] / "web" / "dist"
    if (static_directory / "index.html").is_file():
        app.mount("/", StaticFiles(directory=static_directory, html=True), name="living-room")
    else:
        @app.get("/", include_in_schema=False)
        def missing_build():
            return JSONResponse({"detail": "請先執行 npm.cmd run build --prefix web，或使用 Vite 開發入口。"}, status_code=503)
    return app


def main():
    import argparse
    import uvicorn
    parser = argparse.ArgumentParser(description="智慧客廳本機入口")
    parser.add_argument("--host", type=str, default=os.getenv("HEMS_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8502")))
    args = parser.parse_args()
    uvicorn.run(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
