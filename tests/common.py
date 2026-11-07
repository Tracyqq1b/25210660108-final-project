"""Shared test fixtures: temp state DB initialized from the real config."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import yaml  # noqa: E402

from pma import controller  # noqa: E402
from pma.state import StateStore  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "src", "config.yaml")


class WorkflowTestCase(unittest.TestCase):
    def setUp(self):
        with open(CONFIG) as fh:
            self.cfg = yaml.safe_load(fh)
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "state.db")
        self.store = StateStore(self.db_path)
        init = self.cfg["initial_state"]
        self.store.initialize(init["cash"], init["positions"],
                              self.cfg["rules"])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def ctx(self, **kw):
        kw.setdefault("auto_approve", True)
        return controller.Context(self.store, self.cfg, **kw)
