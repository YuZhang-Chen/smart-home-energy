"""Depth-one model delegation with a shared end-to-end budget and durable IDs."""
import asyncio
import inspect
import json
import re
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
from smart_home_energy.agent_roles import agent_roles
from smart_home_energy.light_agent import light_session, is_explicit_light_only_request
from smart_home_energy.mcp_server import MCPToolExecutionError, SmartHomeMCPServer
from smart_home_energy.tv import tv_session, get_status as get_tv_status
from smart_home_energy.curtain import curtain_session, get_status as get_curtain_status


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
        number(duration, "冷氣運轉分鐘", 1)
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
    mcp = SmartHomeMCPServer(repo)
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
                result = mcp.call_tool_value(
                    role,
                    action["tool"],
                    action["arguments"],
                    tool_instance=tools,
                    run_id=run_id,
                    provider=budget.providers[budget.index].name,
                    device_id=task.device_id,
                    summary=action["summary"],
                )
                messages.extend([{"role": "assistant", "content": reply}, {"role": "user", "content": json.dumps({"tool_result": result}, ensure_ascii=False)}])
                if action["tool"] in {"propose_plan", "reschedule_task"}:
                    status, message = "completed", "計畫已提出，等待使用者核准" if result["status"] == "pending" else "已在核准範圍內改排"
                    break
            except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as error:
                if not isinstance(error, MCPToolExecutionError):
                    repo.log_tool(run_id, {"protocol": "MCP", "role": role, "device_id": task.device_id, "error": str(error)})
                messages.append({"role": "user", "content": json.dumps({"error": str(error)}, ensure_ascii=False)})
    except (ProviderUnavailable, ProviderConfigurationError) as error:
        message = str(error)
    provider = budget.providers[budget.index].name if budget.providers else "none"
    run = AgentRun(run_id, event.event_id, status, provider, budget.count - before, budget.monotonic() - started, message, parent_id, task.device_id, role)
    repo.record_run(run)
    return encode(run)


_AC_DEVICE_RE = re.compile(r"冷氣|空調|空调|air\s*conditioner|\bac\b", re.I)
_AC_OTHER_DEVICE_RE = re.compile(r"洗衣機|洗衣|燈泡|照明|燈|電視|窗簾|washer|bulb|light|tv|curtain", re.I)
_TV_DEVICE_RE = re.compile(r"電視|电视|\btv\b|hdmi|音量|靜音", re.I)
_CURTAIN_DEVICE_RE = re.compile(r"窗簾|窗帘|curtain|blinds", re.I)
_ENERGY_DEVICE_RE = re.compile(r"冷氣|空調|洗衣機|洗衣|燈泡|照明|燈|air\s*conditioner|\bac\b|washer|bulb|light", re.I)
_AC_ACTION_RE = re.compile(r"開啟|打開|開冷氣|開機|開到|開成|啟動|設定|設為|設成|調整|調到|調為|調成|運轉|運行|(?<!關)開(?!始)|turn\s*on|set|adjust", re.I)
_AC_OFF_RE = re.compile(r"關閉|關掉|關冷氣|冷氣關掉|冷氣關閉|停止|關機|turn\s*off", re.I)
_AC_QUERY_RE = re.compile(r"查詢|查看|目前狀態|狀態如何|現在狀態|幾度|多少度|耗電|電費|是否|有沒有|是不是|嗎", re.I)
_AC_FUTURE_RE = re.compile(r"明天|後天|下週|下周|星期[一二三四五六日天]|週[一二三四五六日天]|周[一二三四五六日天]|預約|排程|今晚|明晚|早上|上午|下午|晚上|\d{1,2}\s*點|\d{1,2}:\d{2}", re.I)


def explicit_ac_stop_request(text):
    """Recognize a clear conversational request to stop/cancel the AC only."""
    if not isinstance(text, str) or not _AC_DEVICE_RE.search(text):
        return False
    if _AC_OTHER_DEVICE_RE.search(text) or _AC_QUERY_RE.search(text):
        return False
    return bool(_AC_OFF_RE.search(text))


