"""核对原始业务资料之间的引用与记录形式。"""

import unittest

from inspect_samples import read_samples


class SampleTests(unittest.TestCase):
    def setUp(self):
        self.data = read_samples()

    def test_reported_events_refer_to_original_request(self):
        requests = {row['id']: row for row in self.data['requests']}
        for event in self.data['events']:
            self.assertIn(event['id'], requests)
            self.assertLessEqual(event['event_minute'], event['received'])
        self.assertTrue(any(event['kind'] == '场地停用' for event in self.data['events']))

    def test_activity_spans_more_than_one_slot(self):
        self.assertTrue(any(row['companion'] and row['end']-row['start'] > 45 for row in self.data['requests']))
        self.assertTrue(any(row['kind'] == '候补' for row in self.data['requests']))
