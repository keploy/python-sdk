"""keploy-pytest: joins a pytest suite to keploy mock record and keploy mock
replay. The plugin is keploy_pytest.plugin, which pytest loads by itself once
the package is installed; see its docstring and README.md."""
from .agent import KeployError

__all__ = ["KeployError"]