def _chinese_integer(value):
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if value == "半":
        return 0.5
    if value == "十":
        return 10
    if "十" in value:
        tens, _, units = value.partition("十")
        return (digits[tens] if tens else 1) * 10 + (digits[units] if units else 0)
    if len(value) == 1 and value in digits:
        return digits[value]
    raise ValueError("無法解析冷氣運轉時間")


def explicit_ac_request(text):
    """Return explicit immediate AC targets; leave queries, schedules and mixed-device requests to Main Agent."""
    if not isinstance(text, str) or not _AC_DEVICE_RE.search(text):
        return None
    if _AC_OTHER_DEVICE_RE.search(text) or _AC_OFF_RE.search(text) or _AC_QUERY_RE.search(text):
        return None
    if _AC_FUTURE_RE.search(text) or not _AC_ACTION_RE.search(text):
        return None

    target_values = [
        float(value)
        for value in re.findall(r"(?<!\d)(\d{1,2}(?:\.\d+)?)\s*(?:°\s*C|℃|度)(?!\w)", text, re.I)
    ]
    if len(target_values) > 1:
        return None

    duration_values = []
    for match in re.finditer(
        r"(?<!\d)(\d+(?:\.\d+)?)\s*(小時|小时|時|hr|hrs|hours?|分鐘|分钟|分|min|mins?|minutes?)",
        text,
        re.I,
    ):
        amount = float(match.group(1))
        unit = match.group(2).lower()
        duration_values.append(amount * (60 if unit in {"小時", "小时", "時", "hr", "hrs", "hour", "hours"} else 1))

    if not duration_values:
        half = re.search(r"一個半小時|一個半小时", text)
        if half:
            duration_values.append(90)
        else:
            chinese_matches = list(re.finditer(
                r"(?<![0-9])([零〇一二兩三四五六七八九十半]{1,3})\s*(小時|小时|時|分鐘|分钟|分)",
                text,
            ))
            for match in chinese_matches:
                amount = _chinese_integer(match.group(1))
                unit = match.group(2)
                duration_values.append(amount * (60 if unit in {"小時", "小时", "時"} else 1))
    if len(duration_values) > 1:
        return None

    return {
        "target_c": target_values[0] if target_values else None,
        "duration_minutes": duration_values[0] if duration_values else None,
    }


