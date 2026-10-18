"""Depth-one model delegation with a shared end-to-end budget and durable IDs."""
import asyncio
import inspect
import json
import time
from datetime import datetime, timedelta
from dataclasses import replace
from hashlib import sha256
from uuid import uuid4

from smart_home_energy.agent import AgentTools, SYSTEM, configured_providers, ProviderUnavailable, ProviderConfigurationError
from smart_home_energy.ac_domain import ACConstraints, ACPlan, make_ac_plan, validate_ac
from smart_home_energy.domain import decode, encode, utcnow, TaskConstraints, find_schedule, number
from smart_home_energy.assistant_defaults import preferences
from smart_home_energy.repository import AgentRun
from smart_home_energy.tariffs import selected_tariff


class SharedBudget:
    def __init__(self, hems, providers, monotonic=time.monotonic):
        self.hems, self.providers, self.monotonic = hems, providers, monotonic
        self.started, self.count, self.index = monotonic(), 0, 0

    async def complete(self, messages, repo, run_id):
        while self.providers:
            remaining = self.hems.agent_seconds - (self.monotonic() - self.started)
            if self.count >= self.hems.agent_requests or remaining <= 0:
                raise ProviderUnavailable("家庭工作已達共用模型預算，請使用各設備表單")
            provider = self.providers[self.index]
            self.count += 1
            timeout = min(self.hems.request_seconds, remaining)
            try:
                reply = await asyncio.wait_for(provider.complete(messages, timeout), timeout=timeout)
                if self.monotonic() - self.started >= self.hems.agent_seconds:
                    raise ProviderUnavailable("家庭工作已達總時間上限，未執行回覆中的工具")
                return reply
            except (ProviderUnavailable, TimeoutError) as error:
                repo.log_tool(run_id, {"provider": provider.name, "attempt": self.count,
                                       "error": str(error) if isinstance(error, ProviderUnavailable) else "模型逾時"})
                if self.index + 1 == len(self.providers):
                    raise ProviderUnavailable("模型服務不可用，請使用表單") from None
                self.index += 1
        raise ProviderUnavailable("未設定模型服務，請使用表單")


