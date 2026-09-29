"""Patch a name across a split package.

After a module becomes a package, each submodule holds its own reference to the
names it imports (`from core.audit import audit_log`), so patching the package
attribute alone never reaches the code that runs. These helpers patch the package
and every loaded submodule that has the attribute.
"""
import sys
from unittest import mock


def patch_in_package(pkg, attr: str, new) -> list:
    """Unstarted patchers for `attr` on `pkg` and all of its loaded submodules."""
    prefix = pkg.__name__ + "."
    mods = [pkg] + [m for n, m in sorted(sys.modules.items()) if n.startswith(prefix) and m]
    patchers = [mock.patch.object(m, attr, new) for m in mods if hasattr(m, attr)]
    if not patchers:
        raise AttributeError(f"{pkg.__name__} has no module defining {attr!r}")
    return patchers