def run_explicit_ac_request(repo, event, hems, parent_id, root_id, target, monotonic=time.monotonic, progress=None):
    """Create and validate an AC proposal without an LLM; approval and MQTT execution remain unchanged."""
    started = monotonic()
    run_id = uuid4().hex
    provider = "deterministic-ac-planner"
    status = "failed"
    message = "冷氣計畫未完成"
    original_text = event.payload.get("text", "")
    identifier = sha256(f"{root_id}:ac-1".encode()).hexdigest()

    def report(value):
        if progress:
            progress(value)

    try:
        report("已辨識明確冷氣需求，正在檢查設備狀態並建立計畫")
        with repo.transaction() as db:
            old = db.execute("SELECT data FROM delegations WHERE id=?", (identifier,)).fetchone()
            previous = json.loads(old[0]) if old else None
            if previous and previous.get("result"):
                return previous["result"]

            if previous and previous.get("task_id"):
                task = repo.task(previous["task_id"])
            else:
                task = repo.active_task("ac-1")
                if task is None:
                    task = repo.create_ac_task(None, selected_tariff(repo, hems.tariff()), original_text)
                elif task.status not in {"needs_details", "pending", "scheduled"}:
                    raise ValueError(f"客廳冷氣目前任務狀態為 {task.status}，為避免覆蓋運轉或故障中的設備，請先處理現有任務。")
                elif task.ac_approval is not None:
                    raise ValueError("客廳冷氣已有核准排程；請先在網頁確認或取消原排程，再提出新需求。")

            if task.kind != "ac" or task.device_id != "ac-1":
                raise ValueError("目前任務不是客廳冷氣任務，已停止操作。")
            if task.status not in {"needs_details", "pending", "scheduled"}:
                raise ValueError(f"客廳冷氣目前任務狀態為 {task.status}，不能直接修改。")
            if task.ac_approval is not None:
                raise ValueError("客廳冷氣已有核准排程；請先在網頁確認或取消原排程，再提出新需求。")

            db.execute(
                "INSERT OR IGNORE INTO delegations VALUES (?,?,?,?,?)",
                (identifier, root_id, "ac-1", task.task_id,
                 json.dumps({"task_id": task.task_id, "version": task.version})),
            )

        tools = DeviceTools(repo, task.task_id)
        mcp = SmartHomeMCPServer(repo, hems)
        context = mcp.call_tool_value(
            "AC Agent",
            "get_device_context",
            {},
            tool_instance=tools,
            run_id=run_id,
            provider=provider,
            device_id="ac-1",
            summary="讀取客廳冷氣與最新室溫",
        )
        if not context["fresh"]:
            raise ValueError("目前沒有收到新鮮的冷氣室溫回報；請確認 AC 模擬器與 MQTT 連線。尚未提出或啟動冷氣。")

        plan = mcp.call_tool_value(
            "AC Agent",
            "prepare_default_plan",
            {
                "duration_minutes": target.get("duration_minutes"),
                "target_c": target.get("target_c"),
            },
            tool_instance=tools,
            run_id=run_id,
            provider=provider,
            device_id="ac-1",
            summary="依使用者指定及已同意偏好建立冷氣計畫",
        )

        mcp.call_tool_value(
            "AC Agent",
            "validate_plan",
            {"plan": plan},
            tool_instance=tools,
            run_id=run_id,
            provider=provider,
            device_id="ac-1",
            summary="驗證冷氣溫度、時段與費用計算",
        )
        mcp.call_tool_value(
            "AC Agent",
            "propose_plan",
            {"plan": plan, "expected_version": task.version},
            tool_instance=tools,
            run_id=run_id,
            provider=provider,
            device_id="ac-1",
            summary="提出待使用者核准的冷氣計畫",
        )

        actual_target = plan["constraints"]["target_c"]
        duration = (datetime.fromisoformat(plan["constraints"]["end"]) - datetime.fromisoformat(plan["constraints"]["start"])).total_seconds() / 60
        status = "completed"
        comfort_note = (
            "此測試時間較短，預估結束時可能尚未進入舒適溫度範圍。"
            if plan.get("comfortable_after_seconds") is None else ""
        )
        message = (
            f"已建立客廳冷氣計畫：目標 {actual_target:g}°C，運轉 {duration:g} 分鐘。"
            + comfort_note
            + "計畫已通過驗證，目前等待使用者核准；核准後才會由控制服務透過 MQTT 發送啟動命令，並等待設備回報確認。"
        )
    except (ValueError, TypeError, KeyError, OverflowError) as error:
        message = str(error)
        status = "needs_details" if any(word in message for word in ("新鮮", "請先", "已有核准", "目前任務狀態")) else "failed"
        if not isinstance(error, MCPToolExecutionError):
            repo.log_tool(run_id, {"protocol": "MCP", "role": "AC Agent", "provider": provider, "device_id": "ac-1", "error": message})

    run = AgentRun(run_id, event.event_id, status, provider, 0, monotonic() - started, message, parent_id, "ac-1", "AC Agent")
    repo.record_run(run)
    result = encode(run)
    with repo.transaction() as db:
        db.execute(
            "UPDATE delegations SET data=? WHERE id=?",
            (json.dumps({"task_id": task.task_id if "task" in locals() else None,
                         "version": repo.task(task.task_id).version if "task" in locals() else None,
                         "result": result}), identifier),
        )
    return result


