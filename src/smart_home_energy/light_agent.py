
"""Light-only agent tools; submitted targets still need controller feedback."""

import inspect
import json
import re
from hashlib import sha256
from uuid import uuid4

from smart_home_energy.agent import ProviderUnavailable, ProviderConfigurationError
from smart_home_energy.bulb_control import fresh
from smart_home_energy.bulb_domain import (
    BulbState,
    kelvin_to_mired,
    percent_to_level,
)
from smart_home_energy.domain import decode, utcnow
from smart_home_energy.mcp_server import MCPToolExecutionError, SmartHomeMCPServer
from smart_home_energy.repository import AgentRun


_OTHER_DEVICE_RE = re.compile(
    r"冷氣|空調|洗衣機|洗衣|air\s*conditioner|washer", re.I
)

_RELATIVE_BRIGHTNESS_RE = re.compile(
    r"增加|提高|提升|降低|減少|調亮|調暗|亮一點|暗一點|"
    r"多\s*亮|少\s*亮|increase|decrease|brighter|dimmer",
    re.I,
)

_LIGHT_RE = re.compile(r"燈泡|燈|照明|bulb|light", re.I)

_ACTION_RE = re.compile(
    r"調整|調到|調為|調成|設定|設為|設成|改為|改成|"
    r"調高|調低|提高|降低|增加|減少|升高|下降|"
    r"往上調|往下調|變暖|變冷|set|adjust",
    re.I,
)

_KELVIN_RE = re.compile(r"色溫|色温|kelvin|開爾文", re.I)

_ABSOLUTE_K_RE = re.compile(
    r"(?<!\d)(\d{4})\s*(?:K|Kelvin|開爾文)(?![A-Za-z])",
    re.I,
)

_RELATIVE_K_RE = re.compile(
    r"(提高|調高|升高|增加|往上調|往上升|"
    r"降低|調低|下降|減少|往下調|往下降)"
    r"\s*(?:了|約|大約)?\s*(\d{3,4})\s*"
    r"(?:K|Kelvin|開爾文)?(?![A-Za-z])",
    re.I,
)


def _absolute_kelvin_values(text: str) -> list[int]:
    """Extract final Kelvin targets, excluding relative-change amounts.

    For example, in "色溫提高 500K", 500K is a delta, not an absolute
    target. Remove relative clauses before looking for absolute values.
    """

    searchable = text

    # A value attached to a relative verb is an adjustment amount, not
    # necessarily the final device target (e.g. "往上調 500K").
    relative_matches = list(_RELATIVE_K_RE.finditer(searchable))
    for match in reversed(relative_matches):
        searchable = (
            searchable[:match.start()]
            + (" " * (match.end() - match.start()))
            + searchable[match.end():]
        )

    values = [
        int(x)
        for x in _ABSOLUTE_K_RE.findall(searchable)
    ]

    if values:
        return values

    if _KELVIN_RE.search(searchable):
        matches = re.findall(
            r"(?:色溫|色温)[^\d]{0,16}(\d{4})(?!\d)|"
            r"(?<!\d)(\d{4})(?!\d)[^\d]{0,16}(?:色溫|色温)",
            searchable,
        )

        values = [
            int(value)
            for pair in matches
            for value in pair
            if value
        ]

    return values


def _relative_kelvin_target(
    text: str,
    state: BulbState | None,
) -> int | None:
    """Calculate a relative Kelvin target from the current device state."""

    match = _RELATIVE_K_RE.search(text)

    if not match or state is None or not state.mired:
        return None

    direction = match.group(1)
    amount = int(match.group(2))

    current_kelvin = round(1_000_000 / state.mired)

    increase = direction in {
        "提高", "調高", "升高", "增加", "往上調", "往上升"
    }

    target = (
        current_kelvin + amount
        if increase
        else current_kelvin - amount
    )

    if not 2700 <= target <= 6500:
        return None

    return target


