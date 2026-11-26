"""Trusted model boundary. Demo outputs are never scientific SOC estimates."""
from abc import ABC, abstractmethod
import hashlib
import json
import math
from pathlib import Path

FEATURES = ['voltage_v', 'current_a', 'temperature_c']


class ModelAdapter(ABC):
    @abstractmethod
    def metadata(self): ...

    def load(self): return self

    def validate(self):
        meta = self.metadata()
        required = {'model_id', 'version', 'task', 'framework', 'feature_order', 'feature_units',
                    'current_convention', 'window_length', 'stride', 'sample_period_s', 'normalization',
                    'output_unit', 'output_semantics', 'mode', 'applicability'}
        if required - meta.keys(): raise ValueError('Missing mandatory model metadata')
        if set(meta['feature_order']) != set(FEATURES) or len(meta['feature_order']) != 3:
            raise ValueError('Model inputs must be V/I/T only; no reference-label features')
        if meta['feature_units'] != {'voltage_v': 'V', 'current_a': 'A', 'temperature_c': 'degC'}:
            raise ValueError('Model feature units must be explicit SI units')
        for field in ['window_length', 'stride']:
            if type(meta[field]) is not int or not 1 <= meta[field] <= 4096:
                raise ValueError('Invalid bounded model window/stride')
        if not isinstance(meta['sample_period_s'], (int, float)) or not math.isfinite(meta['sample_period_s']) or meta['sample_period_s'] <= 0:
            raise ValueError('Invalid model sample period')
        if meta['current_convention'] != 'positive_discharge':
            raise ValueError('Only explicitly converted positive-discharge input is supported')
        return meta

    def preprocess(self, window):
        return [[sample[name] for name in self.metadata()['feature_order']] for sample in window]

    @abstractmethod
    def predict(self, inputs): ...

    def postprocess(self, output):
        value = float(output)
        if not math.isfinite(value): raise ValueError('Non-finite model output')
        return value


class DemoModel(ModelAdapter):
    def metadata(self):
        return {'model_id': 'demo-vit', 'version': '1.0', 'task': 'soc', 'framework': 'python-deterministic',
                'feature_order': FEATURES, 'feature_units': {'voltage_v': 'V', 'current_a': 'A', 'temperature_c': 'degC'},
                'current_convention': 'positive_discharge', 'window_length': 3, 'stride': 1,
                'sample_period_s': 1.0, 'normalization': {'kind': 'none'}, 'output_unit': 'percent',
                'output_semantics': 'Deterministic communication demonstration; not calibrated SOC',
                'mode': 'demo', 'applicability': 'Protocol tests only; no scientific or safety validity',
                'weights_sha256': None, 'enabled': True}

    def predict(self, inputs):
        means = [sum(row[i] for row in inputs) / len(inputs) for i in range(3)]
        return min(100.0, max(0.0, 100 * (means[0] - 3.0) / 1.2 - 0.05 * means[1] - 0.01 * (means[2] - 25)))


class TrustedTorchScript(ModelAdapter):
    """Opt-in local trusted code only. No uploads, pickle, training or guessed parameters."""
    def __init__(self, metadata, private_root):
        self.meta = dict(metadata)
        self.private_root = Path(private_root).resolve()
        self.model = None

    def metadata(self): return self.meta

    def load(self):
        self.validate()
        if self.meta.get('trusted_local_file') is not True or self.meta.get('framework') != 'torchscript':
            raise ValueError('Explicit trusted local TorchScript model required')
        path = (self.private_root / self.meta['weights_file']).resolve()
        if not path.is_relative_to(self.private_root): raise ValueError('Model path escapes private model root')
        if hashlib.sha256(path.read_bytes()).hexdigest() != self.meta.get('weights_sha256'):
            raise ValueError('Model weight checksum mismatch')
        import torch
        self.model = torch.jit.load(str(path), map_location='cpu').eval()
        return self

    def preprocess(self, window):
        values = super().preprocess(window)
        config = self.meta['normalization']
        if config.get('kind') != 'standard' or len(config.get('mean', [])) != 3 or len(config.get('std', [])) != 3 or any(not math.isfinite(x) or x <= 0 for x in config['std']):
            raise ValueError('Provide exact training-time standardization; no inferred defaults')
        return [[(value - config['mean'][i]) / config['std'][i] for i, value in enumerate(row)] for row in values]

    def predict(self, inputs):
        if self.model is None: raise ValueError('Trusted model has not been loaded')
        import torch
        if self.meta.get('tensor_layout') != 'BLF' or self.meta.get('hidden_state_policy') != 'reset_per_window':
            raise ValueError('This adapter requires explicit BLF layout and reset-per-window semantics')
        with torch.inference_mode():
            result = self.model(torch.tensor([inputs], dtype=torch.float32))
        if result.numel() != 1: raise ValueError('Provide an adapter for multi-output research models')
        return result.item()


class ModelRegistry:
    def __init__(self, model_manifest=None, private_root=None):
        demo = DemoModel().load(); demo.validate()
        self.models = {('demo-vit', '1.0'): demo}
        self.unavailable = []
        if model_manifest and Path(model_manifest).exists():
            for meta in json.loads(Path(model_manifest).read_text(encoding='utf-8')):
                try:
                    model = TrustedTorchScript(meta, private_root)
                    model.validate()
                    if not meta.get('enabled'): raise ValueError('Model is disabled pending explicit acceptance')
                    model.load()
                    self.models[(meta['model_id'], meta['version'])] = model
                except (OSError, ValueError, ImportError, KeyError) as exc:
                    self.unavailable.append({'model_id': meta.get('model_id'), 'enabled': False, 'error': str(exc)})

    def metadata(self): return [model.metadata() for model in self.models.values()] + self.unavailable

    def get(self, model_id, version):
        try: return self.models[(model_id, version)]
        except KeyError as exc: raise ValueError('Requested model/version is unavailable; no silent demo fallback') from exc