def run_explicit_ac_stop_request(repo, event, parent_id, root_id, monotonic=time.monotonic, progress=None):
    """Queue a confirmed AC abort through the resident controller, never via an LLM tool call."""
    started = monotonic()
    run_id = uuid4().hex
    provider = "deterministic-ac-stop"
    status = "failed"
    message = "冷氣停止請求未完成"

    def report(value):
        if progress:
            progress(value)

    try:
        report("已辨識關閉冷氣需求，正在核對目前運轉任務與設備回報")
        task = repo.active_task("ac-1")
        state = repo.device("ac-1")

        if task is None:
            if state and state.get("status") in {"idle", "completed", "aborted"}:
                status, message = "completed", "客廳冷氣目前已是停止狀態，沒有需要關閉的運轉任務。"
            else:
                raise ValueError("找不到可安全中止的客廳冷氣任務；請先刷新設備狀態。")
        elif task.status in {"needs_details", "pending", "scheduled"}:
            repo.update_task(task.task_id, task.version, status="cancelled", message="使用者要求關閉冷氣；未啟動計畫已取消")
            status, message = "completed", "客廳冷氣尚未啟動，已取消原本的待核准／排程計畫。"
        elif task.status == "stopping":
            status, message = "completed", "客廳冷氣已收到停止請求，目前正在等待設備回報確認。"
        elif task.status in {"starting", "running", "unknown"}:
            if not state:
                raise ValueError("尚未取得冷氣設備狀態，為避免誤送停止命令，請稍後再試。")
            observed = datetime.fromisoformat(state["observed_at"])
            age = (utcnow() - observed).total_seconds()
            if not -2 <= age <= 10:
                raise ValueError("冷氣設備回報已過期，尚未送出停止命令；請確認 MQTT 與 AC 模擬器連線。")
            if state.get("status") != "running" or state.get("task_id") != task.task_id or not task.run_id or state.get("run_id") != task.run_id:
                raise ValueError("冷氣目前沒有回報與此任務相符的運轉狀態；尚未送出停止命令，請先刷新設備狀態。")

            request_id = f"assistant:{root_id}:abort:ac-1"
            payload = {"task_id": task.task_id, "expected_version": task.version, "confirmed": True}
            previous = repo.request(request_id)
            if previous is None:
                repo.submit(request_id, "abort", payload)
            elif previous["kind"] != "abort" or json.loads(previous["payload"]) != payload:
                raise ValueError("停止請求識別碼衝突；請重新送出關閉指令。")

            report("停止命令已交給控制服務，正在等待冷氣設備回報")
            deadline = time.monotonic() + 7.0
            while time.monotonic() < deadline:
                request = repo.request(request_id)
                current = repo.task(task.task_id)
                if request and request["status"] == "error":
                    raise ValueError(request.get("result") or "控制服務拒絕停止命令")
                if current.status == "aborted":
                    status, message = "completed", "已收到設備回報確認，客廳冷氣已關閉。"
                    break
                if current.status in {"failed", "resolved"}:
                    raise ValueError(current.message or "冷氣停止狀態需要人工查核。")
                time.sleep(0.1)
            else:
                current = repo.task(task.task_id)
                if current.status == "stopping":
                    status, message = "completed", "已送出冷氣停止命令，控制服務已接收；目前仍等待設備回報確認，請以冷氣狀態卡為準。"
                else:
                    status, message = "needs_details", "已提出冷氣停止請求，但尚未取得設備確認；請檢查控制服務、MQTT 與冷氣狀態卡，不要重複送出命令。"
        elif task.status in {"completed", "aborted", "cancelled", "resolved"}:
            status, message = "completed", "客廳冷氣任務已結束，目前沒有需要中止的運轉計畫。"
        else:
            raise ValueError(f"客廳冷氣目前任務狀態為 {task.status}，無法安全送出停止命令。")

        repo.log_tool(run_id, {
            "protocol": "MCP", "mcp_method": "tools/call",
            "role": "AC Agent", "provider": provider, "device_id": "ac-1",
            "action": {"tool": "request_ac_stop", "arguments": {"task_id": task.task_id if task else None},
                       "summary": "核對同一運轉任務後，透過控制服務送出停止請求"},
            "result": {"status": status, "message": message},
        })
    except (ValueError, TypeError, KeyError, OverflowError) as error:
        message = str(error)
        status = "needs_details" if any(word in message for word in ("過期", "刷新", "回報", "連線", "尚未取得")) else "failed"
        repo.log_tool(run_id, {"role": "AC Agent", "provider": provider, "device_id": "ac-1", "error": message})

    run = AgentRun(run_id, event.event_id, status, provider, 0, monotonic() - started, message, parent_id, "ac-1", "AC Agent")
    repo.record_run(run)
    return encode(run)


