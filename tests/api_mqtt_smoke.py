"""Opt-in HTTP -> durable queue -> resident controller -> real MQTT appliance test.

Requires an isolated broker. Optional recovery only restarts a named test container.
All child processes, SQLite files and MQTT topics belong to this test run.
"""
import argparse
from datetime import timedelta
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from uuid import uuid4

import httpx

from smart_home_energy.domain import utcnow
from smart_home_energy.repository import Repository


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18884)
    parser.add_argument('--recovery', action='store_true')
    parser.add_argument('--broker-container', default='hems-phase2-3d-test')
    args = parser.parse_args()
    if args.recovery and not args.broker_container.startswith('hems-phase2-'):
        parser.error('Recovery is restricted to an explicitly named hems-phase2-* test container')
    root = Path(__file__).resolve().parents[1]
    directory = root / 'artifacts' / ('3d-api-smoke-' + uuid4().hex[:8])
    directory.mkdir(parents=True)
    repo = Repository(directory / 'control.db')
    repo.switch_clock(60)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        api_port = listener.getsockname()[1]
    env = {**os.environ, 'MQTT_HOST': '127.0.0.1', 'MQTT_PORT': str(args.port),
           'MQTT_TOPIC': 'test/energy/' + directory.name, 'HEMS_MQTT_ROOT': 'test/hems/' + directory.name,
           'HEMS_DB': repo.path, 'HEMS_DEVICE_DB': str(directory / 'washer.db'),
           'HEMS_AC_DB': str(directory / 'ac.db'), 'HEMS_TRANSPORT': 'mqtt',
           'WASH_MINUTES': '3', 'RINSE_MINUTES': '3', 'SPIN_MINUTES': '3', 'PYTHONIOENCODING': 'utf-8'}
    processes, logs, checks = [], [], {}
    client = httpx.Client(base_url=f'http://127.0.0.1:{api_port}', timeout=4)

    def spawn(name, *extra):
        log = (directory / f'{name}.log').open('w', encoding='utf-8')
        logs.append(log)
        process = subprocess.Popen([sys.executable, '-m', f'smart_home_energy.{name}', *extra], env=env,
                                   cwd=root, stdout=log, stderr=log,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        processes.append(process)

    def home():
        response = client.get('/api/home')
        response.raise_for_status()
        return response.json()

    def wait(predicate, label, timeout=30):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if any(p.poll() is not None for p in processes):
                raise AssertionError(f'Child service exited; see {directory}')
            try:
                value = predicate()
                if value:
                    checks[label] = True
                    print(label + ': passed', flush=True)
                    return value
            except httpx.TransportError:
                pass
            time.sleep(.25)
        raise AssertionError(f'Timeout: {label}; see {directory}')

    def post(kind, payload):
        body = {'request_id': uuid4().hex, 'kind': kind, 'payload': payload}
        response = client.post('/api/requests', json=body)
        assert response.status_code == 202, response.text
        def processed():
            result = client.get('/api/requests/' + body['request_id']).json()
            assert result['status'] != 'error', result
            return result['status'] == 'done'
        wait(processed, kind + ':' + body['request_id'][:6], 10)
        return body

    def create(identifier, delay_minutes=10):
        now = repo.clock().now()
        start = now + timedelta(minutes=delay_minutes)
        if identifier == 'ac-1':
            constraints = {'start': start.isoformat(), 'end': (start + timedelta(minutes=25)).isoformat()}
            kind = 'create_ac_form'
        else:
            constraints = {'earliest': start.isoformat(), 'deadline': (start + timedelta(hours=1)).isoformat(), 'quiet_start': None, 'quiet_end': None}
            kind = 'create_form'
        body = post(kind, {'device_id': identifier, 'constraints': constraints})
        task = home()['devices'][identifier]['active_task']
        assert task['status'] == 'pending', task
        assert client.post('/api/requests', json=body).status_code == 202
        return task

    def approve(task):
        payload = {'task_id': task['task_id'], 'expected_version': task['version']}
        if task['device_id'] == 'washer-1':
            payload['max_cost'] = task['plan']['cost']
        body = post('approve', payload)
        assert client.post('/api/requests', json=body).status_code == 202

    try:
        for name in ('washer', 'ac', 'control'):
            spawn(name)
        spawn('api', '--port', str(api_port))
        wait(lambda: all(d['fresh'] for d in home()['devices'].values()), 'live_http_device_feedback')
        assert client.get('/').status_code == 200
        tasks = [create('washer-1'), create('ac-1')]
        assert not any(json.loads(row['data'])['action'] == 'start' for row in repo.records('commands'))
        checks['unapproved_never_started'] = True
        for task in tasks:
            approve(task)
        wait(lambda: all(d['active_task'] and d['active_task']['status'] == 'running' for d in home()['devices'].values()), 'both_devices_running')
        run_ids = {identifier: device['state']['run_id'] for identifier, device in home()['devices'].items()}
        if args.recovery:
            subprocess.run(['docker', 'stop', args.broker_container], check=True, capture_output=True)
            wait(lambda: all(not d['fresh'] for d in home()['devices'].values()), 'stale_feedback_during_disconnect', 18)
            subprocess.run(['docker', 'start', args.broker_container], check=True, capture_output=True)
            wait(lambda: all(d['fresh'] and d['state']['run_id'] == run_ids[identifier] for identifier, d in home()['devices'].items()), 'broker_recovery_same_runs', 20)
        wait(lambda: all(home()['devices'][task['device_id']]['latest_task']['status'] == 'completed' for task in tasks), 'both_completed_via_http', 55)
        for identifier in run_ids:
            starts = [row for row in repo.records('commands', 100) if json.loads(row['data'])['action'] == 'start' and json.loads(row['data'])['device_id'] == identifier]
            assert len(starts) == 1
        checks['no_duplicate_start_commands'] = True
        task = create('washer-1', 4)
        approve(task)
        wait(lambda: home()['devices']['washer-1']['active_task']['status'] == 'running', 'second_washer_running', 15)
        task = home()['devices']['washer-1']['active_task']
        post('abort', {'task_id': task['task_id'], 'expected_version': task['version'], 'confirmed': True})
        wait(lambda: home()['devices']['washer-1']['latest_task']['status'] == 'aborted', 'manual_abort_via_http', 12)
        report = {'passed': True, 'at': utcnow().isoformat(), 'checks': checks, 'directory': str(directory)}
        (root / 'artifacts/3d-api-mqtt-smoke.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(report), flush=True)
    finally:
        client.close()
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
        for log in logs:
            log.close()
        if args.recovery:
            subprocess.run(['docker', 'start', args.broker_container], capture_output=True)


if __name__ == '__main__':
    main()
