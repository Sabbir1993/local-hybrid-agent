"""tests/test_schema_check.py - generic tool-argument validation.

`required` and per-property `type` in a tool schema used to be documentation for the model
rather than enforcement: core/agent_loop/repair.py hand-validates eight tools, and everything
else ran with whatever the model emitted. A comment in execution.py claimed the validation
"covers every tool on every lane" and did not.

These tests pin the generic layer and, importantly, pin that it does NOT become a false
positive machine - a guard that rejects valid calls is worse than no guard, because the model
learns to work around it.

Run: python -m unittest tests.test_schema_check -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.agent_loop import schema_check


def _schema(props=None, required=None):
    """OpenAI function schema shape, as RegisteredTool.schema holds it."""
    return {"type": "function", "function": {"name": "t", "parameters": {
        "type": "object", "properties": props or {}, "required": required or []}}}


class RequiredProperties(unittest.TestCase):
    def test_missing_required_is_rejected(self):
        err = schema_check.check_required("t", {}, _schema({"path": {"type": "string"}}, ["path"]))
        self.assertIsNotNone(err)
        self.assertIn("missing required argument 'path'", err)

    def test_present_required_passes(self):
        self.assertIsNone(schema_check.check_required(
            "t", {"path": "a.py"}, _schema({"path": {"type": "string"}}, ["path"])))

    def test_null_counts_as_missing(self):
        err = schema_check.check_required("t", {"path": None},
                                          _schema({"path": {"type": "string"}}, ["path"]))
        self.assertIsNotNone(err)

    def test_error_says_nothing_was_changed(self):
        # the model must know the call did not partially apply
        err = schema_check.check_required("t", {}, _schema({"path": {"type": "string"}}, ["path"]))
        self.assertIn("Nothing was changed", err)


class TypeEnforcement(unittest.TestCase):
    def test_scalar_where_object_expected(self):
        err = schema_check.check_required("t", {"cfg": "nope"}, _schema({"cfg": {"type": "object"}}))
        self.assertIn("must be a object", err)

    def test_scalar_where_array_expected(self):
        err = schema_check.check_required("t", {"items": "a string"}, _schema({"items": {"type": "array"}}))
        self.assertIn("must be a array", err)

    def test_bool_is_not_an_integer(self):
        """True is an int in Python, but a boolean is never an acceptable count."""
        err = schema_check.check_required("t", {"n": True}, _schema({"n": {"type": "integer"}}))
        self.assertIsNotNone(err)
        self.assertIn("boolean", err)

    def test_bool_is_not_a_number(self):
        err = schema_check.check_required("t", {"x": False}, _schema({"x": {"type": "number"}}))
        self.assertIn("boolean", err)

    def test_int_is_an_acceptable_number(self):
        self.assertIsNone(schema_check.check_required("t", {"x": 3}, _schema({"x": {"type": "number"}})))

    def test_tuple_is_an_acceptable_array(self):
        self.assertIsNone(schema_check.check_required("t", {"a": (1, 2)}, _schema({"a": {"type": "array"}})))


class NoFalsePositives(unittest.TestCase):
    """A guard that fires on valid calls is worse than none."""

    def test_no_schema_means_nothing_to_enforce(self):
        self.assertIsNone(schema_check.check_required("t", {"anything": 1}, {}))
        self.assertIsNone(schema_check.check_required("t", {"x": 1}, None))
        self.assertIsNone(schema_check.check_required("t", {"x": 1}, {"type": "function"}))

    def test_undeclared_properties_are_allowed(self):
        self.assertIsNone(schema_check.check_required(
            "t", {"path": "a", "extra": object()}, _schema({"path": {"type": "string"}})))

    def test_properties_without_a_type_are_not_checked(self):
        self.assertIsNone(schema_check.check_required("t", {"x": 5}, _schema({"x": {"description": "n"}})))

    def test_unknown_type_name_is_ignored(self):
        self.assertIsNone(schema_check.check_required("t", {"x": 5}, _schema({"x": {"type": "wat"}})))

    def test_enum_and_bounds_are_not_enforced_here(self):
        # deliberately left to each tool: several accept a wider set than they advertise
        self.assertIsNone(schema_check.check_required(
            "t", {"mode": "surprising"}, _schema({"mode": {"type": "string", "enum": ["a", "b"]}})))

    def test_additional_properties_false_is_not_enforced(self):
        # MCP servers declare this loosely; enforcing it would break real servers
        self.assertIsNone(schema_check.check_required(
            "t", {"zzz": 1}, {"parameters": {"type": "object", "additionalProperties": False}}))

    def test_bare_function_schema_shape_is_accepted(self):
        self.assertIsNotNone(schema_check.check_required(
            "t", {}, {"parameters": {"type": "object", "properties": {"a": {"type": "string"}},
                                     "required": ["a"]}}))


class RegisteredSchemaLookupIsSafe(unittest.TestCase):
    def test_unregistered_tool_yields_none(self):
        self.assertIsNone(schema_check.registered_schema("definitely_not_a_tool_xyz"))

    def test_none_schema_passes_the_check(self):
        self.assertIsNone(schema_check.check_required("t", {"x": 1}, None))

    def test_lookup_failure_does_not_block_every_tool(self):
        from unittest import mock
        with mock.patch("core.registry.registry", None):
            self.assertIsNone(schema_check.registered_schema("anything"))


class WiredIntoRunTool(unittest.TestCase):
    """End to end through run_tool, with the registry populated the way startup does it."""

    def setUp(self):
        import asyncio
        from core.startup import bootstrap_builtin_tools
        from core.registry import registry
        bootstrap_builtin_tools()
        self._run = asyncio.run
        self.registry = registry

    def _call(self, name, args):
        from core.agent_loop.execution import run_tool
        return self._run(run_tool(name, dict(args)))

    def test_spawn_parallel_agents_rejects_a_string(self):
        if self.registry.get("spawn_parallel_agents") is None:
            self.skipTest("tool not registered")
        out = self._call("spawn_parallel_agents", {"agents": "not-a-list"})
        self.assertTrue(out.startswith("error:"))
        self.assertIn("must be a array", out)

    def test_create_plan_rejects_a_string(self):
        if self.registry.get("create_plan") is None:
            self.skipTest("tool not registered")
        out = self._call("create_plan", {"items": "a string"})
        self.assertIn("must be a array", out)

    def test_missing_path_still_caught_by_the_repair_layer(self):
        """The eight hand-validated tools keep their own stricter messages; both layers must
        reject, and this pins that the new layer did not displace the old one."""
        out = self._call("read_file", {})
        self.assertTrue(out.startswith("error:"))
        self.assertIn("path required", out)


if __name__ == "__main__":
    unittest.main()