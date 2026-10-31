"""Default-Chromium detection and profile-dir resolution (hermes_cli.browser_connect).

These exercise the parsers with real command output shapes instead of
patching the detectors themselves, so a change in what macOS / xdg report is
caught here rather than in a user's browser session.
"""
from unittest.mock import patch

import pytest

import hermes_cli.browser_connect as bc


def _ls_dump(*entries: str) -> str:
    return "(\n" + ",\n".join(entries) + "\n)\n"


def _handler(scheme: str, bundle: str) -> str:
    return (
        "    {\n"
        "        LSHandlerPreferredVersions =         {\n"
        '            LSHandlerRoleAll = "-";\n'
        "        };\n"
        f'        LSHandlerRoleAll = "{bundle}";\n'
        f"        LSHandlerURLScheme = {scheme};\n"
        "    }"
    )


def _content_type_handler(uti: str, bundle: str) -> str:
    return (
        "    {\n"
        f'        LSHandlerContentType = "{uti}";\n'
        f'        LSHandlerRoleViewer = "{bundle}";\n'
        "    }"
    )


class TestLaunchServicesHttpsHandler:
    def test_https_entry_wins_over_other_schemes(self):
        dump = _ls_dump(
            _handler("ftp", "com.google.chrome"),
            _handler("https", "com.apple.safari"),
        )
        assert bc._launchservices_https_handler(dump) == "com.apple.safari"

    def test_content_type_registration_is_not_an_https_handler(self):
        dump = _ls_dump(_content_type_handler("public.html", "com.google.chrome"))
        assert bc._launchservices_https_handler(dump) is None

    def test_no_entries_means_no_recorded_handler(self):
        assert bc._launchservices_https_handler("(\n)\n") is None
        assert bc._launchservices_https_handler("") is None

    def test_nested_dictionary_does_not_split_the_entry(self):
        dump = _ls_dump(_handler("https", "com.microsoft.edgemac"))
        assert bc._launchservices_https_handler(dump) == "com.microsoft.edgemac"


class TestDetectDefaultDarwin:
    def _run_with(self, dump: str):
        class _Proc:
            stdout = dump

        return patch.object(bc.subprocess, "run", return_value=_Proc())

    def test_chrome_as_https_handler(self):
        with self._run_with(_ls_dump(_handler("https", "com.google.chrome"))):
            assert bc._detect_default_darwin() == "chrome"

    def test_safari_default_with_chrome_installed_fails_closed(self):
        """The old fallback returned the first installed Chromium app; a
        non-Chromium default must resolve to None even when Chrome exists."""
        dump = _ls_dump(
            _handler("https", "com.apple.safari"),
            _handler("ftp", "com.google.chrome"),
        )
        with self._run_with(dump), \
             patch.object(bc, "chromium_executable", return_value="/Applications/Google Chrome.app/x"):
            assert bc._detect_default_darwin() is None

    def test_no_handler_recorded_fails_closed(self):
        with self._run_with("(\n)\n"), \
             patch.object(bc, "chromium_executable", return_value="/Applications/Google Chrome.app/x"):
            assert bc._detect_default_darwin() is None

    def test_firefox_default_fails_closed(self):
        with self._run_with(_ls_dump(_handler("https", "org.mozilla.firefox"))):
            assert bc._detect_default_darwin() is None

    def test_reader_failure_fails_closed(self):
        with patch.object(bc.subprocess, "run", side_effect=OSError("no defaults")):
            assert bc._detect_default_darwin() is None

    @pytest.mark.parametrize(
        "bundle,expected",
        [
            ("com.google.Chrome", "chrome"),
            ("com.brave.Browser", "brave"),
            ("com.microsoft.edgemac", "edge"),
            ("org.chromium.Chromium", "chromium"),
        ],
    )
    def test_bundle_map(self, bundle, expected):
        with self._run_with(_ls_dump(_handler("https", bundle))):
            assert bc._detect_default_darwin() == expected


class TestDetectDefaultLinux:
    def _run_with(self, output: str):
        class _Proc:
            stdout = output

        return patch.object(bc.subprocess, "run", return_value=_Proc())

    @pytest.mark.parametrize(
        "desktop,expected",
        [
            ("google-chrome.desktop", "chrome"),
            ("com.google.Chrome.desktop", "chrome"),
            ("chromium_chromium.desktop", "chromium"),
            ("org.chromium.Chromium.desktop", "chromium"),
            ("brave-browser.desktop", "brave"),
            ("com.brave.Browser.desktop", "brave"),
            ("microsoft-edge.desktop", "edge"),
            ("com.microsoft.Edge.desktop", "edge"),
            ("firefox.desktop", None),
            ("org.mozilla.firefox.desktop", None),
            ("", None),
        ],
    )
    def test_xdg_desktop_names(self, desktop, expected):
        with self._run_with(desktop + "\n"):
            assert bc._detect_default_linux() == expected

    def test_missing_xdg_settings_fails_closed(self):
        with patch.object(bc.subprocess, "run", side_effect=FileNotFoundError("xdg-settings")):
            assert bc._detect_default_linux() is None