class DeviceTools:
    def __init__(self, repo, task_id):
        self.repo, self.task_id = repo, task_id
        self.version = repo.task(task_id).version
        self.washer = AgentTools(repo, task_id)
        self.candidates = {}

    def get_device_context(self):
        task = self.repo.task(self.task_id)
        state = self.repo.device(task.device_id)
        return {"task": encode(task), "device": state, "fresh": bool(state and -2 <= (utcnow() - datetime.fromisoformat(state["observed_at"])).total_seconds() <= 10),
                "logical_now": encode(self.repo.clock().now()), "timezone": "Asia/Taipei", "tariff": encode(task.tariff),
                "preferences": preferences(self.repo)}

    def prepare_default_plan(self, duration_minutes=None, target_c=None):
        prefs = preferences(self.repo)
        duration = prefs["duration_minutes"] if duration_minutes is None else duration_minutes
        target = prefs["target_c"] if target_c is None else target_c
        number(duration, "冷氣運轉分鐘", 15)
        if duration > 1440:
            raise ValueError("冷氣運轉時間不能超過一天")
        now = self.repo.clock().now()
        c = ACConstraints(now, now + timedelta(minutes=duration), target, min(prefs["minimum_c"], target),
                          max(prefs["maximum_c"], target))
        plan = self.find_plan(encode(c))
        plan["start_on_approval"] = True
        self.candidates[plan["plan_id"]] = plan
        return plan

    def find_default_schedule(self, immediate=False):
        if type(immediate) is not bool:
            raise ValueError("立即洗衣須為布林值")
        task, clock, prefs = self.repo.task(self.task_id), self.repo.clock(), preferences(self.repo)
        now = clock.now()
        c = TaskConstraints(now + timedelta(seconds=max(120, 120 * clock.speed)), now + timedelta(hours=prefs["washer_hours"]),
                            prefs["quiet_start"], prefs["quiet_end"])
        plan = find_schedule(c, task.program, task.tariff, now, clock.clock_id, prefer_earliest=immediate)
        return {"constraints": encode(c), "plan": encode(plan)}

    def get_task_state(self):
        return encode(self.repo.task(self.task_id))

    def find_plan(self, constraints):
        task = self.repo.task(self.task_id)
        c = decode(ACConstraints, constraints)
        context = self.get_device_context()
        if not context["fresh"]:
            raise ValueError("需要新鮮冷氣室溫才能產生提案")
        plan = encode(make_ac_plan(c, context["device"]["temperature_c"], task.tariff, self.repo.clock().clock_id))
        self.candidates[plan["plan_id"]] = plan
        return plan

    def validate_plan(self, plan):
        p = decode(ACPlan, plan)
        if p.tariff != self.repo.task(self.task_id).tariff:
            raise ValueError("必須使用任務費率快照")
        validate_ac(p, self.repo.clock().now(), self.repo.clock().clock_id)
        return {"valid": True}

    def propose_plan(self, plan, expected_version):
        if not isinstance(plan, dict) or self.candidates.get(plan.get("plan_id")) != plan:
            raise ValueError("請原樣使用 find_plan 回傳的計畫，不得修改室溫、費用或行程")
        return encode(self.repo.propose_ac(self.task_id, expected_version, decode(ACPlan, plan)))

    def call(self, name, arguments):
        task = self.repo.task(self.task_id)
        if task.version != self.version:
            raise ProviderUnavailable("使用者或控制服務已更新任務；舊子工作停止修改")
        if not isinstance(arguments, dict):
            raise ValueError("工具參數須為物件")
        if name == "get_device_context":
            inspect.signature(self.get_device_context).bind(**arguments)
            return self.get_device_context()
        if task.kind == "washer":
            if name == "find_default_schedule":
                inspect.signature(self.find_default_schedule).bind(**arguments)
                return self.find_default_schedule(**arguments)
            return self.washer.call(name, arguments)
        if name not in {"get_task_state", "find_plan", "prepare_default_plan", "validate_plan", "propose_plan"}:
            raise ValueError("冷氣工具不允許核准、啟動、中止或再次委派")
        if name == "propose_plan" and type(arguments.get("expected_version")) is not int:
            raise ValueError("expected_version 必須為整數")
        method = getattr(self, name)
        inspect.signature(method).bind(**arguments)
        return method(**arguments)


AC_CONTRACTS = {"get_device_context": {}, "get_task_state": {}, "find_plan": {"constraints": "ACConstraints"},
                "prepare_default_plan": {"duration_minutes": "number|null", "target_c": "number|null"},
                "validate_plan": {"plan": "ACPlan"}, "propose_plan": {"plan": "ACPlan", "expected_version": "integer"}}
AC_SYSTEM = """你是 AC Agent，以繁體中文工作。每回合只回一個 JSON 物件。
工具格式 {"tool":"name","arguments":{},"summary":"簡短摘要"}；補問 {"ask":"問題"}；查核結束 {"finish":"結果"}。
先查 get_device_context。日期採 Asia/Taipei，工具使用含時區 ISO。
ACConstraints={start:ISO,end:ISO,target_c:27,minimum_c:26,maximum_c:28,ambient_c:32,warming_seconds:1800,device_id:"ac-1"}。
使用者已同意使用常用偏好。只說開冷氣、很熱、馬上開或沒指定開始時間時，呼叫 prepare_default_plan，省略參數即採偏好（預設27°C、一小時），確認後才開始計時，不補問起訖。明確指定運轉分鐘或目標溫度則傳入該參數。
明確預約日期時使用 find_plan；缺少結束時間可採偏好的一小時。禁止改寫使用者明確的日期或時段。到開始時間才開機，不預冷，費用僅估算，舒適優先。
依序 find_plan、validate_plan、propose_plan；計畫必須原樣使用工具結果，expected_version 使用任務版本。
不能核准、啟動、停止、解除故障或委派。已運轉任務只能查詢及回報，不得修改。
工具：""" + json.dumps(AC_CONTRACTS, ensure_ascii=False)


