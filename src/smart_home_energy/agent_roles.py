"""Declared agent responsibilities and device-scoped capabilities."""

import json
from pathlib import Path


CAPABILITY_PATHS = (
    Path(__file__).resolve().parents[2] / "config" / "device_capabilities.json",
    Path(__file__).with_name("data") / "device_capabilities.json",
)


def agent_roles():
    source = next((path for path in CAPABILITY_PATHS if path.exists()), None)
    if source is None:
        raise ValueError("找不到 Agent 權責登錄")
    roles = json.loads(source.read_text(encoding="utf-8"))
    required = {"Main Agent", "Washer Agent", "AC Agent", "Light Agent", "Controller"}
    if set(roles) != required:
        raise ValueError("Agent 權責登錄缺少必要角色")
    known = {"washer-1", "ac-1", "bulb-1"}
    for name, policy in roles.items():
        if set(policy) != {"devices", "can", "cannot"} or any(not isinstance(policy[key], list) for key in policy):
            raise ValueError(f"{name} 權責格式不合法")
        if not set(policy["devices"]) <= known or set(policy["can"]) & set(policy["cannot"]):
            raise ValueError(f"{name} 權責內容不合法")
    return roles
