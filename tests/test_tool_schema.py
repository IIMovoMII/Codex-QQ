from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'codex_desktop_weixin'))
from tool_schema import validate_arguments


class ToolSchemaTests(unittest.TestCase):
    def test_create_local_and_projectless_match_published_union_and_chinese_text(self):
        schema = {'type': 'object', 'additionalProperties': False,
            'properties': {'prompt': {'type': 'string', 'minLength': 1},
                'target': {'anyOf': [
                    {'type': 'object', 'required': ['type', 'projectId', 'environment'],
                     'additionalProperties': False, 'properties': {
                         'type': {'enum': ['project']}, 'projectId': {'type': 'string'},
                         'environment': {'type': 'object', 'required': ['type'],
                             'additionalProperties': False, 'properties': {'type': {'enum': ['local']}}}}},
                    {'type': 'object', 'required': ['type'], 'additionalProperties': False,
                     'properties': {'type': {'enum': ['projectless']}}}]}}, 'required': ['prompt', 'target']}
        local = {'prompt': '继续检查中文路径😀', 'target': {'type': 'project',
                 'projectId': 'p', 'environment': {'type': 'local'}}}
        self.assertTrue(validate_arguments(local, schema))
        self.assertTrue(validate_arguments({'prompt': '你好', 'target': {'type': 'projectless'}}, schema))
        schema['properties']['target']['anyOf'].append({'$ref': '#/future-cloud-shape'})
        self.assertTrue(validate_arguments(local, schema))
        local['target']['environment']['type'] = 'unknown-new-mode'
        self.assertFalse(validate_arguments(local, schema))

    def test_changed_required_fields_types_limits_and_unsupported_rules_reject(self):
        schema = {'type': 'object', 'required': ['threadId'], 'additionalProperties': False,
                  'properties': {'threadId': {'type': 'string', 'minLength': 1},
                                 'turnLimit': {'type': 'integer', 'minimum': 1, 'maximum': 10}}}
        self.assertTrue(validate_arguments({'threadId': 't', 'turnLimit': 1}, schema))
        for args in ({}, {'threadId': ''}, {'threadId': 't', 'turnLimit': 11},
                     {'threadId': 't', 'turnLimit': True}, {'threadId': 't', 'unexpected': 1}):
            self.assertFalse(validate_arguments(args, schema))
        self.assertFalse(validate_arguments({'threadId': 't'}, {**schema, 'not': {'type': 'object'}}))
        self.assertFalse(validate_arguments({}, None))
