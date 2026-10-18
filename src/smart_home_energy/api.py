"""Local HTTP facade. Durable requests are executed only by the resident controller."""
from datetime import datetime
import json
import os
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator

from smart_home_energy.ac_domain import ACConstraints
from smart_home_energy.assistant_defaults import preferences
from smart_home_energy.config import HemsSettings
from smart_home_energy.domain import TaskConstraints, encode, utcnow
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


class ConfirmedRequest(RequestInput):
    kind: Literal["abort", "reset"]
    payload: ConfirmedPayload


WebRequest = Annotated[ACRequest | WasherRequest | ApprovalRequest | TaskRequest | ConfirmedRequest,
                       Body(discriminator="kind")]


def create_app(repo: Repository | None = None, hems: HemsSettings | None = None,
               static_directory: Path | None = None) -> FastAPI:
    hems = hems or HemsSettings.from_env()
    repo = repo or Repository(hems.database)
    dashboard_port = int(os.getenv("HEMS_DASHBOARD_PORT", "8501"))
    if not 1 <= dashboard_port <= 65535:
        raise ValueError("家庭助理連接埠必須介於 1–65535")
    app = FastAPI(title="智慧客廳本機 API", docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)

    @app.get("/api/home")
    def home():
        with repo.transaction():
            clock, real = repo.clock(), utcnow()
            devices = {}
            for identifier in ("ac-1", "washer-1"):
                state = repo.device(identifier)
                fresh = bool(state and -2 <= (real - datetime.fromisoformat(state["observed_at"])).total_seconds() <= 10)
                devices[identifier] = {
                    "state": state, "fresh": fresh,
                    "active_task": encode(repo.active_task(identifier)),
                    "latest_task": encode(repo.latest_task(identifier)),
                }
            return {"server_time": real.isoformat(), "assistant_url": f"http://127.0.0.1:{dashboard_port}", "clock": {"now": clock.now(real).isoformat(),
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
                elif repo.active_task(payload["device_id"]):
                    raise HTTPException(409, "此設備已有未完成任務，請修改目前計畫")
                repo.submit(body.request_id, body.kind, payload)
        return {"request_id": body.request_id}

    @app.get("/api/requests/{request_id}")
    def request_status(request_id: str):
        row = repo.request(request_id)
        if row is None:
            raise HTTPException(404, "找不到請求")
        return {"request_id": row["id"], "kind": row["kind"], "status": row["status"], "result": row["result"]}

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
    parser.add_argument("--port", type=int, default=8502)
    args = parser.parse_args()
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
