import hashlib
import json
import math
import re
import uuid


class Fault(Exception):
    def __init__(self, code, message, status=422):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result: raise Fault('duplicate_key', 'Duplicate JSON keys are forbidden', 400)
            result[key] = value
        return result
    try:
        return json.loads(data, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(Fault('nonfinite_json', 'Non-finite JSON is forbidden', 400)))
    except (ValueError, UnicodeError) as error:
        raise Fault('invalid_json', 'Invalid UTF-8 JSON', 400) from error


def identity(value):
    try: return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError): return False


def identifier(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', value) is not None


def new_id(): return str(uuid.uuid4())


def integer(value, minimum=0, maximum=2147483647):
    return type(value) is int and minimum <= value <= maximum


def validate(value, schema, path='parameters'):
    """Explicit supported JSON Schema subset; never silently ignore unknown constraints."""
    supported = {'type', 'properties', 'required', 'additionalProperties', 'items', 'enum', 'minimum', 'maximum',
                 'minLength', 'maxLength', 'minItems', 'maxItems', 'title', 'description', 'default', 'format'}
    if not isinstance(schema, dict) or set(schema) - supported:
        raise Fault('unsupported_schema', f'Unsupported schema at {path}')
    kind = schema.get('type')
    kinds = {'object': lambda v: isinstance(v, dict), 'array': lambda v: isinstance(v, list),
             'string': lambda v: isinstance(v, str), 'boolean': lambda v: type(v) is bool,
             'integer': lambda v: type(v) is int, 'number': lambda v: type(v) in (int, float) and math.isfinite(v),
             'null': lambda v: v is None}
    if kind not in kinds or not kinds[kind](value): raise Fault('schema_type', f'{path} must be {kind}')
    if 'enum' in schema and not any(type(value) is type(x) and value == x for x in schema['enum']):
        raise Fault('schema_enum', f'Unsupported value at {path}')
    if kind == 'object':
        properties = schema.get('properties', {})
        if set(schema.get('required', [])) - value.keys(): raise Fault('schema_required', f'Missing required field at {path}')
        if schema.get('additionalProperties', False) is False and value.keys() - properties.keys():
            raise Fault('unknown_field', f'Unknown field at {path}')
        for key, item in value.items():
            if key in properties: validate(item, properties[key], path+'.'+key)
    elif kind == 'array':
        if not schema.get('minItems', 0) <= len(value) <= schema.get('maxItems', 10000): raise Fault('array_limit', f'Array length out of bounds at {path}')
        for item in value: validate(item, schema.get('items', {}), path+'[]')
    elif kind == 'string':
        if not schema.get('minLength', 0) <= len(value) <= schema.get('maxLength', 4096): raise Fault('string_limit', f'String length out of bounds at {path}')
    elif kind in ('number', 'integer'):
        if value < schema.get('minimum', -math.inf) or value > schema.get('maximum', math.inf): raise Fault('number_limit', f'Value out of bounds at {path}')
    return value


PUBLIC_DEPLOYMENT_FIELDS = {'schema_version', 'deployment_id', 'deployment_version', 'model_id', 'model_version',
    'variant_id', 'variant_version', 'task_type', 'title', 'tags', 'inputs', 'parameters_schema', 'outputs',
    'capabilities', 'availability', 'weights_sha256', 'code_sha256', 'preprocessing_sha256', 'mode', 'snapshot_hash'}


def public_deployment(item):
    if not isinstance(item, dict) or item.get('schema_version') != 1: raise Fault('deployment_schema', 'Deployment schema version 1 required')
    for key in ['deployment_id', 'deployment_version', 'model_id', 'model_version', 'variant_id', 'variant_version']:
        if not identifier(item.get(key)): raise Fault('deployment_id', 'Invalid model, variant or deployment version')
    if not isinstance(item.get('inputs'), list) or not 1 <= len(item['inputs']) <= 8:
        raise Fault('input_schema', 'Declare between one and eight inputs')
    seen = set()
    for spec in item['inputs']:
        if not isinstance(spec, dict) or not identifier(spec.get('id')) or spec['id'] in seen:
            raise Fault('input_schema', 'Input IDs must be unique')
        seen.add(spec['id'])
        if spec.get('kind') not in {'file', 'files', 'text', 'number', 'string', 'enum', 'boolean', 'json'}:
            raise Fault('input_kind', 'Unsupported input kind')
        if spec['kind'] in {'file', 'files'} and (not isinstance(spec.get('formats'), list) or not spec['formats']):
            raise Fault('input_formats', 'File formats must be declared')
    if not isinstance(item.get('parameters_schema'), dict) or not isinstance(item.get('outputs'), list):
        raise Fault('deployment_contract', 'Input parameters and output contracts are required')
    if item.get('availability') not in {'ready', 'disabled', 'not_validated', 'error'}:
        raise Fault('deployment_health', 'Explicit validation state required')
    if item.get('mode') not in {'demo', 'research'}: raise Fault('model_mode', 'Explicit demo or research mode required')
    result = {key: item[key] for key in PUBLIC_DEPLOYMENT_FIELDS if key in item}
    forbidden = {'weights_path', 'adapter_path', 'interpreter', 'python_path', 'password', 'token', 'private_key'}
    def inspect(value):
        if isinstance(value, dict):
            if forbidden & value.keys(): raise Fault('private_metadata', 'Catalog cannot contain local paths or secrets')
            for v in value.values(): inspect(v)
        elif isinstance(value, list):
            for v in value: inspect(v)
    inspect(result)
    return result
