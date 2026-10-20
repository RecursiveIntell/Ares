"""Shared mutation admission behavior; inert leaf provenance/config owners."""
from __future__ import annotations

import os
from pathlib import Path
from types import ModuleType
import tempfile
import unittest
from unittest.mock import patch

from hermes_cli.update_contract import evaluate_update_admission


class AresUpdateAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = ModuleType("hermes_cli.config")
        self.config.detect_install_method = lambda path: "git"
        self.config.is_nix_install_method = lambda method: method in {"nix","nixos","home-manager"}
        self.config.recommended_update_command_for_method = lambda method: "update-"+method
        self.config.format_docker_update_message = lambda: "image update only"
        self.image = ModuleType("hermes_cli.image_provenance")
        self.image.read_image_provenance = lambda: None
        self.mods = patch.dict("sys.modules", {"hermes_cli.config":self.config,"hermes_cli.image_provenance":self.image})
        self.mods.start()
        self.addCleanup(self.mods.stop)
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_ares_managed_flag_refuses_before_inherited_admission(self):
        os.environ["ARES_MANAGED_RUNTIME"] = "1"
        self.config.detect_install_method = lambda path: self.fail("inherited admission reached")
        refusal = evaluate_update_admission(self.root)
        self.assertIsNotNone(refusal)
        self.assertEqual(refusal.code, "ares-managed-runtime")
        self.assertEqual(refusal.update_command, "ares update")

    def test_durable_release_layout_refuses_without_environment_flag(self):
        source = self.root/"releases"/("a"*40)/"source"
        source.mkdir(parents=True)
        (source.parent/"release.json").write_text('{"revision":"'+("a"*40)+'","source":"inert"}')
        refusal = evaluate_update_admission(source)
        self.assertIsNotNone(refusal)
        self.assertEqual(refusal.code, "ares-managed-runtime")

    def test_missing_or_malformed_release_identity_is_not_mutation_permission(self):
        source = self.root/"releases"/("a"*40)/"source"
        source.mkdir(parents=True)
        for data in (None, "{malformed", '{"revision":"'+("b"*40)+'"}'):
            marker = source.parent/"release.json"
            marker.unlink(missing_ok=True)
            if data is not None:
                marker.write_text(data)
            refusal = evaluate_update_admission(source)
            self.assertIsNotNone(refusal)
            self.assertEqual(refusal.code, "ares-managed-runtime")

    def test_ordinary_writable_hermes_checkout_remains_admitted(self):
        self.assertIsNone(evaluate_update_admission(self.root))

    def test_symlinked_valid_hermes_checkout_remains_admitted(self):
        alias = self.root/"checkout-alias"
        alias.symlink_to(self.root, target_is_directory=True)
        self.assertIsNone(evaluate_update_admission(alias))

    def test_missing_source_identity_fails_closed(self):
        with patch.object(self.config, "detect_install_method") as detect:
            refusal = evaluate_update_admission(self.root/"missing")
        self.assertIsNotNone(refusal)
        self.assertEqual(refusal.code, "runtime-identity-unresolved")
        detect.assert_not_called()

    def test_unresolvable_source_identity_fails_closed(self):
        alias = self.root/"loop"
        alias.symlink_to(alias)
        refusal = evaluate_update_admission(alias)
        self.assertIsNotNone(refusal)
        self.assertEqual(refusal.code, "runtime-identity-unresolved")

    def test_docker_nix_and_apt_refusals_remain(self):
        for method in ("docker","nix","apt"):
            with self.subTest(method=method):
                self.config.detect_install_method = lambda path, value=method: value
                refusal = evaluate_update_admission(self.root)
                self.assertIsNotNone(refusal)
                self.assertEqual(refusal.code, method)

    def test_ares_flag_does_not_require_credentials_or_image_marker(self):
        os.environ["ARES_MANAGED_RUNTIME"] = "1"
        self.image.read_image_provenance = lambda: self.fail("image lookup reached")
        self.assertIsNotNone(evaluate_update_admission(self.root))


if __name__ == "__main__":
    unittest.main()