async def device_session(repo, event, task, budget, parent_id):
    run_id, started, before = uuid4().hex, budget.monotonic(), budget.count
    role = "Washer Agent" if task.kind == "washer" else "AC Agent"
    system = (SYSTEM.replace("智慧家庭 Main Agent", "Washer Agent").replace("資訊不足先補問，不自行推測日期、期限或安靜時段。", "使用已同意的預設偏好，不重複補問未指定的期限或安靜時段。") + """
可使用 get_device_context 一次取得設備、任務、偏好、費率與時間。已有可信資料不必重複查詢。
額外工具 find_default_schedule={immediate:boolean}：沒指定日期／期限時使用，預設24小時內完成、避開23:00–07:00、選便宜時段。明說現在／馬上洗時 immediate=true 選最早合法時段。
它回傳 {constraints,plan}，將這兩個物件原樣交給 propose_plan；使用者明確指定的條件仍用 find_schedule，不得以預設覆蓋。成功提出計畫即結束子工作。""") if task.kind == "washer" else AC_SYSTEM
    tools = DeviceTools(repo, task.task_id)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(encode({"text": event.payload.get("text", ""), "task": task, "event": event.payload}), ensure_ascii=False)}]
    status, message = "failed", "子工作未完成，請使用表單"
    try:
        while True:
            reply = await budget.complete(messages, repo, run_id)
            try:
                if len(reply) > 65536:
                    raise ValueError("模型回覆過大")
                action = json.loads(reply)
                if not isinstance(action, dict):
                    raise ValueError("回覆須為 JSON 物件")
                if set(action) in ({"ask"}, {"finish"}):
                    message = action.get("ask", action.get("finish"))
                    if not isinstance(message, str) or not message.strip():
                        raise ValueError("結果不可為空白")
                    status = "needs_details" if "ask" in action else "completed"
                    break
                if set(action) != {"tool", "arguments", "summary"} or not isinstance(action["summary"], str):
                    raise ValueError("工具動作格式不符")
                result = tools.call(action["tool"], action["arguments"])
                repo.log_tool(run_id, {"role": role, "provider": budget.providers[budget.index].name, "device_id": task.device_id, "action": action, "result": result})
                messages.extend([{"role": "assistant", "content": reply}, {"role": "user", "content": json.dumps({"tool_result": result}, ensure_ascii=False)}])
                if action["tool"] in {"propose_plan", "reschedule_task"}:
                    status, message = "completed", "計畫已提出，等待使用者核准" if result["status"] == "pending" else "已在核准範圍內改排"
                    break
            except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as error:
                repo.log_tool(run_id, {"error": str(error)})
                messages.append({"role": "user", "content": json.dumps({"error": str(error)}, ensure_ascii=False)})
    except (ProviderUnavailable, ProviderConfigurationError) as error:
        message = str(error)
    provider = budget.providers[budget.index].name if budget.providers else "none"
    run = AgentRun(run_id, event.event_id, status, provider, budget.count - before, budget.monotonic() - started, message, parent_id, task.device_id, role)
    repo.record_run(run)
    return encode(run)


