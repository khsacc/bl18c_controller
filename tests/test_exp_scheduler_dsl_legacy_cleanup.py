"""
Phase 9 dead-code cleanup tests — apps/exp_scheduler/REORGANISATION_PLAN.md.

dsl/api.py's exec()-based legacy path (`_local`/`_ctx()`/`api_context()`/
`DSL_NAMESPACE`) was dead code once every real caller went through
dsl/_registry.py::get_registry() instead. This module pins its removal and
the replacement contract: dsl/_registry.py::dsl_command() now substitutes
every decorated function with a stub that always raises
NotImplementedError, after capturing the real function's signature/docstring
into its CommandSpec.

(runner.py's former GlobalXrdSettings/GlobalLimits/GlobalFollowSettings/
GlobalCameraSettings by-name re-export is NOT covered here — runner.py
still needs these classes internally (e.g. `GlobalXrdSettings()` as a
constructor default), so instead of removing the import outright, Phase 9
switched it to `from . import scheduler_settings` +
`scheduler_settings.GlobalXrdSettings` etc., which drops the by-name
attributes from runner.py's own namespace while keeping the classes
usable internally. See tests/test_exp_scheduler_scheduler_settings.py's
ReExportIdentityTests for that contract.)
"""
from __future__ import annotations

import inspect
import sys
import types
import unittest

try:
    import serial  # noqa: F401
except ModuleNotFoundError:
    sys.modules["serial"] = types.SimpleNamespace(
        Serial=object,
        EIGHTBITS=8,
        PARITY_NONE="N",
        STOPBITS_ONE=1,
    )

from apps.exp_scheduler.dsl import api
from apps.exp_scheduler.dsl._registry import get_registry


class DeadCodeRemovedTests(unittest.TestCase):
    """dsl/api.py's exec()-globals path must be gone entirely — not merely
    unused."""

    def test_api_module_has_no_legacy_exec_path_names(self):
        for name in ("DSL_NAMESPACE", "api_context", "_ctx", "_local"):
            self.assertFalse(
                hasattr(api, name), f"dsl.api.{name} should not exist"
            )


class DslCommandStubContractTests(unittest.TestCase):
    """Every @dsl_command-decorated function in dsl/api.py must be an
    always-raising stub whose signature/docstring still match the
    CommandSpec captured at decoration time — REORGANISATION_PLAN.md Phase 9.
    """

    def test_every_registered_command_is_a_not_implemented_stub(self):
        registry = get_registry()
        self.assertTrue(registry, "registry should not be empty")
        for name, spec in registry.items():
            with self.subTest(command=name):
                fn = getattr(api, name)
                # eval_str=True to match how dsl_command() itself captured
                # spec.signature (dsl/api.py's `from __future__ import
                # annotations` stringifies every annotation; without
                # eval_str=True here the two would differ merely in
                # str-vs-real-type annotations even though functools.wraps
                # correctly threads inspect.signature() through to the same
                # underlying parameter list via __wrapped__).
                self.assertEqual(inspect.signature(fn, eval_str=True), spec.signature)
                self.assertEqual(fn.__doc__, spec.doc)
                with self.assertRaises(NotImplementedError):
                    fn()

    def test_stub_rejects_any_arguments_too(self):
        # The wrapper is (*args, **kwargs) -> raise, so it never has a
        # chance to TypeError on a bad call before reporting
        # NotImplementedError — required + optional args, positional or
        # keyword, all hit the same raise.
        with self.assertRaises(NotImplementedError):
            api.move_absolute(ch=8, position=100)
        with self.assertRaises(NotImplementedError):
            api.wait(1.0, "s")

    def test_stub_signature_is_captured_before_wrapping_not_after(self):
        # A regression this guards against: if dsl_command() ever captured
        # inspect.signature() AFTER substituting the (*args, **kwargs) stub
        # instead of before, every CommandSpec.signature would collapse to
        # (*args, **kwargs) — which would still make the naive "signature
        # matches" assertion above pass trivially. Assert directly that a
        # representative registered signature has real, named parameters.
        spec = get_registry()["move_absolute"]
        param_names = list(spec.signature.parameters)
        self.assertEqual(param_names, ["ch", "position"])


if __name__ == "__main__":
    unittest.main()