def is_explicit_tv_only_request(text: str) -> bool:
    if not isinstance(text, str) or not _TV_DEVICE_RE.search(text):
        return False
    if _ENERGY_DEVICE_RE.search(text) or _CURTAIN_DEVICE_RE.search(text):
        return False
    return True


def is_explicit_curtain_only_request(text: str) -> bool:
    if not isinstance(text, str) or not _CURTAIN_DEVICE_RE.search(text):
        return False
    if _ENERGY_DEVICE_RE.search(text) or _TV_DEVICE_RE.search(text):
        return False
    return True


def run_tv_agent(repo, event, parent_id, root_id, text: str, monotonic=time.monotonic, progress=None):
    """Execute a TV command via TV Agent and log durable diagnostics."""
    from smart_home_energy import tv
    started = monotonic()
    run_id = uuid4().hex
    provider = "deterministic-tv-agent"
    status = "failed"
    message = "電視指令未完成"
    identifier = sha256(f"{root_id}:tv-1".encode()).hexdigest()

    if progress:
        progress("TV Agent 正在執行客廳電視控制指令")
    try:
        outcome = tv.execute_command(text)
        status = "completed"
        message = str(outcome["message"])
        repo.log_tool(run_id, {
            "protocol": "MCP", "mcp_method": "tools/call",
            "role": "TV Agent", "provider": provider, "device_id": "tv-1",
            "action": {"tool": "control_tv", "arguments": {"command": text}, "summary": "執行電視控制與狀態同步"},
            "result": outcome["status"],
        })
    except ValueError as error:
        message = str(error)
        status = "needs_details"
        repo.log_tool(run_id, {"role": "TV Agent", "provider": provider, "device_id": "tv-1", "error": message})

    run = AgentRun(run_id, event.event_id, status, provider, 0, monotonic() - started, message, parent_id, "tv-1", "TV Agent")
    repo.record_run(run)
    result = encode(run)
    with repo.transaction() as db:
        db.execute(
            "INSERT OR REPLACE INTO delegations VALUES (?,?,?,?,?)",
            (identifier, root_id, "tv-1", None, json.dumps({"request_id": identifier, "result": result})),
        )
    return result


def run_curtain_agent(repo, event, parent_id, root_id, text: str, monotonic=time.monotonic, progress=None):
    """Execute a curtain command via Curtain Agent and log durable diagnostics."""
    from smart_home_energy import curtain
    started = monotonic()
    run_id = uuid4().hex
    provider = "deterministic-curtain-agent"
    status = "failed"
    message = "窗簾指令未完成"
    identifier = sha256(f"{root_id}:curtain-1".encode()).hexdigest()

    if progress:
        progress("Curtain Agent 正在執行客廳窗簾控制指令")
    try:
        state = curtain.execute_command(text)
        status = "completed"
        target = round(float(state.get("target", state.get("position", 0))))
        if state.get("status") == "stopped" and "停" in text:
            message = f"客廳窗簾已停止移動，目前位置 {round(float(state.get('position', 0)))}%。"
        elif any(word in text for word in ("排程", "定時", "每天", "預約")):
            message = f"已為客廳窗簾設定自動排程（目標位置 {target}%）。"
        else:
            label = "全開 (100%)" if target >= 99 else "全關 (0%)" if target <= 1 else f"開啟 {target}%"
            message = f"客廳窗簾已執行指令：移動至 {label}。"
        repo.log_tool(run_id, {
            "protocol": "MCP", "mcp_method": "tools/call",
            "role": "Curtain Agent", "provider": provider, "device_id": "curtain-1",
            "action": {"tool": "control_curtain", "arguments": {"command": text}, "summary": "執行窗簾開合或排程控制"},
            "result": state,
        })
    except ValueError as error:
        message = str(error)
        status = "needs_details"
        repo.log_tool(run_id, {"role": "Curtain Agent", "provider": provider, "device_id": "curtain-1", "error": message})

    run = AgentRun(run_id, event.event_id, status, provider, 0, monotonic() - started, message, parent_id, "curtain-1", "Curtain Agent")
    repo.record_run(run)
    result = encode(run)
    with repo.transaction() as db:
        db.execute(
            "INSERT OR REPLACE INTO delegations VALUES (?,?,?,?,?)",
            (identifier, root_id, "curtain-1", None, json.dumps({"request_id": identifier, "result": result})),
        )
    return result


