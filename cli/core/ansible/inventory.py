import os

import core.util.dir as dir
import core.util.yaml as yaml


class EvLabInventory:
    def __init__(self):
        file = os.path.join(dir.INVENTORY_DIR, "inventory.yaml")
        self.yaml = yaml.EvLabYAML(file)
        if self.yaml.data is None:
            self.yaml.data = {}

    def group_insert(self, group_name, parent_group_name=None):
        if parent_group_name is not None and parent_group_name not in self.yaml.data:
            self.group_insert(parent_group_name)
        elif group_name not in self.yaml.data and parent_group_name is None:
            self.yaml.data[group_name] = {}
        elif (
            group_name not in self.yaml.data
            and parent_group_name is not None
            and parent_group_name in self.yaml.data
        ):
            self.yaml.data[parent_group_name]["children"][group_name] = {}
        self.yaml.dump()

    def host_insert_update(
        self, host_name, group_name, parent_group_name=None, host_vars=None
    ):
        self.group_insert(group_name, parent_group_name)
        if (
            parent_group_name is not None
            and "hosts" not in self.yaml.data[parent_group_name][group_name]
        ):
            self.yaml.data[group_name]["hosts"] = {}
        elif parent_group_name is None and "hosts" not in self.yaml.data[group_name]:
            self.yaml.data[group_name]["hosts"] = {}
        if host_name not in self.yaml.data[group_name]["hosts"]:
            self.yaml.data[group_name]["hosts"][host_name] = {}
        if host_vars is not None:
            self.yaml.data[group_name]["hosts"][host_name].update(host_vars)
        self.yaml.dump()
