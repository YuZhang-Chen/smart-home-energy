"""Event-driven Main Agent with JSON actions and a constrained tool boundary."""

import asyncio
import inspect
import json
import os
import time
from urllib.parse import urlparse
from uuid import uuid4

from smart_home_energy.config import HemsSettings
from smart_home_energy.domain import (
    Plan, TaskConstraints, decode, encode, find_schedule, validate_plan, utcnow, number,
)
from smart_home_energy.repository import Repository, AgentRun
from smart_home_energy.washer import DeviceState
from smart_home_energy.control import fresh

TOOL_CONTRACTS = {
    "get_home_state": {},
    "get_device_state": {},
    "get_task_state": {},
    "get_tariff": {},
    "find_schedule": {"constraints": "TaskConstraints|null", "max_cost": "number|null"},
    "validate_plan": {"plan": "Plan", "constraints": "TaskConstraints|null"},
    "propose_plan": {"plan": "Plan", "constraints": "TaskConstraints", "expected_version": "integer", "reason": "string"},
    "reschedule_task": {"plan": "Plan", "constraints": "TaskConstraints", "expected_version": "integer", "reason": "string"},
    "verify_execution": {},
}


class AgentTools:
    def __init__(self, repo: Repository, task_id: str):
        self.repo, self.task_id = repo, task_id

    def task(self):
        return self.repo.task(self.task_id)

    def get_home_state(self):
        return {"clock": encode(self.repo.clock()), "logical_now": encode(self.repo.clock().now()),
                "real_now": encode(utcnow()), "timezone": "Asia/Taipei",
                "controller_heartbeat": self.repo.meta("controller_heartbeat"),
                "task": encode(self.task())}

    def get_device_state(self):
        value = self.repo.device()
        state = decode(DeviceState, value) if value else None
        return {"state": value, "fresh": fresh(state, utcnow())}

    def get_task_state(self):
        return encode(self.task())

    def get_tariff(self):
        return {"simulated": self.task().tariff.official is None, "unit": "NT$/kWh", "timezone": "Asia/Taipei", "tariff": encode(self.task().tariff)}

    def find_schedule(self, constraints: dict | None = None, max_cost: float | None = None):
        task = self.task()
        c = decode(TaskConstraints, constraints) if constraints is not None else task.constraints
        if c is None:
            raise ValueError("缺少最早開始、最晚完成與安靜時段，請補問使用者")
        if max_cost is not None:
            number(max_cost, "費用上限")
        clock = self.repo.clock()
        return encode(find_schedule(c, task.program, task.tariff, clock.now(), clock.clock_id, max_cost))

    def validate_plan(self, plan: dict, constraints: dict | None = None):
        task = self.task()
        c = decode(TaskConstraints, constraints) if constraints is not None else task.constraints
        if c is None:
            raise ValueError("缺少任務限制")
        p = decode(Plan, plan)
        if p.program != task.program or p.tariff != task.tariff:
            raise ValueError("必須使用任務行程與費率快照")
        clock = self.repo.clock()
        validate_plan(p, c, clock.now(), clock.clock_id)
        within = False
        if task.approval:
            try:
                validate_plan(p, c, clock.now(), clock.clock_id, task.approval)
                within = True
            except ValueError:
                pass
        return {"valid": True, "within_approval": within}

    def propose_plan(self, plan: dict, constraints: dict, expected_version: int, reason: str = ""):
        return encode(self.repo.propose(self.task_id, expected_version, decode(TaskConstraints, constraints),
                                        decode(Plan, plan), False, reason))

    def reschedule_task(self, plan: dict, constraints: dict, expected_version: int, reason: str = ""):
        return encode(self.repo.propose(self.task_id, expected_version, decode(TaskConstraints, constraints),
                                        decode(Plan, plan), True, reason))

    def verify_execution(self):
        task = self.task()
        device = self.get_device_state()
        value = device["state"]
        return {"task_status": task.status, **device,
                "same_run": bool(value and task.run_id and value["run_id"] == task.run_id and value["task_id"] == task.task_id),
                "confirmed_completed": bool(value and device["fresh"] and task.run_id and
                    value["run_id"] == task.run_id and value["task_id"] == task.task_id and value["status"] == "completed")}

    def call(self, name: str, arguments: dict):
        if name not in TOOL_CONTRACTS or not isinstance(arguments, dict):
            raise ValueError("未知工具或參數格式；Agent 不可核准、取消、中止或執行 shell")
        method = getattr(self, name)
        try:
            inspect.signature(method).bind(**arguments)
        except TypeError as error:
            raise ValueError("工具參數不符合契約") from error
        if "expected_version" in arguments and type(arguments["expected_version"]) is not int:
            raise ValueError("expected_version 必須為整數")
        if "reason" in arguments and not isinstance(arguments["reason"], str):
            raise ValueError("reason 必須為文字")
        # Every mutation is scoped to this event's task and an explicit version.
        return method(**arguments)