async def run_household(repo, event, hems, providers=None, monotonic=time.monotonic, progress=None):
    run_id, started = uuid4().hex, monotonic()
    results, status, message = [], "failed", "家庭工作未完成，請使用各設備表單"
    budget = SharedBudget(hems, [], monotonic)
    root_id = event.payload.get("root_id", event.event_id)
    def report(text):
        if progress:
            progress(text)
    try:
        if event.task_id:
            budget.providers = configured_providers() if providers is None else providers
            results.append(await device_session(repo, event, repo.task(event.task_id), budget, run_id))
            status, message = results[0]["status"], results[0]["message"]
        elif is_explicit_light_only_request(event.payload.get("text", "")):
            # Clear bulb-only brightness/Kelvin controls go straight to Light Agent.
            # Relative Kelvin changes are resolved there using the latest device
            # state, without spending the shared Main Agent model budget.
            report("已辨識燈泡控制需求，正在讀取設備狀態並計算亮度／色溫目標")
            identifier = sha256(f"{root_id}:bulb-1".encode()).hexdigest()
            previous_result = None
            with repo.transaction() as db:
                old = db.execute("SELECT data FROM delegations WHERE id=?", (identifier,)).fetchone()
                previous = json.loads(old[0]) if old else None
                if previous and previous.get("result"):
                    previous_result = previous["result"]
                else:
                    db.execute("INSERT OR IGNORE INTO delegations VALUES (?,?,?,?,?)",
                               (identifier, root_id, "bulb-1", None,
                                json.dumps({"request_id": identifier})))
            if previous_result is not None:
                result = previous_result
            else:
                result = await light_session(repo, event, budget, run_id, root_id)
                with repo.transaction() as db:
                    db.execute("UPDATE delegations SET data=? WHERE id=?",
                               (json.dumps({"request_id": identifier, "result": result}), identifier))
            results.append(result)
            status, message = result["status"], result["message"]
        elif explicit_ac_stop_request(event.payload.get("text", "")):
            result = run_explicit_ac_stop_request(repo, event, run_id, root_id, monotonic, report)
            results.append(result)
            status, message = result["status"], result["message"]
        elif (ac_target := explicit_ac_request(event.payload.get("text", ""))) is not None:
            result = run_explicit_ac_request(repo, event, hems, run_id, root_id, ac_target, monotonic, report)
            results.append(result)
            status, message = result["status"], result["message"]
        else:
            budget.providers = configured_providers() if providers is None else providers
            if not budget.providers and is_explicit_tv_only_request(event.payload.get("text", "")):
                result = await tv_session(repo, event, budget, run_id, root_id, report)
                results.append(result)
                status, message = result["status"], result["message"]
            elif not budget.providers and is_explicit_curtain_only_request(event.payload.get("text", "")):
                result = await curtain_session(repo, event, budget, run_id, root_id, report)
                results.append(result)
                status, message = result["status"], result["message"]
            else:
                system = """你是智慧家庭 Main Agent，使用繁體中文，每次只回一個 JSON 物件。
工具 {"tool":"get_home_state","arguments":{},"summary":"摘要"}。
可用 get_agent_roles={} 查詢角色權責。
批次委派 {"tool":"delegate_tasks","arguments":{"requests":[{"device_id":"washer-1、ac-1、bulb-1、tv-1 或 curtain-1","text":"完整原始需求中與該設備相關的條件","task_id":"僅修改冷氣或洗衣現有任務時填入","expected_version":1}]},"summary":"摘要"}。
同一需求可委派一至五台不同設備，各自失敗；冷氣（ac-1）與洗衣（washer-1）由子 Agent 提出計畫並各自等待核准；照明（bulb-1）由 Light Agent 排入控制佇列並等待回報；電視（tv-1，含開關機、音量、靜音、HDMI1~4、播放/暫停/看電影情境）委派給 TV Agent 執行；窗簾（curtain-1，含開合百分比、遮光/透光/太陽刺眼情境、停止與每日定時排程）委派給 Curtain Agent 執行。
使用者已同意預設：開冷氣27°C、一小時、確認後開始；洗衣24小時內完成、避開23:00–07:00並選較便宜時段。不因未填時間而補問，將完整需求交給子 Agent 套用偏好。
這些數字是初始偏好，以 get_home_state 的 preferences 為準。委派 text 保留使用者原文及明確條件，不把未說出的初始數字當成使用者指定值。
若設備已有待補充、待核准或未啟動任務，修改同設備必須傳 task_id 與 expected_version，不新增第二個任務；未提及的設備保持原樣。缺正式費率基準等必要資訊才補問，向使用者說明需要哪個月份、多少度，不要求填JSON。
task_id 只能取自 get_home_state.tasks 的未完成任務；設備回報可能仍帶有上次已結束的行程，不能當成目前任務。tasks 沒有該設備時（以及 bulb-1、tv-1、curtain-1），委派請省略 task_id 和 expected_version。
先 get_home_state 再 delegate_tasks。每個設備最多一個子工作；Main Agent 不直接核准、不直接控制設備、不自行計算費用。
僅查詢可 {"finish":"結果"}；資訊不足可 {"ask":"問題"}。"""
                mcp = SmartHomeMCPServer(repo, hems)
                messages = [{"role": "system", "content": system}, {"role": "user", "content": event.payload["text"]}]
                while True:
                    report("Main Agent 正在理解需求與檢查設備狀態")
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
                        report("Agent 正在執行「" + action["tool"] + "」")
                        if action["tool"] in {"get_home_state", "get_agent_roles"}:
                            result = mcp.call_tool_value(
                                "Main Agent",
                                action["tool"],
                                action["arguments"],
                                run_id=run_id,
                                provider=reply_provider,
                                summary=action["summary"],
                            )
                            messages.extend([{"role": "assistant", "content": reply}, {"role": "user", "content": json.dumps({"tool_result": result}, ensure_ascii=False)}])
                            continue
                        elif action["tool"] == "delegate_tasks" and set(action["arguments"]) == {"requests"}:
                            requests = action["arguments"]["requests"]
                            if not isinstance(requests, list) or not 1 <= len(requests) <= 5 or len({r["device_id"] for r in requests}) != len(requests):
                                raise ValueError("一次委派一至五台不同設備")
                            for item in requests:
                                if item.get("device_id") not in {"bulb-1", "tv-1", "curtain-1"} and item.get("task_id") and repo.task(item["task_id"]).status in {"completed", "cancelled", "aborted", "resolved"}:
                                    raise ValueError("指定的是已結束任務，請重新讀取 get_home_state；沒有未完成任務的設備須省略 task_id 以建立新計畫")
                            for request in requests:
                                device = request["device_id"]
                                if device not in {"washer-1", "ac-1", "bulb-1", "tv-1", "curtain-1"} or not isinstance(request.get("text"), str):
                                    raise ValueError("設備或需求不合法")
                                identifier = sha256(f"{root_id}:{device}".encode()).hexdigest()
                                try:
                                    if device in {"tv-1", "curtain-1"}:
                                        request = {key: value for key, value in request.items()
                                                   if key not in {"task_id", "expected_version"}}
                                        with repo.transaction() as db:
                                            old = db.execute("SELECT data FROM delegations WHERE id=?", (identifier,)).fetchone()
                                            previous = json.loads(old[0]) if old else None
                                            if previous and previous.get("result"):
                                                results.append(previous["result"])
                                                continue
                                            db.execute("INSERT OR IGNORE INTO delegations VALUES (?,?,?,?,?)",
                                                       (identifier, root_id, device, None, json.dumps({"request_id": identifier})))
                                        child_event = replace(event, task_id=None,
                                                              payload={"text": request["text"], "root_id": root_id})
                                        session_fn = tv_session if device == "tv-1" else curtain_session
                                        result = await session_fn(repo, child_event, budget, run_id, root_id, report)
                                        results.append(result)
                                        with repo.transaction() as db:
                                            db.execute("UPDATE delegations SET data=? WHERE id=?",
                                                       (json.dumps({"request_id": identifier, "result": result}), identifier))
                                        continue
                                    if device == "bulb-1":
                                        # A bulb action is immediate and has no scheduler task.
                                        # Models sometimes copy task_id/expected_version from the
                                        # shared home-state response; discard those fields at this
                                        # boundary instead of turning an otherwise valid light
                                        # request into a user-visible delegation error.
                                        request = {key: value for key, value in request.items()
                                                   if key not in {"task_id", "expected_version"}}
                                        with repo.transaction() as db:
                                            old = db.execute("SELECT data FROM delegations WHERE id=?", (identifier,)).fetchone()
                                            previous = json.loads(old[0]) if old else None
                                            if previous and previous.get("result"):
                                                results.append(previous["result"])
                                                continue
                                            db.execute("INSERT OR IGNORE INTO delegations VALUES (?,?,?,?,?)",
                                                       (identifier, root_id, device, None, json.dumps({"request_id": identifier})))
                                        child_event = replace(event, task_id=None,
                                                              payload={"text": event.payload["text"], "root_id": root_id})
                                        result = await light_session(repo, child_event, budget, run_id, root_id)
                                        results.append(result)
                                        with repo.transaction() as db:
                                            db.execute("UPDATE delegations SET data=? WHERE id=?",
                                                       (json.dumps({"request_id": identifier, "result": result}), identifier))
                                        continue
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
                                    child_event = replace(event, task_id=task.task_id, payload={"text": request["text"], "root_id": root_id})
                                    result = await device_session(repo, child_event, task, budget, run_id)
                                    results.append(result)
                                    report(("冷氣" if device == "ac-1" else "洗衣機") + "：" + result.get("message", "設備計畫已更新"))
                                    with repo.transaction() as db:
                                        db.execute("UPDATE delegations SET data=? WHERE id=?", (json.dumps({"task_id": task.task_id, "version": repo.task(task.task_id).version, "result": result}), identifier))
                                except (ValueError, ProviderUnavailable) as error:
                                    results.append({"device_id": device, "status": "failed", "message": str(error)})
                            result = results
                            good = sum(r["status"] == "completed" for r in results)
                            status = "completed" if good == len(results) else "partial_success" if good else "needs_details" if any(r["status"] == "needs_details" for r in results) else "failed"
                            message = "；".join(r["device_id"] + "：" + r["message"] for r in results)
                            repo.log_tool(run_id, {"protocol": "MCP", "mcp_method": "tools/call", "role": "Main Agent", "provider": reply_provider, "action": action, "result": result})
                            break  # Deterministic summary needs no extra model request.
                        else:
                            mcp.call_tool_value(
                                "Main Agent",
                                action["tool"],
                                action["arguments"],
                                run_id=run_id,
                                provider=reply_provider,
                                summary=action["summary"],
                            )
                    except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as error:
                        if not isinstance(error, MCPToolExecutionError):
                            repo.log_tool(run_id, {"protocol": "MCP", "role": "Main Agent", "error": str(error)})
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
