import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "contracts" / "governed-workstation-bootstrap-contract.schema.json"
EXAMPLE_PATHS = [
    ROOT / "contracts" / "examples" / "governed-workstation-bootstrap-pass.example.json",
    ROOT / "contracts" / "examples" / "governed-workstation-bootstrap-warn.example.json",
    ROOT / "contracts" / "examples" / "governed-workstation-bootstrap-blocked.example.json",
]


class TestGovernedWorkstationBootstrapContractSchema(unittest.TestCase):
    def test_schema_has_required_top_level_fields(self) -> None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(
            schema["properties"]["result_kind"]["const"],
            "amof_governed_workstation_bootstrap_contract",
        )
        self.assertIn("doctor_gates", schema["required"])
        self.assertIn("mutation_policy", schema["required"])
        self.assertIn("rollback_policy", schema["required"])
        self.assertFalse(schema["additionalProperties"])

    def test_examples_validate_when_jsonschema_is_available(self) -> None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        examples = [json.loads(path.read_text(encoding="utf-8")) for path in EXAMPLE_PATHS]

        try:
            import jsonschema
        except ImportError:
            self.assertEqual([item["bootstrap_status"] for item in examples], ["PASS", "WARN", "BLOCKED"])
            return

        for example in examples:
            jsonschema.validate(instance=example, schema=schema)


if __name__ == "__main__":
    unittest.main()
