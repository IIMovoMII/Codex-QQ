"""校验原生工具实时公布的参数格式；不执行代码、不引入额外依赖。"""
from __future__ import annotations

import json
import re
from typing import Any

ANNOTATIONS = {'$schema', '$id', '$comment', 'title', 'description', 'default',
               'examples', 'deprecated', 'readOnly', 'writeOnly'}
SUPPORTED = {'type', 'enum', 'const', 'anyOf', 'oneOf', 'allOf', 'properties',
             'required', 'additionalProperties', 'items', 'minItems', 'maxItems',
             'minLength', 'maxLength', 'pattern', 'minimum', 'maximum',
             'exclusiveMinimum', 'exclusiveMaximum'} | ANNOTATIONS


def validate_arguments(value: dict, schema: dict) -> bool:
    """Unknown validation rules reject only this tool, before any dispatch."""
    try:
        if not isinstance(schema, dict) or len(json.dumps(schema)) > 64 * 1024:
            return False
        return _valid(value, schema, 0)
    except (ValueError, TypeError, RecursionError, re.error):
        return False


def _valid(value: Any, schema: Any, depth: int) -> bool:
    if depth > 12:
        raise ValueError('schema-too-deep')
    if isinstance(schema, bool):
        return schema
    if not isinstance(schema, dict) or schema.keys() - SUPPORTED:
        raise ValueError('unsupported-schema')
    kinds = {'object': isinstance(value, dict), 'array': isinstance(value, list),
             'string': isinstance(value, str), 'boolean': isinstance(value, bool),
             'integer': isinstance(value, int) and not isinstance(value, bool),
             'number': isinstance(value, (int, float)) and not isinstance(value, bool),
             'null': value is None}
    if 'type' in schema:
        types = schema['type'] if isinstance(schema['type'], list) else [schema['type']]
        if not types or any(not isinstance(t, str) or t not in kinds for t in types):
            raise ValueError('unsupported-type')
        if not any(kinds[t] for t in types):
            return False
    if 'enum' in schema and value not in schema['enum']:
        return False
    if 'const' in schema and value != schema['const']:
        return False
    for key in ('anyOf', 'oneOf', 'allOf'):
        if key in schema:
            branches = schema[key]
            if not isinstance(branches, list) or not 1 <= len(branches) <= 32:
                raise ValueError('invalid-union')
            results = []
            for child in branches:
                try:
                    results.append(_valid(value, child, depth + 1))
                except ValueError:
                    if key != 'anyOf':
                        raise
                    # A known matching anyOf branch is enough. An unrelated
                    # future cloud/worktree branch must not block local work.
                    results.append(False)
            if (key == 'anyOf' and not any(results) or key == 'oneOf' and sum(results) != 1
                    or key == 'allOf' and not all(results)):
                return False
    if isinstance(value, dict):
        properties, required = schema.get('properties', {}), schema.get('required', [])
        if (not isinstance(properties, dict) or not isinstance(required, list)
                or any(not isinstance(k, str) for k in required)):
            raise ValueError('invalid-object-schema')
        if set(required) - value.keys():
            return False
        extra = schema.get('additionalProperties', True)
        for key, item in value.items():
            if not _valid(item, properties.get(key, extra), depth + 1):
                return False
    if isinstance(value, list):
        if not schema.get('minItems', 0) <= len(value) <= schema.get('maxItems', float('inf')):
            return False
        if not all(_valid(item, schema.get('items', True), depth + 1) for item in value):
            return False
    if isinstance(value, str):
        if not schema.get('minLength', 0) <= len(value) <= schema.get('maxLength', float('inf')):
            return False
        if 'pattern' in schema and not re.search(schema['pattern'], value):
            return False
    if kinds['number']:
        if not schema.get('minimum', float('-inf')) <= value <= schema.get('maximum', float('inf')):
            return False
        if 'exclusiveMinimum' in schema and value <= schema['exclusiveMinimum']:
            return False
        if 'exclusiveMaximum' in schema and value >= schema['exclusiveMaximum']:
            return False
    return True
