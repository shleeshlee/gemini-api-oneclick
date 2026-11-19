#!/usr/bin/env python3
"""Account 20 only: isolated fixed/random proxy comparison; never restart serving workers."""
import argparse
import copy
import hashlib
import hmac
import ipaddress
import json
import os
import re
from pathlib import Path
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request

SERVING = Path('/www/wwwroot/gemini-api-new')
SOURCE = Path(__file__).resolve().parents[1]
TAGS = ('out-sj4', 'out-family-sj', 'out-sj5')
NAMES = ('oneclick_ip20_proxy', 'oneclick_ip20_fixed', 'oneclick_ip20_multi')
PORTS = (27890, 27891, 27892, 27893, 27860, 27861)


def run(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.PIPE).strip()


def private(path, text):
    with path.open('x') as f:
        os.chmod(path, 0o600)
        f.write(text)


def dotenv(path):
    return {k.strip(): v.strip() for line in path.read_text().splitlines()
            if '=' in line and not line.lstrip().startswith('#') for k, v in [line.split('=', 1)]}


def request(url, key, body=None, timeout=15):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
        headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            body = json.load(exc)
        except ValueError:
            body = {}
        return exc.code, body


def proxy_config(original, log_dir):
    by_tag = {row['tag']: row for row in original['outbounds']}
    if any(tag not in by_tag for tag in TAGS):
        raise RuntimeError('Expected proxy routes are missing')
    inbound = [{'tag': f'ip20-{i}', 'listen': '127.0.0.1', 'port': 27890 + i,
                'protocol': 'http', 'settings': {}} for i in range(4)]
    rules = [{'type': 'field', 'inboundTag': [f'ip20-{i}'], 'outboundTag': tag} for i, tag in enumerate(TAGS)]
    rules.append({'type': 'field', 'inboundTag': ['ip20-3'], 'balancerTag': 'ip20-random'})
    return {'log': {'access': str(log_dir / 'access.log'), 'error': str(log_dir / 'error.log'), 'loglevel': 'warning'},
            'inbounds': inbound, 'outbounds': [copy.deepcopy(by_tag[tag]) for tag in TAGS],
            'routing': {'rules': rules, 'balancers': [{'tag': 'ip20-random', 'selector': list(TAGS),
                                                     'strategy': {'type': 'random'}}]}}


def complete_batch(outcomes):
    return len(outcomes) == 3 and all(row.get('status') == 200 and row.get('has_text') for row in outcomes)