class ProviderUnavailable(RuntimeError):
    pass


class ProviderConfigurationError(RuntimeError):
    pass


class SDKProvider:
    def __init__(self, name: str, base_url: str, api_key: str, model: str):
        self.name, self.base_url, self.api_key, self.model = name, base_url, api_key, model

    async def complete(self, messages: list[dict], timeout: float) -> str:
        try:
            from openai import AsyncOpenAI, APIConnectionError, APITimeoutError, APIStatusError
        except ImportError:
            raise ProviderConfigurationError("尚未安裝 OpenAI SDK，請執行 pip install -e .") from None
        try:
            async with AsyncOpenAI(base_url=self.base_url, api_key=self.api_key, max_retries=0, timeout=timeout) as client:
                response = await client.chat.completions.create(model=self.model, messages=messages)
                if not response.choices or not response.choices[0].message.content:
                    raise ProviderUnavailable(f"{self.name} 回傳空白內容")
                return response.choices[0].message.content
        except (APIConnectionError, APITimeoutError):
            raise ProviderUnavailable(f"{self.name} 連線失敗或逾時") from None
        except APIStatusError as error:
            # Never persist error bodies/headers, which may include credentials.
            if error.status_code in {408, 409, 429} or error.status_code >= 500:
                raise ProviderUnavailable(f"{self.name} HTTP {error.status_code}") from None
            raise ProviderConfigurationError(f"{self.name} HTTP {error.status_code}：請檢查設定／授權") from None


