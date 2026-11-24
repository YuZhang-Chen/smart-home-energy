"""HTTP contract and the real request queue/controller boundary, using isolated databases."""
from dataclasses import replace
from datetime import timedelta
import importlib.util
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

WEB_AVAILABLE = importlib.util.find_spec("fastapi") is not None
if WEB_AVAILABLE:
    from fastapi.testclient import TestClient
    from smart_home_energy.api import create_app
from smart_home_energy.ac import VirtualAC
from smart_home_energy.bulb import VirtualBulb
from smart_home_energy.bulb_control import BulbController
from smart_home_energy.ac_control import ACController
from smart_home_energy.config import HemsSettings
from smart_home_energy.control import Controller
from smart_home_energy.domain import utcnow
from smart_home_energy.repository import Repository
from smart_home_energy.washer import Washer
from test_control import FakeTransport


@unittest.skipUnless(WEB_AVAILABLE, 'Install .[web] for HTTP tests')
class ApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.repo = Repository(self.directory / 'control.db')
        self.hems = replace(HemsSettings.from_env(), database=self.repo.path, device_database=str(self.directory / 'washer.db'))
        self.real = utcnow()
        self.clock = self.repo.clock()
        self.washer = Washer(Repository(self.hems.device_database), self.clock, self.real)
        self.ac = VirtualAC(Repository(self.directory / 'ac.db'), self.clock, self.real)
        self.repo.save_device(self.washer.state)
        self.repo.save_device(self.ac.state)
        self.control = Controller(self.repo, FakeTransport(), self.hems, self.real)
        self.ac_control = ACController(self.repo, FakeTransport(), self.hems, self.real)
        self.client = self.enterContext(TestClient(create_app(self.repo, self.hems, self.directory / 'no-build')))

    def create_plan(self, ac=False, identifier='plan'):
        start = self.clock.now(self.real) + timedelta(minutes=10)
        end = start + timedelta(hours=2)
        constraints = {'start': start.isoformat(), 'end': end.isoformat()} if ac else {
            'earliest': start.isoformat(), 'deadline': end.isoformat(), 'quiet_start': None, 'quiet_end': None}
        body = {'request_id': identifier, 'kind': 'create_ac_form' if ac else 'create_form',
                'payload': {'constraints': constraints}}
        self.assertEqual(self.client.post('/api/requests', json=body).status_code, 202)
        self.assertIsNone(self.repo.active_task('ac-1' if ac else 'washer-1'))
        (self.ac_control if ac else self.control).process_requests(self.real)
        self.assertEqual(self.client.get('/api/requests/' + identifier).json()['status'], 'done')
        return body, self.repo.active_task('ac-1' if ac else 'washer-1')

    def test_home_returns_null_for_missing_and_keeps_completed_task(self):
        home = self.client.get('/api/home').json()
        self.assertTrue(home['devices']['ac-1']['fresh'])
        self.assertEqual(home['clock']['clock_id'], self.clock.clock_id)
        self.assertEqual(home['preferences']['target_c'], 27)
        self.assertNotIn('VLLM_API_KEY', json.dumps(home))
        _, task = self.create_plan()
        self.repo.update_task(task.task_id, task.version, status='cancelled')
        device = self.client.get('/api/home').json()['devices']['washer-1']
        self.assertIsNone(device['active_task'])
        self.assertEqual(device['latest_task']['status'], 'cancelled')
        self.repo.save_device(replace(self.ac.state, observed_at=self.real - timedelta(seconds=11)))
        self.assertFalse(self.client.get('/api/home').json()['devices']['ac-1']['fresh'])

    def test_queue_approval_version_and_idempotency(self):
        body, task = self.create_plan()
        # Retrying the create after a task exists returns the original request.
        self.assertEqual(self.client.post('/api/requests', json=body).status_code, 202)
        self.assertEqual(len(self.repo.recent_tasks()), 1)
        altered = {**body, 'kind': 'clock', 'payload': {'speed': 60}}
        # `clock` is now a supported durable request kind; reusing the original
        # identifier with different content must report the idempotency conflict.
        self.assertEqual(self.client.post('/api/requests', json=altered).status_code, 409)
        context = {'task_id': task.task_id, 'expected_version': task.version, 'max_cost': task.plan.cost}
        request = {'request_id': 'approve', 'kind': 'approve', 'payload': context}
        self.assertEqual(self.client.post('/api/requests', json=request).status_code, 202)
        self.control.process_requests(self.real)
        self.assertEqual(self.repo.task(task.task_id).status, 'scheduled')
        self.assertEqual(self.client.post('/api/requests', json=request).status_code, 202)
        self.assertEqual(self.client.post('/api/requests', json={**request, 'request_id': 'old-version'}).status_code, 409)
        self.assertEqual(self.client.post('/api/requests', json={**body, 'payload': {'constraints': {**body['payload']['constraints'], 'quiet_start': 120, 'quiet_end': 180}}}).status_code, 409)

    def test_ac_and_washer_independent_and_cancel(self):
        _, ac = self.create_plan(True, 'cold')
        _, washer = self.create_plan(False, 'wash')
        request = {'request_id': 'approve-cold', 'kind': 'approve', 'payload': {'task_id': ac.task_id, 'expected_version': ac.version}}
        self.assertEqual(self.client.post('/api/requests', json=request).status_code, 202)
        self.ac_control.process_requests(self.real)
        self.assertEqual(self.repo.task(ac.task_id).status, 'scheduled')
        response = self.client.post('/api/requests', json={'request_id': 'cancel-wash', 'kind': 'cancel', 'payload': {'task_id': washer.task_id, 'expected_version': washer.version}})
        self.assertEqual(response.status_code, 202)
        self.control.process_requests(self.real)
        self.assertEqual(self.repo.task(washer.task_id).status, 'cancelled')

    def test_validation_and_unknown_routes(self):
        self.assertEqual(self.client.get('/api/requests/missing').status_code, 404)
        self.assertEqual(self.client.get('/api/missing').status_code, 404)
        self.assertEqual(self.client.get('/').status_code, 503)
        naive = {'request_id': 'bad', 'kind': 'create_ac_form', 'payload': {'constraints': {'start': '2026-10-18T12:00:00', 'end': '2026-10-18T13:00:00'}}}
        self.assertEqual(self.client.post('/api/requests', json=naive).status_code, 422)
        _, task = self.create_plan()
        for payload in ({'task_id': task.task_id, 'expected_version': task.version}, {'task_id': task.task_id, 'expected_version': task.version, 'confirmed': False}, {'task_id': task.task_id, 'expected_version': task.version, 'confirmed': 1}):
            self.assertEqual(self.client.post('/api/requests', json={'request_id': 'bad-stop', 'kind': 'abort', 'payload': payload}).status_code, 422)
        body = {'request_id': 'missing-cap', 'kind': 'approve', 'payload': {'task_id': task.task_id, 'expected_version': task.version}}
        self.assertEqual(self.client.post('/api/requests', json=body).status_code, 422)
        self.assertEqual(self.repo.pending_requests(), [])

    def test_domain_errors_remain_request_errors(self):
        _, task = self.create_plan(True)
        self.repo.save_device(replace(self.ac.state, observed_at=self.real - timedelta(seconds=20)))
        request = {'request_id': 'refresh', 'kind': 'schedule_ac_form', 'payload': {
            'task_id': task.task_id, 'expected_version': task.version,
            'constraints': {**json.loads(self.repo.request('plan')['payload'])['constraints']}}}
        self.assertEqual(self.client.post('/api/requests', json=request).status_code, 202)
        self.ac_control.process_requests(self.real)
        self.assertEqual(self.client.get('/api/requests/refresh').json()['status'], 'error')

    def test_static_assets_share_origin_with_api(self):
        static = self.directory / 'dist'
        static.mkdir()
        (static / 'index.html').write_text('<!doctype html><title>living room</title>', encoding='utf-8')
        (static / 'app.js').write_text('const ready = true;', encoding='utf-8')
        with TestClient(create_app(self.repo, self.hems, static)) as client:
            self.assertIn('living room', client.get('/').text)
            self.assertEqual(client.get('/app.js').status_code, 200)
            self.assertEqual(client.get('/api/home').status_code, 200)
            self.assertEqual(client.get('/api/does-not-exist').status_code, 404)

    def test_runtime_proxy_failure_and_diagnostics_are_explicit(self):
        with patch.dict(os.environ, {'HEMS_COPILOT_RUNTIME_URL': 'http://127.0.0.1:1'}):
            with TestClient(create_app(self.repo, self.hems, self.directory / 'no-build')) as client:
                self.assertEqual(client.get('/api/copilotkit/info').status_code, 503)
                services = client.get('/api/diagnostics').json()['services']
                self.assertEqual(services['api'], 'online')
                self.assertEqual(services['copilot_runtime'], 'offline')
                self.assertEqual(services['controller'], 'offline')

    def test_bulb_request_uses_same_durable_queue_and_confirmed_feedback(self):
        bulb = VirtualBulb(Repository(self.directory / 'bulb.db'), self.real)
        self.repo.save_device(bulb.state)
        transport = FakeTransport()
        control = BulbController(self.repo, transport)
        body = {'request_id': 'light-40', 'kind': 'set_bulb', 'payload': {
            'device_id': 'bulb-1', 'brightness_percent': 40, 'kelvin': 2700, 'duration_seconds': 0}}
        self.assertEqual(self.client.post('/api/requests', json=body).status_code, 202)
        self.assertEqual(self.client.post('/api/requests', json=body).status_code, 202)
        self.assertEqual(self.client.get('/api/requests/light-40').json()['status'], 'pending')
        self.assertIsNone(self.client.get('/api/requests/light-40').json()['action'])
        control.tick(self.real)
        self.assertEqual(len(transport.sent), 1)
        self.assertEqual(self.client.get('/api/requests/light-40').json()['action']['status'], 'dispatched')
        command = transport.sent[0][1]
        transport.inbox.extend([('result', bulb.handle(command, self.real)), ('state', bulb.tick(self.real))])
        control.tick(self.real)
        self.assertEqual(self.client.get('/api/requests/light-40').json()['status'], 'done')
        self.assertEqual(self.client.get('/api/requests/light-40').json()['action']['status'], 'confirmed')
        self.assertEqual(self.client.get('/api/home').json()['devices']['bulb-1']['state']['level'], 102)
        bad = {'request_id': 'bad-light', 'kind': 'set_bulb', 'payload': {
            'device_id': 'bulb-1', 'power': True, 'brightness_percent': 0}}
        self.assertEqual(self.client.post('/api/requests', json=bad).status_code, 422)

    def test_same_origin_curtain_and_tv_endpoints_and_household_agent_integration(self):
        import asyncio
        from smart_home_energy.multi_agent import run_household

        # 1. Same-origin Curtain REST API on 8502 app
        self.assertEqual(self.client.get('/api/curtain/health').status_code, 200)
        curtain_res = self.client.post('/api/curtain/command', json={'command': '半開'})
        self.assertEqual(curtain_res.status_code, 200)
        self.assertEqual(curtain_res.json()['target'], 50.0)

        # 2. Same-origin TV REST API on 8502 app
        self.assertEqual(self.client.get('/api/tv/status').status_code, 200)
        tv_res = self.client.post('/api/tv/command', json={'command': '打開電視並切換到 HDMI 2'})
        self.assertEqual(tv_res.status_code, 200)
        self.assertTrue(tv_res.json()['status']['power'])
        self.assertEqual(tv_res.json()['status']['input'], 'HDMI2')

        # 3. Unified household assistant conversation controlling TV and Curtain
        self.repo.event('household:tv-chat', None, {'root_id': 'tv-chat', 'text': '幫我把電視音量調到 35'})
        tv_run = asyncio.run(run_household(self.repo, self.repo.claim_event(), self.hems, providers=[]))
        self.assertEqual(tv_run.status, 'completed')
        self.assertEqual(self.client.get('/api/tv/status').json()['volume'], 35)

        self.repo.event('household:curtain-chat', None, {'root_id': 'curtain-chat', 'text': '把客廳窗簾全開'})
        curtain_run = asyncio.run(run_household(self.repo, self.repo.claim_event(), self.hems, providers=[]))
        self.assertEqual(curtain_run.status, 'completed')
        self.assertEqual(self.client.get('/api/curtain/status').json()['target'], 100.0)

    def test_open_curtain_and_turn_off_light_multi_agent_and_scene_sync(self):
        import asyncio
        from smart_home_energy import curtain
        from smart_home_energy.multi_agent import run_household
        from test_agent import FakeModel, action

        # Reset curtain to closed (0%) and bulb to ON (100%)
        curtain.move_to(0, duration=0)
        self.assertEqual(self.client.get('/api/curtain/status').json()['position'], 0.0)
        bulb = VirtualBulb(Repository(self.directory / 'bulb_combo.db'), self.real)
        bulb.state = replace(bulb.state, power=True, level=254)
        self.repo.save_device(bulb.state)

        # User says: "開窗簾 並且關燈"
        self.repo.event(
            'household:curtain-and-light',
            None,
            {'root_id': 'curtain-and-light', 'text': '開窗簾 並且關燈'},
        )
        replies = [
            action('get_home_state'),
            action(
                'delegate_tasks',
                requests=[
                    {'device_id': 'curtain-1', 'text': '開窗簾'},
                    {'device_id': 'bulb-1', 'text': '關燈'},
                ],
            ),
            action('set_curtain_position', device_id='curtain-1', position=100, duration_seconds=2.0),
            action('submit_light_action', device_id='bulb-1', power=False, duration_seconds=0),
        ]
        run = asyncio.run(run_household(self.repo, self.repo.claim_event(), self.hems, [FakeModel(replies)]))
        self.assertEqual(run.status, 'completed')
        self.assertEqual(
            run.message,
            'curtain-1：客廳窗簾已執行指令：移動至 全開 (100%)。；bulb-1：照明設定已排入控制佇列，等待設備回報確認',
        )

        # Simulate separate API process reading persisted curtain_state.json and advancing animation
        curtain.state['position'] = 0.0
        curtain.state['target'] = 0.0
        curtain.state['updated_at'] = '1970-01-01T00:00:00+00:00'
        status_after = self.client.get('/api/curtain/status').json()
        self.assertEqual(status_after['target'], 100.0)
        self.assertGreater(status_after['position'], 0.0)

        # Verify bulb controller processes the queued turn-off command and confirms feedback
        transport = FakeTransport()
        control = BulbController(self.repo, transport)
        control.tick(self.real)
        command = transport.sent[0][1]
        transport.inbox.extend([('result', bulb.handle(command, self.real)), ('state', bulb.tick(self.real))])
        control.tick(self.real)
        self.assertFalse(self.repo.device('bulb-1')['power'])

    def test_hierarchical_main_agent_delegates_to_llm_tv_and_curtain_agents_with_scope_enforcement(self):
        import asyncio
        from smart_home_energy.curtain import CurtainTools
        from smart_home_energy.multi_agent import run_household
        from smart_home_energy.tv import TVTools
        from test_agent import FakeModel, action

        # 1. Verify strict device scope boundaries on TVTools and CurtainTools
        tv_tools = TVTools(self.repo, 'scope-check', '開電視')
        with self.assertRaises(ValueError):
            tv_tools.call('control_tv', {'device_id': 'ac-1', 'power': True})
        curtain_tools = CurtainTools(self.repo, 'scope-check', '拉上窗簾')
        with self.assertRaises(ValueError):
            curtain_tools.call('set_curtain_position', {'device_id': 'bulb-1', 'position': 0})

        # 2. Hierarchical Main Agent -> Curtain Agent (LLM) + TV Agent (LLM) on contextual prompt
        self.repo.event(
            'household:cinema-context',
            None,
            {
                'root_id': 'cinema-context',
                'text': '外面陽光太刺眼了幫我擋光，另外想看電影但電視太吵幫我調小聲並切到第二個 HDMI',
            },
        )
        replies = [
            action('get_home_state'),
            action(
                'delegate_tasks',
                requests=[
                    {'device_id': 'curtain-1', 'text': '外面陽光太刺眼了幫我擋光'},
                    {'device_id': 'tv-1', 'text': '想看電影但電視太吵幫我調小聲並切到第二個 HDMI'},
                ],
            ),
            action('set_curtain_position', device_id='curtain-1', position=0, duration_seconds=0),
            action(
                'control_tv',
                device_id='tv-1',
                power=True,
                input='HDMI2',
                volume=15,
                playback='playing',
            ),
        ]
        main_run = asyncio.run(run_household(self.repo, self.repo.claim_event(), self.hems, [FakeModel(replies)]))
        self.assertEqual(main_run.status, 'completed')
        self.assertEqual(main_run.requests, 4)

        # Verify /api/home snapshot reflects both LLM device agent actions
        home_snapshot = self.client.get('/api/home').json()
        self.assertEqual(home_snapshot['devices']['curtain-1']['state']['position'], 0.0)
        self.assertTrue(home_snapshot['devices']['tv-1']['state']['power'])
        self.assertEqual(home_snapshot['devices']['tv-1']['state']['input'], 'HDMI2')
        self.assertEqual(home_snapshot['devices']['tv-1']['state']['volume'], 15)
        self.assertEqual(home_snapshot['devices']['tv-1']['state']['playback'], 'playing')

        # Verify parent-child hierarchy in agent_runs (Main Agent parent -> Curtain Agent & TV Agent children)
        runs = [json.loads(r['data']) for r in self.repo.records('agent_runs', 20)]
        children = [r for r in runs if r.get('parent_id') == main_run.run_id]
        self.assertEqual({c['role'] for c in children}, {'Curtain Agent', 'TV Agent'})
        self.assertEqual({c['device_id'] for c in children}, {'curtain-1', 'tv-1'})
        self.assertTrue(all(c['provider'] == 'fake' and c['requests'] == 1 for c in children))


if __name__ == '__main__':
    unittest.main()
