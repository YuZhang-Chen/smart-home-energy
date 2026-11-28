"""Standard Model Context Protocol (MCP) server with role-scoped tools and resources."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from mcp.server import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.stdio import stdio_server
import mcp.types as types

from smart_home_energy.agent_roles import agent_roles
from smart_home_energy.assistant_defaults import preferences
from smart_home_energy.config import HemsSettings
from smart_home_energy.domain import encode, utcnow
from smart_home_energy.repository import Repository
from smart_home_energy.tariffs import PLANS, selected_tariff, source_snapshot

MCP_PROTOCOL_VERSION = "2024-11-05"
SUPPORTED_MCP_PROTOCOL_VERSIONS = {
    MCP_PROTOCOL_VERSION,
    "2025-03-26",
    "2025-06-18",
    types.LATEST_PROTOCOL_VERSION,
}
SERVER_NAME = "smart-home-mcp"
SERVER_VERSION = "1.0.0"

_RESOURCE_SPECS: tuple[dict[str, str], ...] = (
    {
        "uri": "home://state",
        "name": "home_state",
        "description": "全屋 5 台設備即時狀態（ac-1、washer-1、bulb-1、tv-1、curtain-1）、邏輯時鐘、待處理任務與常用偏好",
        "mimeType": "application/json",
    },
    {
        "uri": "home://roles",
        "name": "agent_roles",
        "description": "device_capabilities.json 角色權責與設備範圍登錄表",
        "mimeType": "application/json",
    },
    {
        "uri": "home://tariffs/taipower",
        "name": "taipower_tariff",
        "description": "目前台電費率方案、每月基準用電與費率來源快照",
        "mimeType": "application/json",
    },
    {
        "uri": "device://ac-1/context",
        "name": "ac_context",
        "description": "客廳冷氣 (ac-1) 即時室溫、任務狀態與費率偏好上下文",
        "mimeType": "application/json",
    },
    {
        "uri": "device://washer-1/context",
        "name": "washer_context",
        "description": "客廳洗衣機 (washer-1) 設備狀態、任務狀態與費率偏好上下文",
        "mimeType": "application/json",
    },
    {
        "uri": "device://bulb-1/context",
        "name": "bulb_context",
        "description": "客廳智慧燈泡 (bulb-1) 狀態、新鮮度與控制權限範圍",
        "mimeType": "application/json",
    },
    {
        "uri": "device://tv-1/context",
        "name": "tv_context",
        "description": "客廳智慧電視 (tv-1) 目前狀態、音量範圍與支援輸入源",
        "mimeType": "application/json",
    },
    {
        "uri": "device://curtain-1/context",
        "name": "curtain_context",
        "description": "客廳智慧窗簾 (curtain-1) 位置與每日自動排程清單",
        "mimeType": "application/json",
    },
)


def _tool_def(
    name: str,
    description: str,
    properties: dict[str, Any] | None = None,
    required: list[str] | None = None,
) -> types.Tool:
    return types.Tool(
        name=name,
        description=description,
        inputSchema={
            "type": "object",
            "properties": properties or {},
            "required": required or [],
        },
    )


def _build_role_tools() -> dict[str, list[types.Tool]]:
    return {
        "Main Agent": [
            _tool_def(
                "get_home_state",
                "取得全屋 5 台設備即時狀態、邏輯時鐘、未完成任務與常用偏好",
            ),
            _tool_def(
                "get_agent_roles",
                "查詢各 Agent 的權責登錄與設備操作範圍",
            ),
            _tool_def(
                "delegate_tasks",
                "依設備範圍批次委派 1 至 5 台設備需求給對應 Device Agent",
                {
                    "requests": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 5,
                        "items": {
                            "type": "object",
                            "properties": {
                                "device_id": {
                                    "type": "string",
                                    "enum": ["washer-1", "ac-1", "bulb-1", "tv-1", "curtain-1"],
                                },
                                "text": {"type": "string"},
                                "task_id": {"type": "string"},
                                "expected_version": {"type": "integer"},
                            },
                            "required": ["device_id", "text"],
                        },
                    }
                },
                ["requests"],
            ),
        ],
        "Washer Agent": [
            _tool_def("get_device_context", "一次取得洗衣機設備、任務、偏好、費率與邏輯時鐘"),
            _tool_def("get_home_state", "取得邏輯時鐘、控制服務心跳與當前洗衣任務"),
            _tool_def("get_device_state", "取得洗衣機最新設備狀態與新鮮度"),
            _tool_def("get_task_state", "取得目前洗衣任務狀態與版本"),
            _tool_def("get_tariff", "取得目前洗衣任務的費率快照"),
            _tool_def(
                "find_default_schedule",
                "依預設偏好尋找最佳洗衣排程（24小時內完成並避開安靜時段）",
                {"immediate": {"type": "boolean"}},
            ),
            _tool_def(
                "find_schedule",
                "依指定條件搜尋合法的洗衣排程",
                {
                    "constraints": {"type": ["object", "null"]},
                    "max_cost": {"type": ["number", "null"]},
                },
            ),
            _tool_def(
                "validate_plan",
                "驗證洗衣計畫與限制條件是否合法",
                {
                    "plan": {"type": "object"},
                    "constraints": {"type": ["object", "null"]},
                },
                ["plan"],
            ),
            _tool_def(
                "propose_plan",
                "提出待使用者核准的洗衣排程計畫",
                {
                    "plan": {"type": "object"},
                    "constraints": {"type": "object"},
                    "expected_version": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                ["plan", "constraints", "expected_version"],
            ),
            _tool_def(
                "reschedule_task",
                "在核准範圍內重新排程洗衣任務",
                {
                    "plan": {"type": "object"},
                    "constraints": {"type": "object"},
                    "expected_version": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                ["plan", "constraints", "expected_version"],
            ),
            _tool_def("verify_execution", "查核洗衣機目前運轉進度與執行結果"),
        ],
        "AC Agent": [
            _tool_def("get_device_context", "讀取冷氣室溫、任務狀態、費率與偏好上下文"),
            _tool_def("get_task_state", "讀取目前冷氣任務狀態與版本"),
            _tool_def(
                "prepare_default_plan",
                "依已同意偏好（或指定分鐘數與目標溫度）建立冷氣計畫",
                {
                    "duration_minutes": {"type": ["number", "null"]},
                    "target_c": {"type": ["number", "null"]},
                },
            ),
            _tool_def(
                "find_plan",
                "依指定冷氣時間與溫度條件建立冷氣計畫",
                {"constraints": {"type": "object"}},
                ["constraints"],
            ),
            _tool_def(
                "validate_plan",
                "驗證冷氣計畫溫度、時段與費用估算",
                {"plan": {"type": "object"}},
                ["plan"],
            ),
            _tool_def(
                "propose_plan",
                "提出待使用者核准的冷氣計畫",
                {
                    "plan": {"type": "object"},
                    "expected_version": {"type": "integer"},
                },
                ["plan", "expected_version"],
            ),
        ],
        "Light Agent": [
            _tool_def("get_light_context", "讀取客廳智慧燈泡 (bulb-1) 狀態、新鮮度與操作範圍"),
            _tool_def(
                "submit_light_action",
                "提交客廳智慧燈泡 (bulb-1) 開關、亮度或色溫設定至控制佇列",
                {
                    "device_id": {"type": "string", "enum": ["bulb-1"]},
                    "power": {"type": ["boolean", "null"]},
                    "brightness_percent": {"type": ["number", "null"], "minimum": 0, "maximum": 100},
                    "kelvin": {"type": ["number", "null"], "minimum": 2700, "maximum": 6500},
                    "duration_seconds": {"type": "number", "minimum": 0, "maximum": 30},
                },
            ),
        ],
        "TV Agent": [
            _tool_def("get_tv_context", "讀取客廳智慧電視 (tv-1) 目前狀態與支援輸入源"),
            _tool_def(
                "control_tv",
                "控制客廳智慧電視 (tv-1) 電源、音量、靜音、訊號源或播放狀態",
                {
                    "device_id": {"type": "string", "enum": ["tv-1"]},
                    "power": {"type": ["boolean", "null"]},
                    "volume": {"type": ["integer", "null"], "minimum": 0, "maximum": 100},
                    "muted": {"type": ["boolean", "null"]},
                    "input": {"type": ["string", "null"], "enum": ["HDMI1", "HDMI2", "HDMI3", "HDMI4", None]},
                    "playback": {"type": ["string", "null"], "enum": ["playing", "paused", "stopped", None]},
                },
            ),
        ],
        "Curtain Agent": [
            _tool_def("get_curtain_context", "讀取客廳智慧窗簾 (curtain-1) 位置與排程清單"),
            _tool_def(
                "set_curtain_position",
                "設定客廳智慧窗簾 (curtain-1) 目標開合百分比 (0~100%)",
                {
                    "device_id": {"type": "string", "enum": ["curtain-1"]},
                    "position": {"type": "number", "minimum": 0, "maximum": 100},
                    "duration_seconds": {"type": "number", "minimum": 0, "maximum": 120},
                },
            ),
            _tool_def(
                "stop_curtain_movement",
                "立即停止客廳智慧窗簾 (curtain-1) 移動",
                {"device_id": {"type": "string", "enum": ["curtain-1"]}},
            ),
            _tool_def(
                "schedule_curtain",
                "新增客廳智慧窗簾 (curtain-1) 每日定時開合排程",
                {
                    "device_id": {"type": "string", "enum": ["curtain-1"]},
                    "time": {"type": "string"},
                    "position": {"type": "number", "minimum": 0, "maximum": 100},
                    "enabled": {"type": "boolean"},
                },
            ),
            _tool_def(
                "delete_curtain_schedule",
                "刪除客廳智慧窗簾 (curtain-1) 每日定時開合排程（可依 schedule_id、time 或 delete_all 刪除）",
                {
                    "device_id": {"type": "string", "enum": ["curtain-1"]},
                    "schedule_id": {"type": ["string", "null"]},
                    "time": {"type": ["string", "null"]},
                    "delete_all": {"type": "boolean"},
                },
            ),
            _tool_def(
                "set_curtain_schedule_enabled",
                "啟用或停用客廳智慧窗簾 (curtain-1) 每日定時開合排程（可依 schedule_id 或 time 指定）",
                {
                    "device_id": {"type": "string", "enum": ["curtain-1"]},
                    "enabled": {"type": ["boolean", "null"]},
                    "schedule_id": {"type": ["string", "null"]},
                    "time": {"type": ["string", "null"]},
                },
            ),
        ],
        "Controller": [],
    }


ROLE_TOOL_SPECS: dict[str, list[types.Tool]] = _build_role_tools()


class MCPPermissionError(PermissionError):
    """Raised when an agent attempts a tool or resource outside its declared role scope."""


class MCPResourceNotFoundError(KeyError):
    """Raised when a requested MCP resource URI is not registered."""


class MCPToolExecutionError(ValueError):
    """Raised when call_tool_value fails after already recording the MCP error log."""


class SmartHomeMCPServer:
    """Official MCP server wrapper enforcing device_capabilities.json role scoping."""

    def __init__(self, repo: Repository, hems: HemsSettings | None = None):
        self.repo = repo
        self.hems = hems or HemsSettings.from_env()
        self.server = Server(SERVER_NAME)
        self._register_sdk_handlers()

    def _register_sdk_handlers(self) -> None:
        @self.server.list_resources()
        async def _list_resources() -> list[types.Resource]:
            return self.list_resources_for_role(None)

        @self.server.read_resource()
        async def _read_resource(uri: types.AnyUrl) -> list[ReadResourceContents]:
            result = self.read_resource_for_role(None, str(uri))
            return [
                ReadResourceContents(
                    content=item.text,
                    mime_type=item.mimeType,
                )
                for item in result.contents
                if isinstance(item, types.TextResourceContents)
            ]

        @self.server.list_tools()
        async def _list_tools() -> list[types.Tool]:
            return self.list_tools_for_role(None)

        @self.server.call_tool(validate_input=False)
        async def _call_tool(name: str, arguments: dict[str, Any] | None) -> types.CallToolResult:
            return self.call_tool_for_role(None, name, arguments or {})

    @staticmethod
    def _validate_role(role: str | None) -> dict[str, dict[str, list[str]]]:
        roles = agent_roles()
        if role is not None and role not in roles:
            raise ValueError(f"未知的 Agent 角色：{role}")
        return roles

    def list_resources_for_role(self, role: str | None = None) -> list[types.Resource]:
        roles = self._validate_role(role)
        allowed_devices = set(roles[role]["devices"]) if role else None
        resources: list[types.Resource] = []
        for spec in _RESOURCE_SPECS:
            uri = spec["uri"]
            if role and uri == "home://state" and role not in {"Main Agent", "Controller"}:
                continue
            if uri.startswith("device://") and allowed_devices is not None:
                device_id = uri.split("://", 1)[1].split("/", 1)[0]
                if device_id not in allowed_devices:
                    continue
            resources.append(
                types.Resource(
                    uri=uri,
                    name=spec["name"],
                    description=spec["description"],
                    mimeType=spec["mimeType"],
                )
            )
        return resources

    def build_home_state(self) -> dict[str, Any]:
        from smart_home_energy.curtain import get_status as get_curtain_status
        from smart_home_energy.tv import get_status as get_tv_status

        active = self.repo.active_tasks()
        devices = {device: self.repo.device(device) for device in ("washer-1", "ac-1", "bulb-1")}
        for device, state in devices.items():
            if state and not any(task.device_id == device for task in active):
                devices[device] = {**state, "task_id": None, "run_id": None, "plan_version": 0}
        devices["tv-1"] = get_tv_status()
        devices["curtain-1"] = get_curtain_status(repo=self.repo)
        return {
            "tasks": encode(active),
            "clock": encode(self.repo.clock()),
            "logical_now": encode(self.repo.clock().now()),
            "devices": devices,
            "timezone": "Asia/Taipei",
            "preferences": preferences(self.repo),
        }

    def _build_device_context(
        self,
        device_id: str,
        *,
        task_id: str | None = None,
        root_id: str | None = None,
        original_text: str | None = None,
    ) -> dict[str, Any]:
        if device_id in {"ac-1", "washer-1"}:
            resolved_task = (
                self.repo.task(task_id)
                if task_id
                else self.repo.active_task(device_id)
            )
            state = self.repo.device(device_id)
            fresh = bool(
                state
                and -2 <= (utcnow() - datetime.fromisoformat(state["observed_at"])).total_seconds() <= 10
            )
            return {
                "device_id": device_id,
                "task": encode(resolved_task),
                "device": state,
                "fresh": fresh,
                "logical_now": encode(self.repo.clock().now()),
                "timezone": "Asia/Taipei",
                "tariff": encode(resolved_task.tariff) if resolved_task else encode(selected_tariff(self.repo, self.hems.tariff())),
                "preferences": preferences(self.repo),
            }
        if device_id == "bulb-1":
            from smart_home_energy.light_agent import LightTools

            return LightTools(self.repo, root_id or "mcp", original_text or "").get_light_context()
        if device_id == "tv-1":
            from smart_home_energy.tv import TVTools

            return TVTools(self.repo, root_id or "mcp", original_text or "").get_tv_context()
        if device_id == "curtain-1":
            from smart_home_energy.curtain import CurtainTools

            return CurtainTools(self.repo, root_id or "mcp", original_text or "").get_curtain_context()
        raise MCPResourceNotFoundError(f"找不到設備資源：{device_id}")

    def read_resource_for_role(
        self,
        role: str | None,
        uri: str,
        *,
        task_id: str | None = None,
        root_id: str | None = None,
        original_text: str | None = None,
    ) -> types.ReadResourceResult:
        self._validate_role(role)
        known_uris = {spec["uri"] for spec in _RESOURCE_SPECS}
        if uri not in known_uris:
            raise MCPResourceNotFoundError(f"找不到 MCP 資源：{uri}")

        allowed_uris = {str(item.uri) for item in self.list_resources_for_role(role)}
        if uri not in allowed_uris:
            raise MCPPermissionError(f"MCP 權限拒絕：角色 {role} 無權讀取資源 {uri}")

        if uri == "home://state":
            payload: Any = self.build_home_state()
        elif uri == "home://roles":
            payload = agent_roles()
        elif uri == "home://tariffs/taipower":
            payload = {
                "tariff": encode(selected_tariff(self.repo, self.hems.tariff())),
                "settings": self.repo.meta(
                    "tariff_settings",
                    {"plan": "simulated", "baseline_kwh": {}, "revision": 0},
                ),
                "source": self.repo.meta("tariff_source") or source_snapshot(),
                "plans": {"simulated": "模擬費率", **PLANS},
            }
        elif uri.startswith("device://") and uri.endswith("/context"):
            device_id = uri.split("://", 1)[1].split("/", 1)[0]
            payload = self._build_device_context(
                device_id,
                task_id=task_id,
                root_id=root_id,
                original_text=original_text,
            )
        else:
            raise MCPResourceNotFoundError(f"找不到 MCP 資源：{uri}")

        return types.ReadResourceResult(
            contents=[
                types.TextResourceContents(
                    uri=uri,
                    mimeType="application/json",
                    text=json.dumps(encode(payload), ensure_ascii=False),
                )
            ]
        )

    def read_resource_value(
        self,
        role: str | None,
        uri: str,
        *,
        task_id: str | None = None,
        root_id: str | None = None,
        original_text: str | None = None,
    ) -> Any:
        result = self.read_resource_for_role(
            role,
            uri,
            task_id=task_id,
            root_id=root_id,
            original_text=original_text,
        )
        first = result.contents[0]
        assert isinstance(first, types.TextResourceContents)
        return json.loads(first.text)

    def list_tools_for_role(self, role: str | None = None) -> list[types.Tool]:
        self._validate_role(role)
        if role is not None:
            return list(ROLE_TOOL_SPECS.get(role, []))
        tools: list[types.Tool] = []
        seen_names: set[str] = set()
        for role_tools in ROLE_TOOL_SPECS.values():
            for tool in role_tools:
                if tool.name not in seen_names:
                    seen_names.add(tool.name)
                    tools.append(tool)
        return tools

    def _infer_role_for_tool(self, name: str) -> str | None:
        for role_name, role_tools in ROLE_TOOL_SPECS.items():
            if any(tool.name == name for tool in role_tools):
                return role_name
        return None

    def _execute_tool(
        self,
        role: str,
        name: str,
        arguments: dict[str, Any],
        *,
        tool_instance: Any = None,
        task_id: str | None = None,
        root_id: str | None = None,
        original_text: str | None = None,
        delegate_handler: Callable[[dict[str, Any]], Any] | None = None,
    ) -> Any:
        if role == "Main Agent":
            if name == "get_home_state":
                if arguments:
                    raise ValueError("get_home_state 不接受參數")
                return self.read_resource_value("Main Agent", "home://state")
            if name == "get_agent_roles":
                if arguments:
                    raise ValueError("get_agent_roles 不接受參數")
                return self.read_resource_value("Main Agent", "home://roles")
            if name == "delegate_tasks":
                if delegate_handler is None:
                    raise ValueError("delegate_tasks 需在家庭助理對話工作流程中執行")
                return delegate_handler(arguments)
            raise ValueError(f"Main Agent 不支援工具：{name}")

        if tool_instance is not None:
            return tool_instance.call(name, arguments)

        if role in {"Washer Agent", "AC Agent"}:
            from smart_home_energy.multi_agent import DeviceTools

            target_device = "washer-1" if role == "Washer Agent" else "ac-1"
            resolved_task = (
                self.repo.task(task_id)
                if task_id
                else self.repo.active_task(target_device)
            )
            if resolved_task is None:
                if name == "get_device_context" and not arguments:
                    return self.read_resource_value(role, f"device://{target_device}/context")
                raise ValueError(f"目前沒有進行中的 {target_device} 任務")
            return DeviceTools(self.repo, resolved_task.task_id).call(name, arguments)

        if role == "Light Agent":
            from smart_home_energy.light_agent import LightTools

            return LightTools(self.repo, root_id or "mcp", original_text or "").call(name, arguments)

        if role == "TV Agent":
            from smart_home_energy.tv import TVTools

            return TVTools(self.repo, root_id or "mcp", original_text or "").call(name, arguments)

        if role == "Curtain Agent":
            from smart_home_energy.curtain import CurtainTools

            return CurtainTools(self.repo, root_id or "mcp", original_text or "").call(name, arguments)

        raise ValueError(f"角色 {role} 沒有可執行的工具")

    def call_tool_for_role(
        self,
        role: str | None,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        tool_instance: Any = None,
        run_id: str | None = None,
        provider: str | None = None,
        device_id: str | None = None,
        summary: str | None = None,
        task_id: str | None = None,
        root_id: str | None = None,
        original_text: str | None = None,
        delegate_handler: Callable[[dict[str, Any]], Any] | None = None,
    ) -> types.CallToolResult:
        args = {} if arguments is None else arguments
        effective_device = device_id or (args.get("device_id") if isinstance(args, dict) else None)

        def _log(payload: dict[str, Any]) -> None:
            if run_id is not None:
                self.repo.log_tool(run_id, payload)

        def _error_result(message: str) -> types.CallToolResult:
            entry: dict[str, Any] = {
                "protocol": "MCP",
                "mcp_method": "tools/call",
                "role": role or "unknown",
                "action": {
                    "tool": name,
                    "arguments": args if isinstance(args, dict) else {},
                    "summary": summary or f"MCP tools/call {name}",
                },
                "error": message,
            }
            if provider:
                entry["provider"] = provider
            if effective_device:
                entry["device_id"] = effective_device
            _log(entry)
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=message)],
                isError=True,
            )

        try:
            roles = self._validate_role(role)
            if not isinstance(args, dict):
                return _error_result("工具參數須為 JSON 物件")

            effective_role = role
            if effective_role is None:
                effective_role = self._infer_role_for_tool(name)
                if effective_role is None:
                    return _error_result(f"未知的 MCP 工具：{name}")

            allowed_tools = {tool.name for tool in ROLE_TOOL_SPECS.get(effective_role, [])}
            if name not in allowed_tools:
                return _error_result(f"MCP 權限拒絕：角色 {effective_role} 無權呼叫工具 {name}")

            allowed_devices = set(roles[effective_role]["devices"])
            if "device_id" in args and args["device_id"] is not None:
                if args["device_id"] not in allowed_devices:
                    return _error_result(
                        f"MCP 設備越權攔截：角色 {effective_role} 僅能操作 {sorted(allowed_devices)}，"
                        f"不可操作 {args['device_id']}"
                    )

            if effective_role == "Main Agent" and name == "delegate_tasks":
                reqs = args.get("requests")
                if isinstance(reqs, list):
                    for item in reqs:
                        if isinstance(item, dict) and item.get("device_id") not in allowed_devices:
                            return _error_result(
                                f"MCP 委派越權攔截：Main Agent 不可委派設備 {item.get('device_id')}"
                            )

            result = self._execute_tool(
                effective_role,
                name,
                args,
                tool_instance=tool_instance,
                task_id=task_id,
                root_id=root_id,
                original_text=original_text,
                delegate_handler=delegate_handler,
            )
            encoded = encode(result)
            if effective_device is None and len(allowed_devices) == 1:
                effective_device = next(iter(allowed_devices))

            entry: dict[str, Any] = {
                "protocol": "MCP",
                "mcp_method": "tools/call",
                "role": effective_role,
                "action": {
                    "tool": name,
                    "arguments": args,
                    "summary": summary or f"MCP tools/call {name}",
                },
                "result": encoded,
            }
            if provider:
                entry["provider"] = provider
            if effective_device:
                entry["device_id"] = effective_device
            _log(entry)

            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=json.dumps(encoded, ensure_ascii=False),
                    )
                ],
                structuredContent=encoded if isinstance(encoded, dict) else None,
                isError=False,
            )
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError, MCPPermissionError, MCPResourceNotFoundError) as error:
            return _error_result(str(error))

    def call_tool_value(
        self,
        role: str,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        tool_instance: Any = None,
        run_id: str | None = None,
        provider: str | None = None,
        device_id: str | None = None,
        summary: str | None = None,
        task_id: str | None = None,
        root_id: str | None = None,
        original_text: str | None = None,
        delegate_handler: Callable[[dict[str, Any]], Any] | None = None,
    ) -> Any:
        call_result = self.call_tool_for_role(
            role,
            name,
            arguments,
            tool_instance=tool_instance,
            run_id=run_id,
            provider=provider,
            device_id=device_id,
            summary=summary,
            task_id=task_id,
            root_id=root_id,
            original_text=original_text,
            delegate_handler=delegate_handler,
        )
        text = ""
        if call_result.content and isinstance(call_result.content[0], types.TextContent):
            text = call_result.content[0].text
        if call_result.isError:
            raise MCPToolExecutionError(text or f"MCP 工具執行失敗：{name}")
        return json.loads(text) if text else None

    def catalog(self) -> dict[str, Any]:
        return {
            "protocol": "MCP",
            "protocol_version": MCP_PROTOCOL_VERSION,
            "latest_protocol_version": types.LATEST_PROTOCOL_VERSION,
            "server": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "resources": [
                item.model_dump(mode="json", exclude_none=True)
                for item in self.list_resources_for_role(None)
            ],
            "tools_by_role": {
                role_name: [
                    tool.model_dump(mode="json", exclude_none=True)
                    for tool in self.list_tools_for_role(role_name)
                ]
                for role_name in agent_roles()
            },
        }

    def handle_jsonrpc(
        self,
        payload: dict[str, Any],
        header_role: str | None = None,
    ) -> dict[str, Any] | None:
        if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0" or not isinstance(payload.get("method"), str):
            return {
                "jsonrpc": "2.0",
                "id": payload.get("id") if isinstance(payload, dict) else None,
                "error": {"code": -32600, "message": "Invalid JSON-RPC 2.0 request"},
            }

        req_id = payload.get("id")
        method = payload["method"]
        params = payload.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": "params must be an object"},
            }

        if method.startswith("notifications/") and req_id is None:
            return None

        role = params.get("role") or header_role
        if role is not None and not isinstance(role, str):
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": "role must be a string"},
            }

        try:
            if method == "initialize":
                requested = params.get("protocolVersion")
                negotiated = (
                    requested
                    if isinstance(requested, str) and requested in SUPPORTED_MCP_PROTOCOL_VERSIONS
                    else MCP_PROTOCOL_VERSION
                )
                result_obj = types.InitializeResult(
                    protocolVersion=negotiated,
                    capabilities=types.ServerCapabilities(
                        resources=types.ResourcesCapability(subscribe=False, listChanged=False),
                        tools=types.ToolsCapability(listChanged=False),
                    ),
                    serverInfo=types.Implementation(name=SERVER_NAME, version=SERVER_VERSION),
                    instructions="Smart Home Energy Role-Scoped MCP Server",
                )
                result = result_obj.model_dump(mode="json", exclude_none=True)
            elif method == "ping":
                result = {}
            elif method == "resources/list":
                resources = self.list_resources_for_role(role)
                result = types.ListResourcesResult(resources=resources).model_dump(
                    mode="json", exclude_none=True
                )
            elif method == "resources/read":
                uri = params.get("uri")
                if not isinstance(uri, str) or not uri:
                    return {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32602, "message": "Missing required parameter: uri"},
                    }
                res = self.read_resource_for_role(role, uri)
                result = res.model_dump(mode="json", exclude_none=True)
            elif method == "tools/list":
                tools = self.list_tools_for_role(role)
                result = types.ListToolsResult(tools=tools).model_dump(
                    mode="json", exclude_none=True
                )
            elif method == "tools/call":
                name = params.get("name")
                if not isinstance(name, str) or not name:
                    return {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32602, "message": "Missing required parameter: name"},
                    }
                arguments = params.get("arguments")
                if arguments is not None and not isinstance(arguments, dict):
                    return {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32602, "message": "arguments must be an object"},
                    }
                call_res = self.call_tool_for_role(role, name, arguments or {})
                result = call_res.model_dump(mode="json", exclude_none=True)
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }
            return {"jsonrpc": "2.0", "id": req_id, "result": result}
        except MCPPermissionError as error:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32003, "message": str(error)},
            }
        except MCPResourceNotFoundError as error:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32002, "message": str(error)},
            }
        except ValueError as error:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": str(error)},
            }


def create_mcp_router(
    repo: Repository,
    hems: HemsSettings | None = None,
    mcp_server: SmartHomeMCPServer | None = None,
) -> APIRouter:
    """Create FastAPI router exposing standard MCP JSON-RPC 2.0 and manifest endpoints."""
    server = mcp_server or SmartHomeMCPServer(repo, hems)
    router = APIRouter(prefix="/api/mcp", tags=["mcp"])

    @router.get("/manifest")
    def mcp_manifest():
        return server.catalog()

    @router.post("")
    async def mcp_jsonrpc(request: Request):
        try:
            payload = await request.json()
        except ValueError as error:
            raise HTTPException(status_code=400, detail="無效的 JSON 請求") from error
        header_role = request.headers.get("X-Agent-Role")
        response = server.handle_jsonrpc(payload, header_role=header_role)
        if response is None:
            return Response(status_code=202)
        return JSONResponse(response)

    return router


async def run_stdio_server(hems: HemsSettings | None = None) -> None:
    """Run SmartHomeMCPServer over stdio for Claude Desktop / MCP Inspector."""
    settings = hems or HemsSettings.from_env()
    repo = Repository(settings.database)
    mcp_server = SmartHomeMCPServer(repo, settings)
    async with stdio_server() as (read_stream, write_stream):
        await mcp_server.server.run(
            read_stream,
            write_stream,
            mcp_server.server.create_initialization_options(),
        )


def main() -> None:
    asyncio.run(run_stdio_server())


if __name__ == "__main__":
    main()