def configured_providers() -> list[SDKProvider]:
    providers = []
    for name, keys in (
        ("vllm", ("VLLM_BASE_URL", "VLLM_API_KEY", "VLLM_MODEL")),
        ("azure", ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_DEPLOYMENT")),
    ):
        url, key, model = (os.getenv(k, "").strip() for k in keys)
        if not any((url, key, model)):
            continue
        if not url or not model or (name == "azure" and not key):
            raise ProviderConfigurationError(f"{name} 設定不完整；請填妥連線欄位或全部留白")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ProviderConfigurationError(f"{name} URL 格式不合法")
        if name == "azure":
            if parsed.scheme != "https":
                raise ProviderConfigurationError("Azure Endpoint 必須使用 HTTPS")
            url = url.rstrip("/")
            if not url.endswith("/openai/v1"):
                url += "/openai/v1"
        elif not url.rstrip("/").endswith("/v1"):
            raise ProviderConfigurationError("VLLM_BASE_URL 必須包含 /v1")
        providers.append(SDKProvider(name, url.rstrip("/") + "/", key or "not-required", model))
    return providers


SYSTEM = """你是智慧家庭 Main Agent。使用繁體中文與使用者溝通。每回合只回傳一個 JSON 物件。
工具動作格式：{"tool":"工具名稱","arguments":{},"summary":"簡短決策摘要"}
完成格式：{"finish":"給使用者的結果"}。需補問時：{"ask":"具體問題"}。
不用展示私有推理全文。工具結果是事實依據，不能宣稱命令受理就是完成。
每次先查 get_home_state。資訊不足先補問，不自行推測日期、期限或安靜時段。
日期輸入與顯示使用 Asia/Taipei，工具日期用含時區 ISO 格式；分鐘用當地午夜後分鐘數。
TaskConstraints={earliest:ISO,deadline:ISO,quiet_start:0..1439|null,quiet_end:0..1439|null,device_id:"washer-1"}。
請依序查狀態／電價，find_schedule，validate_plan，再 propose_plan；有 approval 的未啟動任務使用 reschedule_task。
Plan 必須使用 find_schedule 傳回的完整物件，不得自行計算或修改費用與行程。
expected_version 必須來自任務狀態。補充需求以事件 text 為準，結合目前任務條件。
不能自行核准、啟動、中止或解除故障，不能操作其他任務。運轉中只查核並回報。
改排應先使用原核准的 constraints 與 max_cost 搜尋；無法滿足時說明衝突，若提出新範圍須重新核准。
沒有可行方案時不可默默放寬限制。模擬電價／預估費用必須清楚標示。
工具與參數：""" + json.dumps(TOOL_CONTRACTS, ensure_ascii=False)


async def run_agent(repo: Repository, event, hems: HemsSettings, providers=None, monotonic=time.monotonic) -> AgentRun:
    started = monotonic()
    run_id = uuid4().hex
    count, provider_name, index = 0, "none", 0
    status, message = "failed", "模型不可用，請使用表單排程"
    tools = AgentTools(repo, event.task_id)
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": json.dumps(encode({"event": event, "task": tools.get_task_state()}), ensure_ascii=False)}]
    try:
        providers = configured_providers() if providers is None else providers
        if not providers:
            raise ProviderUnavailable("vLLM／Azure OpenAI 尚未設定，請使用表單排程")
        while count < hems.agent_requests:
            remaining = hems.agent_seconds - (monotonic() - started)
            if remaining <= 0:
                raise ProviderUnavailable("Agent 已達總時間上限")
            provider = providers[index]
            provider_name = provider.name
            count += 1
            timeout = min(hems.request_seconds, remaining)
            try:
                reply = await asyncio.wait_for(provider.complete(messages, timeout), timeout=timeout)
            except (ProviderUnavailable, TimeoutError) as error:
                safe_reason = str(error) if isinstance(error, ProviderUnavailable) else "模型單次請求逾時"
                repo.log_tool(run_id, {"provider": provider_name, "attempt": count, "error": safe_reason})
                if index + 1 < len(providers):
                    index += 1
                    continue
                raise ProviderUnavailable(safe_reason) from None
            if monotonic() - started >= hems.agent_seconds:
                raise ProviderUnavailable("Agent 已達總時間上限，未執行回覆中的工具")
            try:
                if len(reply) > 65536:
                    raise ValueError("模型回覆過大")
                action = json.loads(reply)
                if not isinstance(action, dict):
                    raise ValueError("動作需為 JSON 物件")
                if set(action) in ({"finish"}, {"ask"}):
                    message = action.get("finish", action.get("ask"))
                    if not isinstance(message, str) or not message.strip():
                        raise ValueError("完成／補問內容不可空白")
                    status = "needs_details" if "ask" in action else "completed"
                    repo.log_tool(run_id, {"provider": provider_name, "action": action})
                    break
                if set(action) != {"tool", "arguments", "summary"} or not isinstance(action["summary"], str) or not isinstance(action["tool"], str):
                    raise ValueError("請使用工具名稱、參數與簡短摘要的 JSON 動作")
                result = tools.call(action["tool"], action["arguments"])
                repo.log_tool(run_id, {"provider": provider_name, "action": action, "result": result})
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": json.dumps({"tool_result": result}, ensure_ascii=False)})
            except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as error:
                result = {"error": str(error)}
                repo.log_tool(run_id, {"provider": provider_name, "result": result})
                messages.append({"role": "user", "content": json.dumps(result, ensure_ascii=False)})
        else:
            message = "Agent 已達模型請求次數上限，請使用表單排程"
    except (ProviderUnavailable, ProviderConfigurationError) as error:
        message = str(error)
    finally:
        run = AgentRun(run_id, event.event_id, status, provider_name, count, monotonic() - started, message)
        repo.record_run(run)
        repo.finish_event(event.event_id)
    return run


def agent_worker(repo: Repository, hems: HemsSettings, stopped):
    while not stopped.is_set():
        event = repo.claim_event()
        if event:
            from smart_home_energy.multi_agent import run_household
            asyncio.run(run_household(repo, event, hems))
        else:
            stopped.wait(.5)