def _endpoint_kelvin(text: str) -> int | None:
    """Resolve explicit warm/cool presets."""

    warm = bool(re.search(r"暖色|暖光|調暖|變暖", text))
    cool = bool(re.search(r"冷色|冷光|調冷|變冷", text))

    if warm and not cool:
        return 2700

    if cool and not warm:
        return 6500

    return None


def _needs_kelvin_target(text: str) -> bool:
    """Detect temperature direction requests without a usable target."""

    if not _KELVIN_RE.search(text):
        return False

    direction = re.search(
        r"調高|調低|提高|降低|增加|減少|升高|下降|"
        r"往上|往下|變暖|變冷|調暖|調冷",
        text,
    )

    return bool(
        direction
        and not _absolute_kelvin_values(text)
        and not _RELATIVE_K_RE.search(text)
        and _endpoint_kelvin(text) is None
    )


def explicit_light_target(
    text: str,
    state: BulbState | None = None,
) -> dict | None:
    """Parse explicit brightness and Kelvin targets."""

    if not isinstance(text, str) or not _LIGHT_RE.search(text):
        return None

    has_brightness = bool(re.search(r"亮度|(?<!\d)\d{1,3}\s*%", text))
    has_kelvin = bool(
        _KELVIN_RE.search(text)
        or _RELATIVE_K_RE.search(text)
        or re.search(r"暖色|暖光|冷色|冷光|調暖|調冷", text)
    )

    absolute_kelvin = _absolute_kelvin_values(text)

    if not _ACTION_RE.search(text):
        if not absolute_kelvin:
            return None

    target = {
        "device_id": "bulb-1",
        "duration_seconds": 0.5,
    }

    # Brightness
    if has_brightness:
        if _RELATIVE_BRIGHTNESS_RE.search(text):
            return None

        values = [
            int(value)
            for value in re.findall(
                r"(?<!\d)(\d{1,3})\s*%",
                text,
            )
        ]

        if len(values) != 1 or not 1 <= values[0] <= 100:
            return None

        target["brightness_percent"] = values[0]

    # Kelvin
    if has_kelvin or absolute_kelvin:

        if absolute_kelvin:
            if len(absolute_kelvin) != 1:
                return None

            kelvin = absolute_kelvin[0]

        else:
            kelvin = _relative_kelvin_target(text, state)

            if kelvin is None:
                kelvin = _endpoint_kelvin(text)

        if kelvin is None or not 2700 <= kelvin <= 6500:
            return None

        target["kelvin"] = kelvin

    if (
        "brightness_percent" not in target
        and "kelvin" not in target
    ):
        return None

    return target


# Backward compatibility
def explicit_brightness_target(
    text: str,
    state: BulbState | None = None,
) -> dict | None:
    return explicit_light_target(text, state)


def is_explicit_light_only_request(text: str) -> bool:
    """Route clear bulb-only controls to Light Agent without using Main LLM.

    Relative Kelvin commands need the current bulb state before their target
    can be calculated. They must still enter light_session directly, rather
    than being sent to Main Agent just because the parser has no state yet.
    """

    if not isinstance(text, str):
        return False

    if not _LIGHT_RE.search(text) or _OTHER_DEVICE_RE.search(text):
        return False

    if explicit_light_target(text) is not None:
        return True

    has_kelvin_intent = bool(
        _KELVIN_RE.search(text)
        or _ABSOLUTE_K_RE.search(text)
        or _RELATIVE_K_RE.search(text)
        or re.search(r"暖色|暖光|冷色|冷光|調暖|調冷", text)
    )
    has_adjustment_intent = bool(
        _ACTION_RE.search(text)
        or re.search(r"往上|往下", text)
    )

    return has_kelvin_intent and has_adjustment_intent



