import importlib.util
import unittest
from pathlib import Path


path = Path(__file__).parents[1] / "deploy" / "provision-node.py"
spec = importlib.util.spec_from_file_location("hearth_provision_node", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ProvisionTests(unittest.TestCase):
    def test_generic_node_is_unpaired_and_button_is_opt_in(self):
        config = module.make_config("hearth-kitchen", "http://hearth-hub.local:8080", "Raspberry Pi Zero 2 W")
        self.assertEqual(config["id"], "hearth-kitchen")
        self.assertEqual(config["pairingToken"], "")
        self.assertEqual(config["audioProfile"], "auto")
        self.assertFalse(config["button"]["enabled"])

    def test_spotpear_must_be_requested(self):
        plain = module.make_config("kitchen", "http://hub.local:8080", "Raspberry Pi Zero 2 W")
        hat = module.make_config("kitchen", "http://hub.local:8080", "Raspberry Pi Zero 2 W", "spotpear", 26)
        self.assertNotIn("display", plain)
        self.assertEqual(hat["display"]["type"], "spotpear_st7789")
        self.assertEqual(hat["button"]["gpio"], 26)

    def test_rejects_credentials_in_hub_url(self):
        with self.assertRaises(ValueError):
            module.make_config("kitchen", "http://name:secret@hub.local:8080", "Raspberry Pi 4")


if __name__ == "__main__":
    unittest.main()