async def run_household(repo, event, hems, providers=None, monotonic=time.monotonic):
    run_id, started = uuid4().hex, monotonic()
    results, status, message = [], "failed", "家庭工作未完成，請使用各設備表單"
    budget = SharedBudget(hems, [], monotonic)
    root_id = event.payload.get("root_id", event.event_id)
    try:
        budget.providers = configured_providers() if providers is None else providers
        if event.task_id:
            results.append(await device_session(repo, event, repo.task(event.task_id), budget, run_id))
            status, message = results[0]["status"], results[0]["message"]
        else:
            system = """你是智慧家庭 Main Agent，使用繁體中文，每次只回一個 JSON 物件。
工具 {"tool":"get_home_state","arguments":{},"summary":"摘要"}。
批次委派 {"tool":"delegate_tasks","arguments":{"requests":[{"device_id":"washer-1 或 ac-1","text":"完整原始需求中與該設備相關的條件","task_id":"僅修改現有任務時填入","expected_version":1}]},"summary":"摘要"}。
同一需求可委派兩台設備，各自核准、各自失敗。使用者已同意預設：開冷氣27°C、一小時、確認後開始；洗衣24小時內完成、避開23:00–07:00並選較便宜時段。不因未填時間而補問，將完整需求交給子 Agent 套用偏好。
這些數字是初始偏好，以 get_home_state 的 preferences 為準。委派 text 保留使用者原文及明確條件，不把未說出的初始數字當成使用者指定值。
若設備已有待補充、待核准或未啟動任務，修改同設備必須傳 task_id 與 expected_version，不新增第二個任務；未提及的設備保持原樣。缺正式費率基準等必要資訊才補問，向使用者說明需要哪個月份、多少度，不要求填JSON。
task_id 只能取自 get_home_state.tasks 的未完成任務；設備回報可能仍帶有上次已結束的行程，不能當成目前任務。tasks 沒有該設備時，委派請省略 task_id 和 expected_version。
先 get_home_state 再 delegate_tasks。每個設備最多一個子工作，不核准、不執行設備、不自行計算費用。
僅查詢可 {"finish":"結果"}；資訊不足可 {"ask":"問題"}。"""
            messages = [{"role": "system", "content": system}, {"role": "user", "content": event.payload["text"]}]
            while True:
                reply = await budget.complete(messages, repo, run_id)
                reply_provider = budget.providers[budget.index].name
                try:
                    if len(reply) > 65536:
                        raise ValueError("模型回覆過大")
                    action = json.loads(reply)
                    if not isinstance(action, dict):
                        raise ValueError("回覆須為 JSON 物件")
                    if set(action) in ({"ask"}, {"finish"}):
                        message = action.get("ask", action.get("finish"))
                        if not isinstance(message, str) or not message.strip():
                            raise ValueError("結果不可為空白")
                        status = "needs_details" if "ask" in action else "completed"
                        break
                    if set(action) != {"tool", "arguments", "summary"} or not isinstance(action["arguments"], dict):
                        raise ValueError("工具動作格式不符")
                    if action["tool"] == "get_home_state" and action["arguments"] == {}:
                        active = repo.active_tasks()
                        devices = {device: repo.device(device) for device in ("washer-1", "ac-1")}
                        for device, state in devices.items():
                            if state and not any(task.device_id == device for task in active):
                                devices[device] = {**state, "task_id": None, "run_id": None, "plan_version": 0}
                        result = {"tasks": encode(active), "clock": encode(repo.clock()), "logical_now": encode(repo.clock().now()),
                                  "devices": devices, "timezone": "Asia/Taipei",
                                  "preferences": preferences(repo)}
                    elif action["tool"] == "delegate_tasks" and set(action["arguments"]) == {"requests"}:
                        requests = action["arguments"]["requests"]
                        if not isinstance(requests, list) or not 1 <= len(requests) <= 2 or len({r["device_id"] for r in requests}) != len(requests):
                            raise ValueError("一次委派一至兩台不同設備")
                        for item in requests:
                            if item.get("task_id") and repo.task(item["task_id"]).status in {"completed", "cancelled", "aborted", "resolved"}:
                                raise ValueError("指定的是已結束任務，請重新讀取 get_home_state；沒有未完成任務的設備須省略 task_id 以建立新計畫")
                        for request in requests:
                            device = request["device_id"]
                            if device not in {"washer-1", "ac-1"} or not isinstance(request.get("text"), str):
                                raise ValueError("設備或需求不合法")
                            identifier = sha256(f"{root_id}:{device}".encode()).hexdigest()
                            try:
                                with repo.transaction() as db:
                                    old = db.execute("SELECT data FROM delegations WHERE id=?", (identifier,)).fetchone()
                                    previous = json.loads(old[0]) if old else None
                                    if previous and previous.get("result"):
                                        current = repo.task(previous["task_id"])
                                        results.append(previous["result"] if current.version == previous["version"] else {"device_id": device, "status": "failed", "message": "委派結果已過期，保留使用者更新後的任務"})
                                        continue
                                    if previous:
                                        task = repo.task(previous["task_id"])
                                        if task.version != previous["version"]:
                                            raise ValueError("委派任務版本已改變，舊委派停止")
                                    elif request.get("task_id"):
                                        task = repo.task(request["task_id"])
                                        if task.device_id != device or type(request.get("expected_version")) is not int or task.version != request.get("expected_version") or task.status not in {"needs_details", "pending", "scheduled", "running", "unknown", "failed"}:
                                            raise ValueError("委派設備或任務版本不符")
                                    else:
                                        tariff = selected_tariff(repo, hems.tariff())
                                        task = repo.create_task(None, hems.program(), tariff, request["text"]) if device == "washer-1" else repo.create_ac_task(None, tariff, request["text"])
                                    if task.status in {"needs_details", "pending"} and not task.approval and not task.ac_approval:
                                        current_tariff = selected_tariff(repo, hems.tariff(), task.task_id)
                                        if current_tariff != task.tariff:
                                            task = repo.update_task(task.task_id, task.version, tariff=current_tariff, plan=None, ac_plan=None)
                                    db.execute("INSERT OR IGNORE INTO delegations VALUES (?,?,?,?,?)", (identifier, root_id, device, task.task_id,
                                        json.dumps({"task_id": task.task_id, "version": task.version})))
                                from dataclasses import replace
                                child_event = replace(event, task_id=task.task_id, payload={"text": request["text"], "root_id": root_id})
                                result = await device_session(repo, child_event, task, budget, run_id)
                                results.append(result)
                                with repo.transaction() as db:
                                    db.execute("UPDATE delegations SET data=? WHERE id=?", (json.dumps({"task_id": task.task_id, "version": repo.task(task.task_id).version, "result": result}), identifier))
                            except (ValueError, ProviderUnavailable) as error:
                                results.append({"device_id": device, "status": "failed", "message": str(error)})
                        result = results
                        good = sum(r["status"] == "completed" for r in results)
                        status = "completed" if good == len(results) else "partial_success" if good else "needs_details" if any(r["status"] == "needs_details" for r in results) else "failed"
                        message = "；".join(r["device_id"] + "：" + r["message"] for r in results)
                        repo.log_tool(run_id, {"provider": reply_provider, "action": action, "result": result})
                        break  # Deterministic summary needs no extra model request.
                    else:
                        raise ValueError("Main Agent 只能查詢與委派")
                    repo.log_tool(run_id, {"provider": reply_provider, "action": action, "result": result})
                    messages.extend([{"role": "assistant", "content": reply}, {"role": "user", "content": json.dumps({"tool_result": result}, ensure_ascii=False)}])
                except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as error:
                    repo.log_tool(run_id, {"error": str(error)})
                    messages.append({"role": "user", "content": json.dumps({"error": str(error)}, ensure_ascii=False)})
    except (ProviderUnavailable, ProviderConfigurationError) as error:
        message = str(error)
    finally:
        if status == "failed" and any(result["status"] == "completed" for result in results):
            status = "partial_success"
        provider = budget.providers[budget.index].name if budget.providers else "none"
        run = AgentRun(run_id, event.event_id, status, provider, budget.count, monotonic() - started, message)
        repo.record_run(run)
        repo.finish_event(event.event_id)
    return run