class LightTools:

    def __init__(self, repo, root_id: str, original_text: str):
        self.repo = repo
        self.root_id = root_id
        self.original_text = original_text
        self.seen = None

    def get_light_context(self):
        from smart_home_energy.lighting_runner import lighting_summary

        data = self.repo.device("bulb-1")
        state = decode(BulbState, data) if data else None
        now = utcnow()

        self.seen = (
            (state.boot_id, state.state_version)
            if state and fresh(state, now)
            else None
        )

        return {
            "device_id": "bulb-1",
            "state": data,
            "fresh": self.seen is not None,
            "automation": lighting_summary(self.repo, now),
            "scope": "只能依本次使用者需求操作客廳燈；可讀取並說明日夜自動照明狀態，但不能建立或修改規則",
        }

    def submit_light_action(
        self,
        device_id="bulb-1",
        power=None,
        brightness_percent=None,
        kelvin=None,
        duration_seconds=0.5,
    ):

        if device_id != "bulb-1":
            raise ValueError("Light Agent 只能控制 bulb-1")

        if not self.seen or not self.original_text.strip():
            raise ValueError("需要原始使用者需求與新鮮燈泡狀態")

        data = self.repo.device("bulb-1")
        state = decode(BulbState, data) if data else None

        if (
            not state
            or not fresh(state, utcnow())
            or (state.boot_id, state.state_version) != self.seen
        ):
            raise ValueError("燈泡狀態已改變，請重新讀取")

        if power is not None and type(power) is not bool:
            raise ValueError("power 須為布林值")

        if brightness_percent is not None:
            percent_to_level(brightness_percent)

        if kelvin is not None:
            kelvin_to_mired(kelvin)

        if (
            type(duration_seconds) not in (int, float)
            or not 0 <= duration_seconds <= 30
        ):
            raise ValueError("漸變秒數須為 0–30")

        if (
            power is None
            and brightness_percent is None
            and kelvin is None
        ):
            raise ValueError("沒有燈泡目標")

        text = self.original_text

        if not _LIGHT_RE.search(text):
            raise ValueError("原始需求沒有授權操作燈泡")

        # Power validation
        turn_off = bool(
            re.search(
                r"關燈|關掉.*燈|燈.*關掉|燈.*關閉|關閉.*燈",
                text,
            )
        )

        turn_on = bool(
            re.search(
                r"開燈|打開.*燈|燈.*打開|燈.*開啟|開啟.*燈",
                text,
            )
        )

        if turn_off and power is not False:
            raise ValueError("開關目標與使用者的關燈需求不符")

        if turn_on and power is False:
            raise ValueError("開關目標與使用者的開燈需求不符")

        # Brightness validation
        explicit_percent = [
            int(x)
            for x in re.findall(
                r"(?<!\d)(\d{1,3})\s*%",
                text,
            )
        ]

        if brightness_percent is not None and not explicit_percent:
            raise ValueError("使用者沒有指定可執行的亮度數值")

        if (
            explicit_percent
            and brightness_percent not in explicit_percent
        ):
            raise ValueError("亮度與使用者明確指定值不符")

        # Kelvin validation
        if kelvin is not None:

            explicit_kelvin = _absolute_kelvin_values(text)

            if explicit_kelvin:

                if kelvin not in explicit_kelvin:
                    raise ValueError("色溫與使用者明確指定值不符")

            else:

                relative_kelvin = _relative_kelvin_target(
                    text,
                    state,
                )

                endpoint_kelvin = _endpoint_kelvin(text)

                expected_kelvin = (
                    relative_kelvin
                    if relative_kelvin is not None
                    else endpoint_kelvin
                )

                if expected_kelvin is None:
                    raise ValueError(
                        "請明確指定色溫目標，例如 3000K 或 4000K"
                    )

                if kelvin != expected_kelvin:
                    raise ValueError("色溫目標與使用者需求不符")

        # Power authorization
        if power is not None and not (
            turn_on
            or turn_off
            or explicit_percent
            or 0 in explicit_percent
        ):
            raise ValueError("使用者沒有指定開關目標")

        payload = {
            "device_id": device_id,
            "power": power,
            "brightness_percent": brightness_percent,
            "kelvin": kelvin,
            "duration_seconds": duration_seconds,
            "source": "Light Agent",
            "authorization_ref": "household:" + self.root_id,
        }

        request_id = sha256(
            f"{self.root_id}:bulb-1".encode()
        ).hexdigest()

        self.repo.submit(
            request_id,
            "set_bulb",
            payload,
        )

        return {
            "request_id": request_id,
            "status": "pending",
            "device_id": device_id,
            "message": "照明設定已排入控制佇列，等待設備回報確認",
        }

    def call(self, name, arguments):

        if not isinstance(arguments, dict):
            raise ValueError("工具參數須為物件")

        if name not in {
            "get_light_context",
            "submit_light_action",
        }:
            raise ValueError(
                "Light Agent 工具不允許控制其他設備或建立自動規則"
            )

        method = getattr(self, name)

        inspect.signature(method).bind(**arguments)

        return method(**arguments)


