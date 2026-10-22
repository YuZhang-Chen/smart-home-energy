"""Light-only agent tools; submitted targets still need controller feedback."""

import inspect
import json
import re
from datetime import datetime
from hashlib import sha256
from uuid import uuid4

from smart_home_energy.agent import ProviderUnavailable, ProviderConfigurationError
from smart_home_energy.bulb_control import fresh
from smart_home_energy.bulb_domain import BulbState, kelvin_to_mired, percent_to_level
from smart_home_energy.domain import decode, utcnow
from smart_home_energy.repository import AgentRun


class LightTools:
    def __init__(self, repo, root_id: str, original_text: str):
        self.repo, self.root_id, self.original_text = repo, root_id, original_text
        self.seen = None

    def get_light_context(self):
        data = self.repo.device("bulb-1")
        state = decode(BulbState, data) if data else None
        self.seen = (state.boot_id, state.state_version) if state and fresh(state, utcnow()) else None
        return {"device_id": "bulb-1", "state": data, "fresh": self.seen is not None,
                "scope": "只能依本次使用者需求操作客廳燈；日夜自動規則尚未啟用"}

    def submit_light_action(self, device_id="bulb-1", power=None, brightness_percent=None,
                            kelvin=None, duration_seconds=0.5):
        if device_id != "bulb-1":
            raise ValueError("Light Agent 只能控制 bulb-1")
        if not self.seen or not self.original_text.strip():
            raise ValueError("需要原始使用者需求與新鮮燈泡狀態")
        data = self.repo.device("bulb-1")
        state = decode(BulbState, data) if data else None
        if not state or not fresh(state, utcnow()) or (state.boot_id, state.state_version) != self.seen:
            raise ValueError("燈泡狀態已改變，請重新讀取")
        if power is not None and type(power) is not bool:
            raise ValueError("power 須為布林值")
        if brightness_percent is not None:
            percent_to_level(brightness_percent)
        if kelvin is not None:
            kelvin_to_mired(kelvin)
        if type(duration_seconds) not in (int, float) or not 0 <= duration_seconds <= 30:
            raise ValueError("漸變秒數須為 0–30")
        if power is None and brightness_percent is None and kelvin is None:
            raise ValueError("沒有燈泡目標")
        text = self.original_text
        if not re.search(r"燈|照明|bulb|light", text, re.I):
            raise ValueError("原始需求沒有授權操作燈泡")
        turn_off = bool(re.search(r"關燈|關掉.*燈|燈.*關掉|燈.*關閉|關閉.*燈", text))
        turn_on = bool(re.search(r"開燈|打開.*燈|燈.*打開|燈.*開啟|開啟.*燈", text))
        if turn_off and power is not False:
            raise ValueError("開關目標與使用者的關燈需求不符")
        if turn_on and power is False:
            raise ValueError("開關目標與使用者的開燈需求不符")
        explicit_percent = [int(x) for x in re.findall(r"(?<!\d)(\d{1,3})\s*%", text)]
        if brightness_percent is not None and not explicit_percent:
            raise ValueError("使用者沒有指定可執行的亮度數值")
        if explicit_percent and brightness_percent not in explicit_percent:
            raise ValueError("亮度與使用者明確指定值不符")
        explicit_kelvin = [int(x) for x in re.findall(r"(?<!\d)(\d{4})\s*(?:K|Kelvin|色溫)", text, re.I)]
        if explicit_kelvin and kelvin not in explicit_kelvin:
            raise ValueError("色溫與使用者明確指定值不符")
        if kelvin is not None and not explicit_kelvin:
            if re.search(r"暖色|暖光", text) and kelvin != 2700:
                raise ValueError("暖色預設只能使用 2700K")
            if not re.search(r"暖色|暖光|冷色|冷光|色溫|kelvin|\d{4}\s*K", text, re.I):
                raise ValueError("使用者沒有指定色溫需求")
        if power is not None and not (turn_on or turn_off or explicit_percent or 0 in explicit_percent):
            raise ValueError("使用者沒有指定開關目標")
        payload = {"device_id": device_id, "power": power, "brightness_percent": brightness_percent,
                   "kelvin": kelvin, "duration_seconds": duration_seconds,
                   "source": "Light Agent", "authorization_ref": "household:" + self.root_id}
        request_id = sha256(f"{self.root_id}:bulb-1".encode()).hexdigest()
        self.repo.submit(request_id, "set_bulb", payload)
        return {"request_id": request_id, "status": "pending", "device_id": device_id,
                "message": "照明設定已排入控制佇列，等待設備回報確認"}

    def call(self, name, arguments):
        if not isinstance(arguments, dict):
            raise ValueError("工具參數須為物件")
        if name not in {"get_light_context", "submit_light_action"}:
            raise ValueError("Light Agent 工具不允許控制其他設備或建立自動規則")
        method = getattr(self, name)
        inspect.signature(method).bind(**arguments)
        return method(**arguments)


