"""Load service classes only when requested, after managed environment setup."""
from importlib import import_module

__all__ = ['AIService', 'ProjectContext', 'FileService', 'ExportService']

_EXPORTS = {
    'AIService': '.ai_service',
    'ProjectContext': '.ai_service',
    'FileService': '.file_service',
    'ExportService': '.export_service',
}


def __getattr__(name):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
    value = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value