LIGHT_SYSTEM = """你是 Light Agent，只能處理客廳智慧燈泡 bulb-1。
使用繁體中文。

系統已經先取得燈泡狀態，結果會放在使用者訊息的 tool_result 欄位。
不得再次呼叫 get_light_context。

如果使用者明確要求調整亮度、開關或色溫，
且設備狀態 fresh=true，請呼叫 submit_light_action。

色溫範圍為 2700K 至 6500K。
數值越低越暖黃，數值越高越冷白。

不得自行猜測使用者沒有指定的色溫數值。
相對調整必須依照目前設備狀態計算。
沒有明確目標時，應使用 ask 詢問使用者。

工具呼叫格式：
{
  "tool": "submit_light_action",
  "arguments": {
    "device_id": "bulb-1",
    "power": null,
    "brightness_percent": null,
    "kelvin": 4000,
    "duration_seconds": 0.5
  },
  "summary": "調整燈泡色溫"
}

若資訊不足或設備狀態不新鮮：
{"ask": "具體問題"}

若使用者詢問目前自動照明規則、或詢問為什麼燈泡自動開關／被手動暫停，請依據 tool_result.automation 的內容以 {"finish": "說明"} 回答，並提醒使用者若需啟用、停用或修改時段，可至「偏好設定 › 自動照明」調整；Light Agent 不能建立或修改自動規則。

重要規則：
1. arguments 必須是 JSON 物件。
2. 不得將 arguments 和其他文字合併成欄位名稱。
3. 不得將 ask 放在 arguments 裡面。
4. 工具呼叫必須包含 tool、arguments、summary 三個欄位。
5. 不得回傳 Markdown 或 JSON 以外的文字。
6. 不得控制冷氣或洗衣機。

提交命令不代表設備已完成。
必須等待 MQTT 設備回報並確認狀態後，才能宣稱成功。"""


