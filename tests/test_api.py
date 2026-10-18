"""HTTP contract and the real request queue/controller boundary, using isolated databases."""
from dataclasses import replace
from datetime import timedelta
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

WEB_AVAILABLE = importlib.util.find_spec("fastapi") is not None
if WEB_AVAILABLE:
    from fastapi.testclient import TestClient
    from smart_home_energy.api import create_app
from smart_home_energy.ac import VirtualAC
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
        self.assertEqual(self.client.post('/api/requests', json=altered).status_code, 422)
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


if __name__ == '__main__':
    unittest.main()