def restore_routing(panel, key, enabled):
    action = 'enable' if enabled else 'disable'
    for _ in range(3):
        try:
            status, _ = request(panel + f'/gateway/{action}/20', key, {})
            code, data = request(panel + '/gateway/status', key)
            row = next(row for row in data.get('containers', []) if row['num'] == 20)
            if status == 200 and code == 200 and row['enabled'] == enabled:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def wait_idle(base, key):
    deadline = time.monotonic() + 100
    while time.monotonic() < deadline:
        try:
            status, data = request(base + '/worker/status', key, timeout=5)
            row = next(row for row in data.get('slots', []) if row['num'] == 20)
            if status == 200 and row.get('active_requests') == 0 and row.get('research_active') == 0:
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--controlled', action='store_true', help='Keep the same client; force A/A/A versus A/B/C, restart both through A')
    args = parser.parse_args()
    root = args.runtime.resolve()
    prefix = 'oneclick_ip20_control_' if args.controlled else 'oneclick_ip20_'
    names = tuple(prefix + suffix for suffix in ('proxy', 'fixed', 'multi'))
    if root.exists() or root == SERVING or SERVING in root.parents or root == SOURCE or SOURCE in root.parents:
        raise RuntimeError('Experiment runtime must be new and separate from serving/source')
    if run('git', '-C', str(SOURCE), 'status', '--porcelain', '--untracked-files=no'):
        raise RuntimeError('Source is not clean')
    existing = set(run('docker', 'ps', '-a', '--format', '{{.Names}}').splitlines())
    if existing.intersection(names):
        raise RuntimeError('Experiment container already exists; refusing reuse')
    for port in PORTS:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', port))
    root.mkdir(mode=0o700)
    (root / 'proxy').mkdir(mode=0o700)
    config = dotenv(SERVING / '.env')
    api_key = config['API_KEY']
    management_key = config.get('WORKER_MANAGEMENT_KEY') or api_key
    panel_key = config.get('COOKIE_MANAGER_PASSWORD', '')
    panel = 'http://127.0.0.1:' + config.get('GATEWAY_PORT', '9880')
    status, panel_data = request(panel + '/gateway/status', panel_key)
    account = next(row for row in panel_data['containers'] if row['num'] == 20)
    if status != 200 or account.get('busy') or not account.get('healthy'):
        raise RuntimeError('Serving account 20 is not healthy and idle')
    route_file = SERVING / 'state/worker-routes.json'
    routes = json.loads(route_file.read_text()) if route_file.exists() else {}
    if routes.get('accounts', {}).get('20', 'http://127.0.0.1:7860') != 'http://127.0.0.1:7860':
        raise RuntimeError('Account 20 route changed; do not use assumed credential source')
    old = json.loads(run('docker', 'inspect', 'gemini_worker'))[0]
    candidate = json.loads(run('docker', 'inspect', 'gemini_worker_candidate'))[0]
    proxy = json.loads(run('docker', 'inspect', 'xray'))[0]
    report = {'started': time.time(), 'source_commit': run('git', '-C', str(SOURCE), 'rev-parse', 'HEAD'),
              'slot': 20, 'controlled': args.controlled, 'original_enabled': account['enabled'], 'requests_per_arm': 3,
              'original_started': {'gemini_worker': old['State']['StartedAt'], 'gemini_worker_candidate': candidate['State']['StartedAt']},
              'egress': {}, 'arms': {}, 'events': []}
    key = os.urandom(32)
    private(root / 'fingerprint.key', key.hex())
    started = []
    restore_needed = False

    def emit(event, **data):
        row = {'time': time.time(), 'event': event, **data}
        report['events'].append(row)
        with (root / 'report.json').open('w') as f:
            os.chmod(root / 'report.json', 0o600)
            json.dump(report, f, indent=2)
        print(json.dumps(row), flush=True)

    def fingerprints(path):
        data = dotenv(path)
        return {name: hmac.new(key, data[name].encode(), hashlib.sha256).hexdigest()
                for name in ('SECURE_1PSID', 'SECURE_1PSIDTS')}

    def health(base):
        deadline = time.monotonic() + 140
        while time.monotonic() < deadline:
            try:
                code, data = request(base + '/slot/20/health', api_key, timeout=3)
                if code == 200 and data.get('status') != 'initializing':
                    return {k: data.get(k) for k in ('status', 'auth_status', 'client_ready', 'last_error_type', 'last_http_status')}
            except (OSError, ValueError):
                pass
            time.sleep(2)
        return {'status': 'experiment_wait_timeout'}

    def begin_worker(arm, port, proxy_port, seed):
        directory = root / arm
        (directory / 'envs').mkdir(mode=0o700, parents=True)
        (directory / 'state/cookie-cache').mkdir(mode=0o700, parents=True)
        private(directory / 'envs/account20.env', seed)
        env = {'HTTP_PROXY': f'http://127.0.0.1:{proxy_port}', 'HTTPS_PROXY': f'http://127.0.0.1:{proxy_port}',
               'NO_PROXY': 'localhost,127.0.0.1', 'API_KEY': api_key, 'WORKER_MANAGEMENT_KEY': management_key,
               'ENVS_DIR': '/app/envs', 'ONECLICK_STATE_DIR': '/app/state', 'GEMINI_COOKIE_PATH': '/app/state/cookie-cache'}
        if any('\n' in value or '\r' in value for value in env.values()):
            raise RuntimeError('Invalid multiline environment')
        private(directory / 'runtime.env', ''.join(f'{k}={v}\n' for k, v in env.items()))
        name = prefix + arm
        command = ['docker', 'run', '-d', '--name', name, '--network', 'host', '--env-file', str(directory / 'runtime.env')]
        mounts = [(directory / 'envs', '/app/envs', 'rw'), (directory / 'state', '/app/state', 'rw'),
                  (SOURCE / 'lib/gemini_webapi', '/app/gemini_webapi', 'ro'), (SOURCE / 'app/parsers', '/app/parsers', 'ro')]
        mounts += [(SOURCE / 'app' / name, '/app/' + name, 'ro') for name in
                   ('worker.py', 'slot.py', 'worker_events.py', 'raw_capture_tracer.py')]
        if args.controlled:
            mounts.append((SOURCE / 'scripts/ip-experiment-worker.py', '/app/ip_experiment_worker.py', 'ro'))
        for host, target, mode in mounts:
            command += ['-v', f'{host}:{target}:{mode}']
        module = 'ip_experiment_worker:app' if args.controlled else 'worker:app'
        command += [old['Image'], '/app/.venv/bin/python', '-m', 'uvicorn', module, '--host', '127.0.0.1', '--port', str(port)]
        started.append(name)
        run(*command)
        return directory, name, f'http://127.0.0.1:{port}'

    def proxy_evidence(arm, offset):
        file = root / 'proxy/access.log'
        if not file.exists():
            return []
        with file.open() as stream:
            stream.seek(offset)
            lines = stream.read().splitlines()
        # No authentication fields or account payloads are present in CONNECT logs.
        return [line for line in lines if 'google.com:443' in line and
                ('[ip20-' in line if args.controlled else f'[ip20-{0 if arm == "fixed" else 3} ' in line)]

    def interrupted(signum, frame):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    try:
        original_proxy = json.loads(Path('/etc/xray/config.json').read_text())
        private(root / 'proxy/config.json', json.dumps(proxy_config(original_proxy, Path('/experiment'))))
        started.append(names[0])
        run('docker', 'run', '-d', '--name', names[0], '--network', 'host',
            '-v', f'{root / "proxy/config.json"}:/etc/xray/config.json:ro',
            '-v', f'{root / "proxy"}:/experiment:rw', proxy['Image'])
        time.sleep(2)
        for i, tag in enumerate(TAGS):
            observed = []
            for _ in range(2):
                body = run('curl', '-fsS', '--max-time', '20', '--proxy', f'http://127.0.0.1:{27890+i}', 'https://api.ipify.org?format=json')
                observed.append(str(ipaddress.ip_address(json.loads(body)['ip'])))
            report['egress'][tag] = observed
            emit('egress_verified', tag=tag, addresses=observed)
        if len(set(report['egress'][TAGS[0]])) != 1 or len({ips[0] for ips in report['egress'].values()}) < 2:
            raise RuntimeError('Fixed egress not stable or multiple routes do not expose distinct public IPs')
        restore_needed = True
        code, _ = request(panel + '/gateway/disable/20', panel_key, {})
        if code != 200:
            raise RuntimeError('Unable to pause account 20 normal routing')
        code, panel_data = request(panel + '/gateway/status', panel_key)
        row = next(row for row in panel_data['containers'] if row['num'] == 20)
        if row['enabled'] or row['busy']:
            raise RuntimeError('Account 20 is not idle after pause')
        seed = (SERVING / 'envs/account20.env').read_text()
        private(root / 'seed.env', seed)
        report['baseline_credentials'] = fingerprints(root / 'seed.env')
        emit('routing_paused', slot=20)
        for arm, port, proxy_port in [('fixed', 27860, 27890), ('multi', 27861, 27893)]:
            if args.controlled:
                proxy_port = 27890
            offset = (root / 'proxy/access.log').stat().st_size
            directory, name, base = begin_worker(arm, port, proxy_port, seed)
            report['arms'][arm] = data = {'requests': []}
            emit('arm_started', arm=arm)
            data['initial_login'] = health(base)
            data['initial_proxy_connections'] = proxy_evidence(arm, offset)
            emit('initial_login', arm=arm, result=data['initial_login'])
            if data['initial_login'].get('auth_status') != 'AVAILABLE':
                data['proxy_connections'] = proxy_evidence(arm, offset)
                emit('arm_aborted_before_requests', arm=arm)
                break
            for index in range(3):
                chat_offset = (root / 'proxy/access.log').stat().st_size
                if args.controlled:
                    code, changed = request(base + '/_experiment/egress', management_key,
                                            {'index': index if arm == 'multi' else 0})
                    data.setdefault('transport_switches', []).append(changed)
                    if code != 200 or not changed.get('same_client') or not changed.get('cookies_unchanged'):
                        raise RuntimeError('Unable to switch only the test transport')
                    if len({row['client_id'] for row in data['transport_switches']}) != 1:
                        raise RuntimeError('Test client changed unexpectedly between requests')
                start = time.monotonic()
                try:
                    code, reply = request(base + '/slot/20/v1/chat/completions', api_key,
                        {'model': 'gemini-flash', 'messages': [{'role': 'user', 'content': 'Reply with exactly OK.'}], 'stream': False}, timeout=90)
                    content = (reply.get('choices') or [{}])[0].get('message', {}).get('content')
                    outcome = {'number': index + 1, 'status': code, 'has_text': bool(content), 'exact_ok': str(content).strip() == 'OK'}
                except Exception as exc:
                    outcome = {'number': index + 1, 'error_type': type(exc).__name__}
                outcome['seconds'] = round(time.monotonic() - start, 3)
                outcome['proxy_connections'] = proxy_evidence(arm, chat_offset)
                data['requests'].append(outcome)
                emit('chat_completed', arm=arm, **{k: v for k, v in outcome.items() if k != 'proxy_connections'})
                if outcome.get('status') != 200 or not outcome.get('has_text'):
                    break
            if not complete_batch(data['requests']) or not wait_idle(base, management_key):
                data['inconclusive'] = 'incomplete_request_batch_or_not_idle'
                emit('arm_aborted_without_restart', arm=arm, reason=data['inconclusive'])
                break
            used_tags = {match.group(1) for row in data['requests'] for line in row['proxy_connections']
                         for match in [re.search(r' -> ([^\]]+)\]', line)] if match}
            data['chat_egress_tags'] = sorted(used_tags)
            data['observed_distinct_chat_ips'] = sorted({ip for tag in used_tags for ip in report['egress'].get(tag, [])})
            if arm == 'multi' and len(data['observed_distinct_chat_ips']) < 2:
                data['inconclusive'] = 'multiple_chat_egress_ips_not_observed'
                emit('multi_ip_condition_not_proven', arm=arm)
            data['before_restart_credentials'] = fingerprints(directory / 'envs/account20.env')
            data['before_restart_health'] = health(base)
            data['before_started'] = json.loads(run('docker', 'inspect', name))[0]['State']['StartedAt']
            restart_offset = (root / 'proxy/access.log').stat().st_size
            run('docker', 'restart', name)
            emit('test_worker_restarted', arm=arm)
            data['after_restart_login'] = health(base)
            data['after_restart_credentials'] = fingerprints(directory / 'envs/account20.env')
            data['after_started'] = json.loads(run('docker', 'inspect', name))[0]['State']['StartedAt']
            data['restart_proxy_connections'] = proxy_evidence(arm, restart_offset)
            data['proxy_connections'] = proxy_evidence(arm, offset)
            emit('after_restart_login', arm=arm, result=data['after_restart_login'],
                 credentials_unchanged=data['before_restart_credentials'] == data['after_restart_credentials'])
            run('docker', 'stop', name)
            if data['after_restart_login'].get('auth_status') != 'AVAILABLE':
                emit('experiment_stopped_after_auth_failure', arm=arm)
                break
        report['serving_credentials_unchanged'] = fingerprints(SERVING / 'envs/account20.env') == report['baseline_credentials']
    except BaseException as exc:
        emit('experiment_error', error_type=type(exc).__name__)
        raise
    finally:
        for name in reversed(started):
            try:
                run('docker', 'stop', name)
            except Exception:
                emit('cleanup_failed', container=name)
        if restore_needed:
            report['routing_restored'] = restore_routing(panel, panel_key, report['original_enabled'])
            report['manual_restore_required'] = not report['routing_restored']
            emit('routing_restore_checked', restored=report['routing_restored'], manual_restore_required=report['manual_restore_required'])
        try:
            current = {name: json.loads(run('docker', 'inspect', name))[0]['State']['StartedAt'] for name in report['original_started']}
            report['serving_workers_not_restarted'] = current == report['original_started']
            code, final_health = request('http://127.0.0.1:7860/slot/20/health', api_key)
            report['serving_final_health'] = {k: final_health.get(k) for k in ('status', 'auth_status', 'last_error_type')}
        except Exception as exc:
            emit('final_diagnostic_error', error_type=type(exc).__name__)
        emit('experiment_finished', serving_workers_not_restarted=report.get('serving_workers_not_restarted'),
             routing_restored=report.get('routing_restored'), serving_health=report.get('serving_final_health'))
        if report.get('manual_restore_required'):
            raise RuntimeError('Account 20 routing restoration failed; manual restoration required')


if __name__ == '__main__':
    main()