async def light_session(
    repo,
    event,
    budget,
    parent_id,
    root_id,
):

    started = budget.monotonic()
    before = budget.count
    run_id = uuid4().hex

    original_text = event.payload["text"]

    tools = LightTools(
        repo,
        root_id,
        original_text,
    )
    mcp = SmartHomeMCPServer(repo)

    status = "failed"
    message = "照明需求未處理"
    provider_name = "none"

    try:

        context = mcp.call_tool_value(
            "Light Agent",
            "get_light_context",
            {},
            tool_instance=tools,
        )

        state = (
            decode(BulbState, context["state"])
            if context["state"]
            else None
        )

        direct_target = explicit_light_target(
            original_text,
            state,
        )

        # Relative Kelvin commands must be resolved from the live bulb
        # state. Never send an unresolved relative request to the LLM.
        relative_kelvin_request = bool(_RELATIVE_K_RE.search(original_text))

        if direct_target is None and relative_kelvin_request:

            status = "needs_details"

            if state is None or not state.mired:
                message = (
                    "目前無法讀取燈泡的現有色溫，"
                    "因此不能安全計算相對調整量；請確認燈泡與 MQTT 回報。"
                )
            else:
                message = (
                    "依目前色溫與調整幅度計算，目標可能超出燈泡支援的 "
                    "2700K–6500K 範圍，請縮小調整幅度或直接指定目標色溫。"
                )

        # Direction-only requests need a user-specified amount or final target.
        elif direct_target is None and _needs_kelvin_target(original_text):

            status = "needs_details"
            message = (
                "請說明色溫要增加或降低多少 K，"
                "例如「往上調 500K」；也可以直接指定 3000K、4000K 或 6500K。"
                "色溫越低越暖黃，越高越冷白。"
            )

        elif direct_target is not None:

            if not context["fresh"]:

                status = "needs_details"
                message = (
                    "目前沒有收到新鮮的燈泡狀態，"
                    "尚未送出照明指令；請確認燈泡與 MQTT 連線。"
                )

            else:

                result = mcp.call_tool_value(
                    "Light Agent",
                    "submit_light_action",
                    direct_target,
                    tool_instance=tools,
                    run_id=run_id,
                    provider="deterministic-explicit-light-parser",
                    device_id="bulb-1",
                    summary="依使用者明確指定的亮度及色溫執行控制",
                )

                status = "completed"

                changes = []

                if "brightness_percent" in direct_target:
                    changes.append(
                        f"亮度目標設為 "
                        f"{direct_target['brightness_percent']}%"
                    )

                if "kelvin" in direct_target:
                    changes.append(
                        f"色溫目標設為 "
                        f"{direct_target['kelvin']}K"
                    )

                message = (
                    "已將客廳燈泡"
                    + "、".join(changes)
                    + "；控制指令已送出，正在等待設備回報（等待燈泡設備回報確認）。"
                    "請查看下方客廳燈狀態卡。"
                )

                provider_name = "deterministic-explicit-light-parser"

        else:

            messages = [
                {
                    "role": "system",
                    "content": LIGHT_SYSTEM,
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "text": original_text,
                            "tool_result": context,
                        },
                        ensure_ascii=False,
                    ),
                },
            ]

            while True:

                reply = await budget.complete(
                    messages,
                    repo,
                    run_id,
                )

                provider_name = (
                    budget.providers[budget.index].name
                    if budget.providers
                    else "none"
                )

                try:

                    if len(reply) > 65536:
                        raise ValueError("模型回覆過大")

                    action = json.loads(reply)

                    if not isinstance(action, dict):
                        raise ValueError("回覆須為 JSON 物件")

                    if (
                        set(action) == {"ask"}
                        and isinstance(action["ask"], str)
                    ):
                        status = "needs_details"
                        message = action["ask"]
                        break

                    if (
                        set(action)
                        != {"tool", "arguments", "summary"}
                        or not isinstance(action["summary"], str)
                    ):
                        raise ValueError("工具動作格式不符")

                    result = mcp.call_tool_value(
                        "Light Agent",
                        action["tool"],
                        action["arguments"],
                        tool_instance=tools,
                        run_id=run_id,
                        provider=provider_name,
                        device_id="bulb-1",
                        summary=action["summary"],
                    )

                    messages.extend(
                        [
                            {
                                "role": "assistant",
                                "content": reply,
                            },
                            {
                                "role": "user",
                                "content": json.dumps(
                                    {"tool_result": result},
                                    ensure_ascii=False,
                                ),
                            },
                        ]
                    )

                    if action["tool"] == "submit_light_action":
                        status = "completed"
                        message = result["message"]
                        break

                except (
                    ValueError,
                    TypeError,
                    KeyError,
                    OverflowError,
                    RecursionError,
                ) as error:

                    if not isinstance(error, MCPToolExecutionError):
                        repo.log_tool(
                            run_id,
                            {
                                "protocol": "MCP",
                                "role": "Light Agent",
                                "error": str(error),
                            },
                        )

                    messages.append(
                        {
                            "role": "user",
                            "content": json.dumps(
                                {"error": str(error)},
                                ensure_ascii=False,
                            ),
                        }
                    )

    except (
        ProviderUnavailable,
        ProviderConfigurationError,
    ) as error:

        message = str(error)

    except (
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
    ) as error:

        message = str(error)

    run = AgentRun(
        run_id,
        event.event_id,
        status,
        provider_name,
        budget.count - before,
        budget.monotonic() - started,
        message,
        parent_id,
        "bulb-1",
        "Light Agent",
    )

    repo.record_run(run)

    from smart_home_energy.domain import encode

    return encode(run)
