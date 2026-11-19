"""Offline checks for the isolated experiment's routing boundary."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('experiment', Path(__file__).resolve().parents[1] / 'scripts/ip-restart-experiment.py')
experiment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(experiment)


class ProxyIsolation(unittest.TestCase):
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