class TestDetectDefaultLinuxOmarchyWrapper:
    """Omarchy registers ``omarchy-browser.desktop``, a launcher wrapper, as the
    xdg default. Its own ``omarchy-cmd-default-browser`` names the real
    browser. Detection must follow that indirection and then apply the same
    fail-closed classification — never guess an installed Chromium."""

    @staticmethod
    def _runner(answers: dict):
        calls = []

        def run(argv, **kwargs):
            calls.append(list(argv))
            answer = answers.get(argv[0])
            if isinstance(answer, BaseException):
                raise answer

            class _Proc:
                stdout = answer if isinstance(answer, str) else ""
                returncode = 0 if isinstance(answer, str) else 1

            return _Proc()

        return run, calls

    @pytest.mark.parametrize(
        "resolved,expected",
        [
            ("chromium.desktop\n", "chromium"),
            ("google-chrome.desktop\n", "chrome"),
            ("brave-browser.desktop\n", "brave"),
            ("firefox.desktop\n", None),
            ("google-chrome-beta.desktop\n", bc.UNSUPPORTED_CHANNEL),
            ("omarchy-browser.desktop\n", None),
            ("\n", None),
        ],
    )
    def test_wrapper_is_resolved_then_classified(self, resolved, expected):
        run, calls = self._runner(
            {"xdg-settings": "omarchy-browser.desktop\n", "omarchy-cmd-default-browser": resolved}
        )
        with patch.object(bc.subprocess, "run", side_effect=run):
            assert bc._detect_default_linux() == expected
        assert [c[0] for c in calls] == ["xdg-settings", "omarchy-cmd-default-browser"]

    @pytest.mark.parametrize(
        "failure",
        [FileNotFoundError("omarchy-cmd-default-browser"), bc.subprocess.TimeoutExpired("x", 5), None],
    )
    def test_wrapper_resolution_failure_fails_closed(self, failure):
        run, _ = self._runner({"xdg-settings": "omarchy-browser.desktop\n", "omarchy-cmd-default-browser": failure})
        with patch.object(bc.subprocess, "run", side_effect=run):
            assert bc._detect_default_linux() is None

    def test_non_wrapper_default_never_invokes_resolver(self):
        run, calls = self._runner({"xdg-settings": "chromium.desktop\n"})
        with patch.object(bc.subprocess, "run", side_effect=run):
            assert bc._detect_default_linux() == "chromium"
        assert [c[0] for c in calls] == ["xdg-settings"]


class TestLinuxProfileDir:
    def _env(self, monkeypatch, home):
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    def test_native_path_when_nothing_exists(self, tmp_path, monkeypatch):
        self._env(monkeypatch, tmp_path)
        assert bc.real_profile_data_dir("chromium", "Linux") == str(tmp_path / ".config" / "chromium")

    def test_snap_chromium_profile_is_found(self, tmp_path, monkeypatch):
        self._env(monkeypatch, tmp_path)
        snap = tmp_path / "snap" / "chromium" / "common" / "chromium"
        snap.mkdir(parents=True)
        assert bc.real_profile_data_dir("chromium", "Linux") == str(snap)

    def test_flatpak_chrome_profile_is_found(self, tmp_path, monkeypatch):
        self._env(monkeypatch, tmp_path)
        flatpak = tmp_path / ".var" / "app" / "com.google.Chrome" / "config" / "google-chrome"
        flatpak.mkdir(parents=True)
        assert bc.real_profile_data_dir("chrome", "Linux") == str(flatpak)

    def test_native_profile_wins_when_present(self, tmp_path, monkeypatch):
        self._env(monkeypatch, tmp_path)
        native = tmp_path / ".config" / "BraveSoftware" / "Brave-Browser"
        native.mkdir(parents=True)
        (tmp_path / ".var" / "app" / "com.brave.Browser" / "config" / "BraveSoftware" / "Brave-Browser").mkdir(parents=True)
        assert bc.real_profile_data_dir("brave", "Linux") == str(native)

    def test_xdg_config_home_is_honoured(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("XDG_CONFIG_HOME", "/home/t/.config")
        assert bc.real_profile_data_dir("edge", "Linux") == "/home/t/.config/microsoft-edge"
