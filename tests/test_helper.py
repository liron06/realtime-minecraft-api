import importlib.machinery
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


HELPER_PATH = Path(__file__).parents[1] / "deploy" / "realtime-minecraft-control"
loader = importlib.machinery.SourceFileLoader("control_helper", str(HELPER_PATH))
spec = importlib.util.spec_from_loader(loader.name, loader)
helper = importlib.util.module_from_spec(spec)
loader.exec_module(helper)


class HelperTests(unittest.TestCase):
    @patch.object(helper.subprocess, "run")
    def test_subprocess_has_fixed_cwd_and_no_shell(self, run):
        helper.run([helper.DOCKER, "version"], 10)
        _, kwargs = run.call_args
        self.assertEqual(kwargs["cwd"], "/opt/realtime-minecraft")
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(kwargs["env"], helper.SAFE_ENV)

    @patch.object(helper, "compose")
    def test_lifecycle_target_is_fixed(self, compose):
        compose.return_value.returncode = 0
        compose.return_value.stdout = ""
        with patch.object(helper.sys, "argv", ["helper", "restart"]):
            helper.main()
        compose.assert_called_once_with("restart", "minecraft", timeout=120)

    def test_unknown_or_extra_operations_are_rejected(self):
        for args in (["exec", "anything"], ["start", "other"], ["restart", "other"]):
            with self.subTest(args=args), patch.object(helper.sys, "argv", ["helper", *args]):
                with self.assertRaises(SystemExit) as raised:
                    helper.main()
                self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
