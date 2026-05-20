from __future__ import annotations

import os

from ruamel.yaml import YAML


class EvLabYAML:
    file: str = ""

    def __init__(self, file):
        self.file = file
        self.yaml = YAML()
        self.yaml.preserve_quotes = True

        os.makedirs(os.path.dirname(self.file), exist_ok=True)
        if not os.path.exists(self.file):
            with open(self.file, "w") as f:
                f.write("{}\n")

        with open(self.file, "r") as f:
            self.data = self.yaml.load(f)

    def dump(self, data=None, _stream=None):
        if data is None and self.data is not None:
            data = self.data
        elif self.data is None:
            raise ValueError("No data to dump")

        with open(self.file, "w") as f:
            self.yaml.dump(data, f)
