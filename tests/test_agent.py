import asyncio
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from smart_home_energy.agent import AgentTools, ProviderUnavailable, run_agent, configured_providers
from smart_home_energy.config import HemsSettings
from smart_home_energy.domain import TaskConstraints, WashProgram, Tariff, encode
from smart_home_energy.repository import Repository


class FakeModel:
    name = "fake"

    def __init__(self, replies):
        self.replies = iter(replies)
        self.contexts, self.timeouts = [], []

    async def complete(self, messages, timeout):
        self.contexts.append(list(messages))
        self.timeouts.append(timeout)
        reply = next(self.replies)
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply):
            return reply(messages)
        return json.dumps(reply)


def action(tool, **arguments):
    return {"tool": tool, "arguments": arguments, "summary": "讀取工具結果後繼續"}


class AgentTests(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.repo = Repository(directory / "test.db")
        self.hems = HemsSettings.from_env()
        now = self.repo.clock().now()
        self.c = TaskConstraints(now + timedelta(minutes=5), now + timedelta(hours=12), None, None)
        self.task = self.repo.create_task(self.c, WashProgram(), Tariff())
        self.repo.event("requirement", self.task.task_id, {"text": "請排程"})
        self.event = self.repo.claim_event()

    def test_multiturn_feedback_and_no_approval_tool(self):
        def propose(messages):
            plan = json.loads(messages[-1]["content"])["tool_result"]
            return json.dumps(action("propose_plan", plan=plan, constraints=encode(self.c), expected_version=self.task.version))
        model = FakeModel([action("get_home_state"), action("get_tariff"), action("find_schedule"), propose, {"finish": "計畫待使用者核准"}])
        result = asyncio.run(run_agent(self.repo, self.event, self.hems, [model]))
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.requests, 5)
        self.assertIsNotNone(self.repo.active_task().plan)
        self.assertIsNone(self.repo.active_task().approval)
        self.assertEqual(self.repo.active_task().status, "pending")
        with self.assertRaises(ValueError):
            AgentTools(self.repo, self.task.task_id).call("approve", {})
        with self.assertRaises(ValueError):
            AgentTools(self.repo, self.task.task_id).call("get_task_state", {"task_id": "other"})

    def test_sticky_fallback_preserves_tool_results(self):
        vllm = FakeModel([action("get_task_state"), ProviderUnavailable("vllm 503")])
        vllm.name = "vllm"
        backup = FakeModel([action("get_tariff"), {"finish": "完成查詢"}])
        backup.name = "azure"
        result = asyncio.run(run_agent(self.repo, self.event, self.hems, [vllm, backup]))
        self.assertEqual(result.provider, "azure")
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.requests, 4)
        self.assertTrue(any("tool_result" in m["content"] for m in backup.contexts[0]))
        self.assertEqual(len(vllm.contexts), 2)
        self.assertEqual(len(backup.contexts), 2)

    def test_format_repair_request_and_time_budgets(self):
        model = FakeModel([lambda m: "not json"] * 20)
        result = asyncio.run(run_agent(self.repo, self.event, self.hems, [model]))
        self.assertEqual(result.requests, 15)
        self.assertEqual(result.status, "failed")
        self.assertTrue(all(0 < t <= 20 for t in model.timeouts))
        times = iter([0, 119, 120, 120])
        model = FakeModel([action("get_home_state")])
        result = asyncio.run(run_agent(self.repo, self.event, self.hems, [model], lambda: next(times)))
        self.assertEqual(model.timeouts, [1])
        self.assertEqual(result.status, "failed")

    def test_both_unavailable_and_form_still_works(self):
        vllm = FakeModel([ProviderUnavailable("vllm offline")])
        vllm.name = "vllm"
        azure = FakeModel([ProviderUnavailable("azure offline")])
        azure.name = "azure"
        result = asyncio.run(run_agent(self.repo, self.event, self.hems, [vllm, azure]))
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.provider, "azure")
        self.assertEqual(result.requests, 2)
        tools = AgentTools(self.repo, self.task.task_id)
        plan = tools.find_schedule()
        tools.propose_plan(plan, encode(self.c), self.task.version)
        self.assertIsNotNone(self.repo.active_task().plan)

    def test_actual_single_request_timeout_falls_back(self):
        class SlowModel:
            name = "vllm"
            async def complete(self, messages, timeout):
                await asyncio.sleep(1)
        backup = FakeModel([{"ask": "請補充最晚完成時間"}])
        backup.name = "azure"
        hems = replace(self.hems, request_seconds=.02)
        result = asyncio.run(run_agent(self.repo, self.event, hems, [SlowModel(), backup]))
        self.assertEqual(result.provider, "azure")
        self.assertEqual(result.requests, 2)
        self.assertEqual(result.status, "needs_details")

    def test_provider_url_and_no_key_disclosure(self):
        with patch.dict("os.environ", {"AZURE_OPENAI_ENDPOINT": "https://test.openai.azure.com", "AZURE_OPENAI_API_KEY": "secret", "AZURE_OPENAI_DEPLOYMENT": "deployment", "VLLM_BASE_URL": "http://localhost:8000/v1", "VLLM_MODEL": "served", "VLLM_API_KEY": ""}, clear=True):
            providers = configured_providers()
        self.assertEqual([provider.name for provider in providers], ["vllm", "azure"])
        self.assertEqual(providers[0].base_url, "http://localhost:8000/v1/")
        self.assertEqual(providers[0].model, "served")
        self.assertEqual(providers[1].base_url, "https://test.openai.azure.com/openai/v1/")
        self.assertEqual(providers[1].model, "deployment")

    def test_official_sdk_uses_vllm_first_and_falls_back_only_on_failure(self):
        import httpx
        from openai import AsyncOpenAI

        def handler(request):
            requests.append(request)
            if request.url.host == "vllm.test":
                if scenario == "connection":
                    raise httpx.ConnectError("offline", request=request)
                if isinstance(scenario, int):
                    return httpx.Response(scenario, json={"error": {"message": "unavailable"}})
            model = json.loads(request.content)["model"]
            payload = {"id": "chatcmpl-local", "object": "chat.completion", "created": 0, "model": model,
                       "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps({"finish": "SDK 模擬端點成功"})}}]}
            if request.url.host == "vllm.test" and scenario == "empty":
                payload["choices"] = []
            return httpx.Response(200, json=payload)

        def factory(**kwargs):
            self.assertEqual(kwargs["max_retries"], 0)
            return AsyncOpenAI(**kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

        env = {"AZURE_OPENAI_ENDPOINT": "https://azure.test", "AZURE_OPENAI_API_KEY": "test-only",
               "AZURE_OPENAI_DEPLOYMENT": "deployment", "VLLM_BASE_URL": "http://vllm.test/v1",
               "VLLM_API_KEY": "test-only", "VLLM_MODEL": "served"}
        for scenario in ("healthy", "connection", "empty", 429, 503):
            with self.subTest(scenario=scenario), patch.dict("os.environ", env, clear=True):
                requests = []
                with patch("openai.AsyncOpenAI", side_effect=factory):
                    result = asyncio.run(run_agent(self.repo, self.event, self.hems))
                expected_count = 1 if scenario == "healthy" else 2
                self.assertEqual(result.status, "completed")
                self.assertEqual(result.provider, "vllm" if scenario == "healthy" else "azure")
                self.assertEqual(result.requests, expected_count)
                self.assertEqual(len(requests), expected_count)
                self.assertEqual(str(requests[0].url), "http://vllm.test/v1/chat/completions")
                self.assertEqual(json.loads(requests[0].content)["model"], "served")
                if expected_count == 2:
                    self.assertEqual(str(requests[1].url), "https://azure.test/openai/v1/chat/completions")
                    self.assertEqual(json.loads(requests[1].content)["model"], "deployment")
                self.assertNotIn("test-only", str(self.repo.records("tool_logs")))


if __name__ == "__main__":
    unittest.main()
