import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class WindowsLauncherScriptTests(unittest.TestCase):
    def _read(self, name: str) -> str:
        return (ROOT / "scripts" / name).read_text(encoding="utf-8-sig")

    def test_ensure_login_captures_redbook_stderr(self):
        text = self._read("ensure_login.ps1")
        self.assertIn("function Invoke-NodeCaptured", text)
        self.assertIn('$ErrorActionPreference = "Continue"', text)
        self.assertIn('Invoke-NodeCaptured @($CliJs, "whoami", "--json")', text)
        self.assertNotIn('& node $CliJs whoami --json 2>$null | Out-Null', text)
        self.assertIn('Invoke-NodeCaptured @($Exporter, [string]$Port, $CookieFile)', text)

    def test_setup_verification_uses_captured_native_process(self):
        text = self._read("setup_windows.ps1")
        self.assertIn("function Invoke-NodeCaptured", text)
        self.assertIn('Invoke-NodeCaptured @($CliJs, "whoami", "--json")', text)
        self.assertNotIn('& node $CliJs whoami\n', text)
        self.assertIn('Invoke-NativePassthrough "npm"', text)
        self.assertIn('Invoke-NodeCaptured @($Exporter, [string]$CdpPort, $CookieFile)', text)

    def test_manual_login_verification_uses_captured_native_process(self):
        text = self._read("configure_cookie.ps1")
        self.assertIn("function Invoke-NodeCaptured", text)
        self.assertIn('Invoke-NodeCaptured @($CliJs, "whoami", "--json")', text)
        self.assertNotIn('& node $CliJs whoami\n', text)
        self.assertIn('Invoke-NodeCaptured @($Exporter, [string]$CdpPort, $CookieFile)', text)


if __name__ == "__main__":
    unittest.main()
