import asyncio
import os
import sys
import unittest

from dotenv import load_dotenv

from smart_home_energy.agent import configured_providers


load_dotenv()


class LLMConnectionTests(unittest.TestCase):
	@classmethod
	def setUpClass(cls) -> None:
		if os.getenv("RUN_LLM_CONNECT_TESTS", "").lower() not in {"1", "true", "yes"}:
			raise unittest.SkipTest("設定 RUN_LLM_CONNECT_TESTS=1 才執行真實模型連線測試")
		prompt = os.getenv("LLM_CONNECT_PROMPT", "").strip()
		if not prompt and sys.stdin.isatty():
			prompt = input("請輸入 prompt: ").strip()
		cls.prompt = prompt or "Reply with OK only."

	def setUp(self) -> None:
		if os.getenv("RUN_LLM_CONNECT_TESTS", "").lower() not in {"1", "true", "yes"}:
			self.skipTest("設定 RUN_LLM_CONNECT_TESTS=1 才執行真實模型連線測試")

	def _test_provider(self, name: str) -> None:
		providers = {provider.name: provider for provider in configured_providers()}
		provider = providers.get(name)
		if provider is None:
			self.skipTest(f"未設定 {name} provider 的環境變數")

		reply = asyncio.run(provider.complete(
			[{"role": "user", "content": self.prompt}],
			timeout=float(os.getenv("LLM_CONNECT_TIMEOUT", "20")),
		))
		print(f"[{name}] response: {reply}")
		self.assertTrue(reply.strip(), f"{name} 回傳空白內容")

	def test_azure_openai_connection(self) -> None:
		self._test_provider("azure")

	def test_vllm_connection(self) -> None:
		self._test_provider("vllm")


if __name__ == "__main__":
	unittest.main()
