from importlib import import_module
from pathlib import Path
from pkgutil import iter_modules

from .base import Base

_models_dir = Path(__file__).parent

for _module in iter_modules([str(_models_dir)]):
    module_name = _module.name
    if module_name.startswith("_") or module_name == "base":
        continue
    import_module(f"{__name__}.{module_name}")

__all__ = ["Base"]
