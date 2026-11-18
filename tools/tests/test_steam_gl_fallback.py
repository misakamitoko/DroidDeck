import os
from pathlib import Path
import runpy
import subprocess
import tempfile
import textwrap
import unittest


SESSION = Path(__file__).resolve().parents[1] / "linuxfs/overlay/usr/local/bin/droiddeck-session"
SOURCE = SESSION.read_text()
START = SOURCE.index('    if [ "${DD_VENUS:-0}" = 1 ] && [ -x /usr/local/bin/droiddeck-glprobe ]')
END = SOURCE.index('    rm -f "$HOME/.dd-restart-game"', START)
BLOCK = textwrap.dedent(SOURCE[START:END])
GAME_GPU = runpy.run_path(str(SESSION.with_name("droiddeck-game-env")))["game_gpu"]


class SteamGlFallbackTest(unittest.TestCase):
    def run_probe(self, statuses, venus="1", driver="zink", verbose=False, lavapipe=True):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            probe = root / "droiddeck-glprobe"
            probe.write_text(
                "#!/bin/bash\n"
                'n=$(cat "$DD_DEBUG_DIR/count" 2>/dev/null || echo 0)\n'
                'statuses=($PROBE_STATUSES)\n'
                'status=${statuses[$n]:-1}\n'
                'echo $((n + 1)) > "$DD_DEBUG_DIR/count"\n'
                'if [ "$PROBE_VERBOSE" = 1 ]; then\n'
                '  for ((i=0; i<10000; i++)); do echo "probe line $i"; done\n'
                'fi\n'
                'exit "$status"\n')
            probe.chmod(0o755)
            timeout = root / "timeout"
            timeout.write_text('#!/bin/sh\n[ "$1" = -k ] || exit 99\nshift 3\nexec "$@"\n')
            timeout.chmod(0o755)
            vulkaninfo = root / "vulkaninfo"
            vulkaninfo.write_text("#!/bin/sh\nexit 0\n")
            vulkaninfo.chmod(0o755)
            icd = root / "lvp_icd.json"
            if lavapipe:
                icd.write_text("{}")
            script = (
                'set -u\n_dd_cef=(-cef-force-gpu -cef-use-gl=angle -cef-use-angle=vulkan)\n'
                + BLOCK.replace("/usr/local/bin/droiddeck-glprobe", str(probe)).replace("/usr/share/vulkan/icd.d/lvp_icd.json", str(icd))
                + '\nprintf "DRIVER:%s\\nSOFTWARE:%s\\nLOADER:%s\\nKOPPER:%s\\nZINK:%s\\n" '
                '"$GALLIUM_DRIVER" "${LIBGL_ALWAYS_SOFTWARE:-}" "${MESA_LOADER_DRIVER_OVERRIDE:-}" '
                '"${LIBGL_KOPPER_DRI2:-}" "${ZINK_DEBUG:-}"\n'
                'printf "CEF:%s\\n" "${_dd_cef[@]}"\n'
                'python3 -c \'import json, os; print("ENV:" + json.dumps(dict(os.environ)))\'\n')
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ["PATH"],
                       DD_VENUS=venus, DD_DEBUG_DIR=temp, GALLIUM_DRIVER=driver,
                       MESA_LOADER_DRIVER_OVERRIDE="zink", LIBGL_KOPPER_DRI2="true",
                       VK_DRIVER_FILES="/gpu/venus.json", VK_ICD_FILENAMES="/gpu/venus.json",
                       LIBGL_ALWAYS_SOFTWARE="", ZINK_DEBUG="validation",
                       PROBE_STATUSES=" ".join(map(str, statuses)), PROBE_VERBOSE=str(int(verbose)))
            result = subprocess.run(["bash", "-c", script], env=env, text=True,
                                    capture_output=True, timeout=10)
            self.assertEqual(0, result.returncode, result.stderr)
            count = root / "count"
            return result.stdout, int(count.read_text()) if count.exists() else 0

    def assert_software(self, output):
        for line in ("DRIVER:llvmpipe", "SOFTWARE:1", "LOADER:", "KOPPER:",
                     "CEF:-cef-disable-gpu", "CEF:-cef-disable-gpu-compositing"):
            self.assertIn(line + "\n", output)
        self.assertNotIn("CEF:-cef-force-gpu", output)
        self.assertNotIn("CEF:-cef-use-angle=vulkan", output)

    def environment(self, output):
        import json
        return json.loads(next(line[4:] for line in output.splitlines() if line.startswith("ENV:")))

    def test_success_keeps_gpu_gl_and_cef(self):
        output, count = self.run_probe([0])
        self.assertEqual(1, count)
        self.assertIn("DRIVER:zink\n", output)
        self.assertIn("CEF:-cef-force-gpu\n", output)

    def test_timeout_crash_and_other_failures_fall_back_without_retries(self):
        for status in (124, 137, 139, 126, 127, 2):
            with self.subTest(status=status):
                output, count = self.run_probe([status])
                self.assertEqual(1, count)
                self.assertIn(f"failed with status {status}", output)
                self.assert_software(output)

    def test_ordinary_failure_keeps_working_zink_variant(self):
        output, count = self.run_probe([1, 1, 1, 0])
        self.assertEqual(4, count)
        self.assertIn("DRIVER:zink\n", output)
        self.assertIn("ZINK:\n", output)
        self.assertIn("CEF:-cef-force-gpu\n", output)

    def test_all_variants_failing_switches_gl_and_cef_to_software(self):
        output, count = self.run_probe([1] * 7)
        self.assertEqual(7, count)
        self.assert_software(output)

    def test_variant_timeout_stops_remaining_retries(self):
        output, count = self.run_probe([1, 1, 1, 124, 0])
        self.assertEqual(4, count)
        self.assert_software(output)

    def test_success_without_required_mali_compat_still_falls_back(self):
        output, count = self.run_probe([1] * 6 + [0])
        self.assertEqual(7, count)
        self.assert_software(output)

    def test_verbose_success_does_not_get_sigpipe_from_log_limit(self):
        output, count = self.run_probe([0], verbose=True)
        self.assertEqual(1, count)
        self.assertEqual(60, sum(line.startswith("== glprobe: probe line") for line in output.splitlines()))
        self.assertIn("DRIVER:zink\n", output)

    def test_non_venus_or_software_path_is_unchanged(self):
        for venus, driver in (("0", "zink"), ("1", "llvmpipe")):
            with self.subTest(venus=venus, driver=driver):
                output, count = self.run_probe([139], venus=venus, driver=driver)
                self.assertEqual(0, count)
                self.assertIn(f"DRIVER:{driver}\n", output)
                self.assertIn("CEF:-cef-force-gpu\n", output)

    def test_software_client_uses_lavapipe_and_games_restore_original_gpu(self):
        output, _ = self.run_probe([124])
        client = self.environment(output)
        self.assertTrue(client["VK_DRIVER_FILES"].endswith("/lvp_icd.json"))
        self.assertEqual(client["VK_DRIVER_FILES"], client["VK_ICD_FILENAMES"])
        game = GAME_GPU(client)
        for key, value in (("GALLIUM_DRIVER", "zink"), ("MESA_LOADER_DRIVER_OVERRIDE", "zink"),
                           ("LIBGL_KOPPER_DRI2", "true"), ("VK_DRIVER_FILES", "/gpu/venus.json"),
                           ("VK_ICD_FILENAMES", "/gpu/venus.json")):
            self.assertEqual(value, game[key])
            self.assertNotIn("DROIDDECK_GAME_" + key, game)
        self.assertNotIn("LIBGL_ALWAYS_SOFTWARE", game)
        self.assertEqual("llvmpipe", client["GALLIUM_DRIVER"])
        chromium = GAME_GPU(client, True)
        self.assertEqual(client["VK_DRIVER_FILES"], chromium["VK_DRIVER_FILES"])
        self.assertEqual("1", chromium["LIBGL_ALWAYS_SOFTWARE"])

    def test_missing_lavapipe_disables_client_vulkan_queries_and_preserves_game_icd(self):
        output, _ = self.run_probe([139], lavapipe=False)
        client = self.environment(output)
        self.assertEqual("/dev/null", client["VK_DRIVER_FILES"])
        self.assertEqual("/dev/null", client["VK_ICD_FILENAMES"])
        self.assertEqual("/gpu/venus.json", GAME_GPU(client)["VK_DRIVER_FILES"])


if __name__ == "__main__":
    unittest.main()
