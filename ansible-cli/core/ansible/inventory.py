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

    def group_delete(self, group_name, parent_group_name=None):
        if group_name in self.yaml.data and parent_group_name is None:
            del self.yaml.data[group_name]
            self.yaml.dump()
        elif (
            group_name in self.yaml.data
            and parent_group_name is not None
            and parent_group_name in self.yaml.data
        ):
            del self.yaml.data[parent_group_name]["children"][group_name]
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

    def host_delete(self, host_name, group_name, parent_group_name=None):
        if group_name in self.yaml.data and host_name in self.yaml.data[group_name]:
            if parent_group_name is None:
                del self.yaml.data[group_name]["hosts"][host_name]
            elif (
                parent_group_name in self.yaml.data
                and group_name in self.yaml.data[parent_group_name]["children"]
            ):
                del self.yaml.data[parent_group_name]["children"][group_name]["hosts"][
                    host_name
                ]
            self.yaml.dump()

    def vars_insert(self, vars_name, group_name, parent_group_name=None, vars=None):
        self.group_insert(group_name, parent_group_name)
        if (
            parent_group_name is not None
            and "vars" not in self.yaml.data[parent_group_name][group_name]
        ):
            self.yaml.data[group_name]["vars"] = {}
        elif parent_group_name is None and "vars" not in self.yaml.data[group_name]:
            self.yaml.data[group_name]["vars"] = {}
        if vars_name not in self.yaml.data[group_name]["vars"]:
            self.yaml.data[group_name]["vars"][vars_name] = {}
        if vars is not None:
            self.yaml.data[group_name]["vars"][vars_name].update(vars)
        self.yaml.dump()