LIGHT_SYSTEM = """你是 Light Agent，只能處理客廳智慧燈泡 bulb-1。每次只回一個 JSON 物件。
先呼叫 get_light_context；使用者明確要求開關、亮度或色溫時，呼叫 submit_light_action。
工具格式 {"tool":"name","arguments":{},"summary":"簡短摘要"}；資訊不足則 {"ask":"問題"}。
submit_light_action 參數為 device_id="bulb-1"、power:boolean|null、brightness_percent:0–100|null、kelvin:2700–6500|null、duration_seconds:0–30。
暖色預設 2700K；只改色溫時不指定 power；40% 亮度可開燈。不得自行建立日夜規則、控制冷氣或洗衣機。
提交只代表排入佇列；必須等待設備回報才能宣稱完成。"""


async def light_session(repo, event, budget, parent_id, root_id):
    started, before, run_id = budget.monotonic(), budget.count, uuid4().hex
    tools = LightTools(repo, root_id, event.payload["text"])
    messages = [{"role": "system", "content": LIGHT_SYSTEM},
                {"role": "user", "content": json.dumps({"text": event.payload["text"]}, ensure_ascii=False)}]
    status, message = "failed", "照明需求未處理"
    try:
        while True:
            reply = await budget.complete(messages, repo, run_id)
            try:
                if len(reply) > 65536:
                    raise ValueError("模型回覆過大")
                action = json.loads(reply)
                if not isinstance(action, dict):
                    raise ValueError("回覆須為 JSON 物件")
                if set(action) == {"ask"} and isinstance(action["ask"], str):
                    status, message = "needs_details", action["ask"]
                    break
                if set(action) != {"tool", "arguments", "summary"} or not isinstance(action["summary"], str):
                    raise ValueError("工具動作格式不符")
                result = tools.call(action["tool"], action["arguments"])
                repo.log_tool(run_id, {"role": "Light Agent", "action": action, "result": result})
                messages.extend([{"role": "assistant", "content": reply},
                                 {"role": "user", "content": json.dumps({"tool_result": result}, ensure_ascii=False)}])
                if action["tool"] == "submit_light_action":
                    status, message = "completed", result["message"]
                    break
            except (ValueError, TypeError, KeyError, OverflowError) as error:
                repo.log_tool(run_id, {"role": "Light Agent", "error": str(error)})
                messages.append({"role": "user", "content": json.dumps({"error": str(error)}, ensure_ascii=False)})
    except (ProviderUnavailable, ProviderConfigurationError) as error:
        message = str(error)
    run = AgentRun(run_id, event.event_id, status,
                   budget.providers[budget.index].name if budget.providers else "none",
                   budget.count - before, budget.monotonic() - started, message,
                   parent_id, "bulb-1", "Light Agent")
    repo.record_run(run)
    from smart_home_energy.domain import encode
    return encode(run)
