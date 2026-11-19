"""Offline checks for the isolated experiment's routing boundary."""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from fastapi import FastAPI, HTTPException

spec = importlib.util.spec_from_file_location('experiment', Path(__file__).resolve().parents[1] / 'scripts/ip-restart-experiment.py')
experiment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(experiment)
worker_spec = importlib.util.spec_from_file_location('ip_test_worker', Path(__file__).resolve().parents[1] / 'scripts/ip-experiment-worker.py')
test_worker = importlib.util.module_from_spec(worker_spec)
with patch.dict(sys.modules, {'worker': SimpleNamespace(app=FastAPI(), _verify_management_key=lambda: None)}):
    worker_spec.loader.exec_module(test_worker)


class ProxyIsolation(unittest.TestCase):
    def synthetic_slot(self):
        transport = SimpleNamespace(proxies={'all': 'http://127.0.0.1:27890'}, cookies={'synthetic': 'unchanged'})
        audit = SimpleNamespace(snapshot=lambda cookies: dict(cookies), client_id='same-client')
        client = SimpleNamespace(_running=True, client=transport, cookie_audit=audit, proxy='http://127.0.0.1:27890')
        return SimpleNamespace(active_requests=0, state={}, retiring=False, client=client)

    def test_transport_switch_retains_client_and_cookie_jar(self):
        slot = self.synthetic_slot()
        client, transport, cookies = slot.client, slot.client.client, slot.client.client.cookies
        for index in (0, 1, 2):
            result = test_worker.switch_transport(slot, index)
            self.assertTrue(result['same_client'])
            self.assertTrue(result['cookies_unchanged'])
            self.assertIs(slot.client, client)
            self.assertIs(slot.client.client, transport)
            self.assertIs(slot.client.client.cookies, cookies)
            self.assertEqual(transport.proxies, {'all': f'http://127.0.0.1:{27890+index}'})

    def test_switch_rejects_busy_client_and_unlisted_proxy_selection(self):
        slot = self.synthetic_slot()
        slot.active_requests = 1
        with self.assertRaises(HTTPException):
            test_worker.switch_transport(slot, 1)
        slot.active_requests = 0
        for index in (-1, 3, True, 'http://outside.test'):
            with self.assertRaises(HTTPException):
                test_worker.switch_transport(slot, index)
        self.assertEqual(slot.client.client.proxies, {'all': 'http://127.0.0.1:27890'})

    def test_only_test_inbounds_and_selected_outbounds_are_included(self):
        original = {'inbounds': [{'port': 443}], 'routing': {'rules': [{'outboundTag': 'unrelated'}]},
                    'outbounds': [{'tag': tag, 'settings': {'servers': [{'address': 'offline.test'}]}}
                                  for tag in (*experiment.TAGS, 'unrelated')]}
        result = experiment.proxy_config(original, Path('/experiment'))
        self.assertEqual([x['port'] for x in result['inbounds']], [27890, 27891, 27892, 27893])
        self.assertTrue(all(x['listen'] == '127.0.0.1' for x in result['inbounds']))
        self.assertEqual([x['tag'] for x in result['outbounds']], list(experiment.TAGS))
        self.assertEqual(result['routing']['rules'][0]['outboundTag'], 'out-sj4')
        self.assertEqual(result['routing']['rules'][-1]['balancerTag'], 'ip20-random')
        result['outbounds'][0]['settings']['servers'][0]['address'] = 'changed.test'
        self.assertEqual(original['outbounds'][0]['settings']['servers'][0]['address'], 'offline.test')

    def test_missing_expected_egress_is_rejected(self):
        with self.assertRaises(RuntimeError):
            experiment.proxy_config({'outbounds': []}, Path('/experiment'))

    def test_failed_or_incomplete_chat_batch_is_not_restart_eligible(self):
        good = {'status': 200, 'has_text': True}
        self.assertTrue(experiment.complete_batch([good] * 3))
        self.assertFalse(experiment.complete_batch([good] * 2))
        self.assertFalse(experiment.complete_batch([good, good, {'error_type': 'TimeoutError'}]))
        self.assertFalse(experiment.complete_batch([good, good, {'status': 200, 'has_text': False}]))

    def test_restore_retries_and_verifies_actual_enabled_state(self):
        wrong = {'containers': [{'num': 20, 'enabled': False}]}
        restored = {'containers': [{'num': 20, 'enabled': True}]}
        with patch.object(experiment, 'request', side_effect=[(200, {}), (200, wrong), (200, {}), (200, restored)]) as request, patch.object(experiment.time, 'sleep'):
            self.assertTrue(experiment.restore_routing('http://offline', 'synthetic', True))
            self.assertEqual(request.call_count, 4)

    def test_restore_failure_is_reported_after_bounded_retries(self):
        with patch.object(experiment, 'request', side_effect=OSError('offline')) as request, patch.object(experiment.time, 'sleep'):
            self.assertFalse(experiment.restore_routing('http://offline', 'synthetic', True))
            self.assertEqual(request.call_count, 3)

    def test_active_request_must_drain_before_restart(self):
        busy = {'slots': [{'num': 20, 'active_requests': 1, 'research_active': 0}]}
        idle = {'slots': [{'num': 20, 'active_requests': 0, 'research_active': 0}]}
        with patch.object(experiment, 'request', side_effect=[(200, busy), (200, idle)]) as request, patch.object(experiment.time, 'sleep'):
            self.assertTrue(experiment.wait_idle('http://offline', 'synthetic'))
            self.assertEqual(request.call_count, 2)


if __name__ == '__main__':
    unittest.main()
