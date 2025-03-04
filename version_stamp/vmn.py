#!/usr/bin/env python3
import argparse
import copy
import glob
import json
import os
import pathlib
import random
import re
import sys
import time
from multiprocessing import Pool
from pathlib import Path
from pprint import pformat

import jinja2
import tomlkit
import yaml
from filelock import FileLock
from packaging import version as pversion

CUR_PATH = "{0}/".format(os.path.dirname(__file__))
sys.path.append(CUR_PATH)
import version as version_mod
import stamp_utils


LOCK_FILE_ENV = "VMN_LOCK_FILE_PATH"

VER_FILE_NAME = "last_known_app_version.yml"
INIT_FILENAME = "conf.yml"
LOCK_FILENAME = "vmn.lock"
LOG_FILENAME = "vmn.log"
CACHE_FILENAME = "vmn.cache"

IGNORED_FILES = [
    LOCK_FILENAME,
    LOG_FILENAME,
    CACHE_FILENAME,
    stamp_utils.GLOBAL_LOG_FILENAME,
]
VMN_ARGS = {
    "init": "remote",
    "init-app": "remote",
    "show": "local",
    "stamp": "remote",
    "goto": "local",
    "release": "remote",
    "gen": "local",
    "add": "remote",
}


class VMNContainer(object):
    @stamp_utils.measure_runtime_decorator
    def __init__(self, args, root_path):
        self.args = args
        root = False
        if "root" in self.args:
            root = self.args.root

        initial_params = {"root": root, "name": None, "root_path": root_path}

        if "name" in self.args and self.args.name:
            validate_app_name(self.args)
            initial_params["name"] = self.args.name

            if "command" in self.args and "stamp" in self.args.command:
                initial_params["extra_commit_message"] = self.args.extra_commit_message

        self.params = initial_params
        self.vcs = None

        # Currently this is used only for show and only for cargo situation
        # TODO:: think if this feature should exist at all
        self.params["be_type"] = stamp_utils.VMN_BE_TYPE_GIT
        if "from_file" in self.args and self.args.from_file:
            self.params["be_type"] = stamp_utils.VMN_BE_TYPE_LOCAL_FILE

        self.vcs = VersionControlStamper(self.params)


class IVersionsStamper(object):
    @stamp_utils.measure_runtime_decorator
    def __init__(self, arg_params):
        self.app_conf_path = None
        self.params: dict = arg_params
        self.vmn_root_path: str = arg_params["root_path"]
        self.repo_name: str = "."
        self.name: str = arg_params["name"]
        self.be_type = arg_params["be_type"]

        # Configuration defaults
        self.template: str = stamp_utils.VMN_DEFAULT_TEMPLATE
        self.extra_info = False
        self.create_verinfo_files = False
        self.hide_zero_hotfix = True
        self.version_backends = {}
        # This one will be filled with self dependency ('.') by default
        self.raw_configured_deps = {}
        self.configured_deps = {}
        self.conf_file_exists = False
        self.root_conf_file_exists = False

        self.should_publish = True
        self.current_version_info = {
            "vmn_info": {
                "description_message_version": "1.1",
                "vmn_version": version_mod.version,
            },
            "stamping": {"msg": "", "app": {"info": {}}, "root_app": {}},
        }

        # root_context means that the user uses vmn in a context of a root app
        self.root_context = arg_params["root"]

        self.backend, err = stamp_utils.get_client(
            self.vmn_root_path,
            self.be_type,
            inherit_env=True,
        )
        if err:
            err_str = "Failed to create backend {0}. Exiting".format(err)
            stamp_utils.VMN_LOGGER.error(err_str)
            raise RuntimeError(err_str)

        if self.name is None:
            self.tracked = False
            return

        self.initialize_paths()
        self.update_attrs_from_app_conf_file()

        self.version_files = [self.app_conf_path, self.version_file_path]

        if not self.root_context:
            self.current_version_info["stamping"]["app"]["name"] = self.name

        if self.root_app_name is not None:
            self.current_version_info["stamping"]["root_app"] = {
                "name": self.root_app_name,
                # if we stamp, the latest service will be the self.name indeed.
                # when we show, we want to show self.name service as latest
                "latest_service": self.name,
                "services": {},
                "external_services": self.external_services,
            }

        err = self.initialize_backend_attrs()
        if err:
            # TODO:: test this
            raise RuntimeError("Failed to initialize_backend_attrs")

    @stamp_utils.measure_runtime_decorator
    def update_attrs_from_app_conf_file(self):
        # TODO:: handle deleted app with missing conf file
        if os.path.isfile(self.app_conf_path):
            self.conf_file_exists = True

            with open(self.app_conf_path, "r") as f:
                data = yaml.safe_load(f)
                if "template" in data["conf"]:
                    self.template = data["conf"]["template"]
                if "extra_info" in data["conf"]:
                    self.extra_info = data["conf"]["extra_info"]
                if "deps" in data["conf"]:
                    self.raw_configured_deps = data["conf"]["deps"]
                if "hide_zero_hotfix" in data["conf"]:
                    self.hide_zero_hotfix = data["conf"]["hide_zero_hotfix"]
                if "version_backends" in data["conf"]:
                    self.version_backends = data["conf"]["version_backends"]
                if "create_verinfo_files" in data["conf"]:
                    self.create_verinfo_files = data["conf"]["create_verinfo_files"]

                self.set_template(self.template)

        if self.root_app_conf_path is not None and os.path.isfile(
            self.root_app_conf_path
        ):
            self.root_conf_file_exists = True
            with open(self.root_app_conf_path) as f:
                data = yaml.safe_load(f)
                if "external_services" in data["conf"]:
                    self.external_services = data["conf"]["external_services"]

    @stamp_utils.measure_runtime_decorator
    def initialize_paths(self):
        self.app_dir_path = os.path.join(
            self.vmn_root_path, ".vmn", self.name.replace("/", os.sep)
        )

        self.version_file_path = os.path.join(self.app_dir_path, VER_FILE_NAME)

        self.app_conf_path = os.path.join(
            self.app_dir_path,
            f"{self.backend.active_branch}_conf.yml",
        )
        if not os.path.isfile(self.app_conf_path):
            self.app_conf_path = os.path.join(self.app_dir_path, "conf.yml")

        if self.root_context:
            self.root_app_name = self.name
        else:
            self.root_app_name = stamp_utils.VMNBackend.get_root_app_name_from_name(
                self.name
            )

        self.external_services = None
        self.root_app_dir_path = self.app_dir_path
        self.root_app_conf_path = None
        if self.root_app_name is not None:
            self.root_app_dir_path = os.path.join(
                self.vmn_root_path, ".vmn", self.root_app_name
            )

            self.root_app_conf_path = os.path.join(
                self.root_app_dir_path,
                f"{self.backend.active_branch}_root_conf.yml",
            )
            if not os.path.isfile(self.root_app_conf_path):
                self.root_app_conf_path = os.path.join(
                    self.root_app_dir_path, "root_conf.yml"
                )

    @stamp_utils.measure_runtime_decorator
    def initialize_configured_deps(self, self_base, self_dep):
        if self.raw_configured_deps:
            self.configured_deps = self.raw_configured_deps

        if os.path.join("../") not in self.configured_deps:
            self.configured_deps[os.path.join("../")] = {}
        if self_base not in self.configured_deps[os.path.join("../")]:
            self.configured_deps[os.path.join("../")][self_base] = {}

        self.configured_deps[os.path.join("../")][self_base] = self_dep

        flat_deps = {}
        for rel_path, v in self.configured_deps.items():
            for repo in v:
                key = os.path.relpath(
                    os.path.join(self.vmn_root_path, rel_path, repo),
                    self.vmn_root_path,
                )
                flat_deps[key] = v[repo]

        self.configured_deps = flat_deps

    @stamp_utils.measure_runtime_decorator
    def get_version_info_from_verstr(self, verstr):
        tag_name = self.get_tag_name(verstr)
        if self.root_context:
            try:
                int(verstr)
            except Exception:
                stamp_utils.VMN_LOGGER.error(
                    "wrong version specified: root version must be an integer"
                )

                return tag_name, {}
        else:
            try:
                stamp_utils.VMNBackend.deserialize_vmn_tag_name(tag_name)
            except Exception as exc:
                stamp_utils.VMN_LOGGER.error(f"Wrong version specified: {verstr}")
                stamp_utils.VMN_LOGGER.debug(f"Logged exception: ", exc_info=True)

                return tag_name, {}

        tag_name, ver_infos = self.backend.get_tag_version_info(tag_name)
        if not ver_infos:
            stamp_utils.VMN_LOGGER.error(
                f"Failed to get version info for tag: {tag_name}"
            )

            return tag_name, {}

        if tag_name not in ver_infos or ver_infos[tag_name]["ver_info"] is None:
            return tag_name, {}

        stamp_utils.VMNBackend.enhance_ver_info(ver_infos)

        return tag_name, ver_infos

    @stamp_utils.measure_runtime_decorator
    def get_tag_name(self, verstr):
        tag_name = f'{self.name.replace("/", "-")}'
        assert verstr is not None
        tag_name = f"{tag_name}_{verstr}"

        return tag_name

    @stamp_utils.measure_runtime_decorator
    def initialize_backend_attrs(self):
        self_base = os.path.basename(self.vmn_root_path)
        self_dep = {"remote": self.backend.remote(), "vcs_type": self.backend.type()}

        self.initialize_configured_deps(self_base, self_dep)

        if self.name is None:
            return

        self.last_user_changeset = self.backend.last_user_changeset()
        if self.last_user_changeset is None:
            raise RuntimeError(
                "Somehow vmn was not able to get last user changeset. "
                "This usually means that not enough git commit history was cloned. "
                "This can happen when using shallow repositories. "
                "Check your clone / checkout process."
            )

        self.actual_deps_state = self.backend.get_actual_deps_state(
            self.vmn_root_path,
            self.configured_deps,
        )
        self.actual_deps_state["."]["hash"] = self.last_user_changeset
        self.current_version_info["stamping"]["app"]["changesets"] = copy.deepcopy(
            self.actual_deps_state
        )

        self.ver_infos_from_repo = {}
        self.selected_tag = None
        (
            initial_version,
            prerelease,
            prerelease_count,
        ) = VersionControlStamper.get_version_number_from_file(self.version_file_path)
        if initial_version is not None:
            verstr = stamp_utils.VMNBackend.serialize_vmn_version(
                initial_version, prerelease, prerelease_count, self.hide_zero_hotfix
            )
            (
                self.selected_tag,
                self.ver_infos_from_repo,
            ) = self.get_version_info_from_verstr(verstr)
            t = self.get_tag_name(initial_version)
            if t != self.selected_tag and t in self.ver_infos_from_repo:
                self.selected_tag = t

        if not self.ver_infos_from_repo:
            (
                selected_tag,
                self.ver_infos_from_repo,
            ) = self.backend.get_first_reachable_version_info(
                self.name,
                self.root_context,
                type=stamp_utils.RELATIVE_TO_CURRENT_VCS_POSITION_TYPE,
            )
            if selected_tag is not None and selected_tag != self.selected_tag:
                self.selected_tag = selected_tag

        self.tracked = (
            self.selected_tag in self.ver_infos_from_repo
            and self.ver_infos_from_repo[self.selected_tag]["ver_info"] is not None
        )
        if self.tracked:
            for rel_path, dep in self.configured_deps.items():
                if rel_path.endswith(os.path.join("/", self_base)):
                    continue

                if "remote" in dep:
                    continue

                if rel_path in self.actual_deps_state:
                    dep["remote"] = self.actual_deps_state[rel_path]["remote"]
                elif (
                    rel_path
                    in self.ver_infos_from_repo[self.selected_tag]["ver_info"][
                        "stamping"
                    ]["app"]["changesets"]
                ):
                    dep["remote"] = self.ver_infos_from_repo[self.selected_tag][
                        "ver_info"
                    ]["stamping"]["app"]["changesets"][rel_path]["remote"]

        return 0

    def set_template(self, template):
        try:
            self.template = IVersionsStamper.parse_template(template)
            self.bad_format_template = False
        except Exception as exc:
            stamp_utils.VMN_LOGGER.debug("Logged exception: ", exc_info=True)
            self.template = IVersionsStamper.parse_template(
                stamp_utils.VMN_DEFAULT_TEMPLATE
            )
            self.template_err_str = (
                "Failed to parse template: "
                f"{template}. "
                f"Falling back to default one: "
                f"{stamp_utils.VMN_DEFAULT_TEMPLATE}"
            )

            self.bad_format_template = True

    def __del__(self):
        if self.backend is not None:
            del self.backend
            self.backend = None

    # Note: this function generates a version (including prerelease)
    def gen_advanced_version(
        self, initial_version, initialprerelease, initialprerelease_count
    ):
        verstr = self._advance_version(initial_version)

        prerelease = self.prerelease
        # If user did not specify a change in prerelease,
        # stay with the previous one
        if prerelease is None and self.release_mode is None:
            prerelease = initialprerelease
        prerelease_count = copy.deepcopy(initialprerelease_count)

        # Continue from last stamp prerelease information as long as
        # the last version is coherent with what is requested from
        # the version file or manual version (manual version is not yet implemented)
        prerelease, prerelease_count = self._advance_prerelease(
            verstr, prerelease, prerelease_count
        )

        return verstr, prerelease, prerelease_count

    def _advance_prerelease(self, verstr, prerelease, prerelease_count):
        if prerelease is None:
            return None, {}
        if prerelease == "release":
            try:
                raise RuntimeError()
            except RuntimeError:
                stamp_utils.VMN_LOGGER.error(
                    "prerelease equals to 'release' somehow. "
                    "Turn on debug mode to see traceback"
                )
                stamp_utils.VMN_LOGGER.debug("Exception info: ", exc_info=True)

            return None, {}

        prerelease_count = copy.deepcopy(prerelease_count)
        counter_key = f"{prerelease}"
        if counter_key not in prerelease_count:
            prerelease_count[counter_key] = 0

        tag_name_prefix = stamp_utils.VMNBackend.app_name_to_git_tag_app_name(self.name)

        tag_name_prefix = f'{self.name.replace("/", "-")}_{verstr}-{prerelease}*'
        tag = self.backend.get_latest_available_tag(tag_name_prefix)
        if tag:
            props = stamp_utils.VMNBackend.deserialize_vmn_tag_name(tag)

            global_val = int(props["prerelease"].split(prerelease)[1])
            prerelease_count[counter_key] = max(
                prerelease_count[counter_key], global_val
            )

        prerelease_count[counter_key] += 1

        if self.release_mode is not None:
            prerelease_count = {counter_key: 1}

        return counter_key, prerelease_count

    def increase_octet(self, tag_name_prefix: str, version_number_oct: str) -> str:
        tag = self.backend.get_latest_available_tag(tag_name_prefix)
        version_number_oct = int(version_number_oct)
        if tag:
            props = stamp_utils.VMNBackend.deserialize_vmn_tag_name(tag)
            version_number_oct = max(version_number_oct, int(props[self.release_mode]))
        version_number_oct += 1
        return str(version_number_oct)

    def _advance_version(self, version):
        # TODO: maybe move up the version validity test
        match = re.search(stamp_utils.VMN_REGEX, version)
        gdict = match.groupdict()
        if gdict["hotfix"] is None:
            gdict["hotfix"] = "0"

        major = gdict["major"]
        minor = gdict["minor"]
        patch = gdict["patch"]
        hotfix = gdict["hotfix"]

        if self.release_mode == "major":
            tag_name_prefix = stamp_utils.VMNBackend.app_name_to_git_tag_app_name(
                self.name
            )

            tag_name_prefix = f"{tag_name_prefix}_*"
            major = self.increase_octet(tag_name_prefix, major)

            minor = "0"
            patch = "0"
            hotfix = "0"
        elif self.release_mode == "minor":
            tag_name_prefix = stamp_utils.VMNBackend.app_name_to_git_tag_app_name(
                self.name
            )

            tag_name_prefix = f"{tag_name_prefix}_{major}*"
            minor = self.increase_octet(tag_name_prefix, minor)

            patch = "0"
            hotfix = "0"
        elif self.release_mode == "patch":
            tag_name_prefix = stamp_utils.VMNBackend.app_name_to_git_tag_app_name(
                self.name
            )

            tag_name_prefix = f"{tag_name_prefix}_{major}.{minor}*"
            patch = self.increase_octet(tag_name_prefix, patch)

            hotfix = "0"
        elif self.release_mode == "hotfix":
            tag_name_prefix = stamp_utils.VMNBackend.app_name_to_git_tag_app_name(
                self.name
            )

            tag_name_prefix = f"{tag_name_prefix}_{major}.{minor}.{patch}*"
            hotfix = self.increase_octet(tag_name_prefix, hotfix)

        return stamp_utils.VMNBackend.serialize_vmn_version_hotfix(
            self.hide_zero_hotfix,
            major,
            minor,
            patch,
            hotfix,
        )

    def write_version_to_file(
        self, version_number: str, prerelease: str, prerelease_count: dict
    ) -> None:
        if self.dry_run:
            stamp_utils.VMN_LOGGER.info(
                "Would have written to version file:\n"
                f"version: {version_number}\n"
                f"prerelease: {prerelease}\n"
                f"prerelease count: {prerelease_count}"
            )
        else:
            self._write_version_to_vmn_version_file(
                prerelease, prerelease_count, version_number
            )

        if not self.version_backends:
            return

        verstr = stamp_utils.VMNBackend.serialize_vmn_version(
            version_number,
            prerelease,
            prerelease_count,
            self.hide_zero_hotfix,
        )
        verstr = self.get_be_formatted_version(verstr)
        for backend in self.version_backends:
            try:
                if backend == "vmn_version_file":
                    stamp_utils.VMN_LOGGER.warning(
                        "Remove vmn_version_file version backend from the configuration"
                    )
                    continue

                handler = getattr(self, f"_write_version_to_{backend}")
                if self.dry_run:
                    stamp_utils.VMN_LOGGER.info(
                        "Would have written to a version backend file:\n"
                        f"backend: {backend}\n"
                        f"version: {verstr}"
                    )
                else:
                    handler(verstr)
            except AttributeError:
                stamp_utils.VMN_LOGGER.warning(f"Unsupported version backend {backend}")
                continue

    def _write_version_to_npm(self, verstr):
        backend_conf = self.version_backends["npm"]
        file_path = os.path.join(self.vmn_root_path, backend_conf["path"])
        try:
            with open(file_path, "r") as f:
                data = json.load(f)

            data["version"] = verstr
            with open(file_path, "w") as f:
                json.dump(data, f, indent=4, sort_keys=True)
        except IOError as e:
            stamp_utils.VMN_LOGGER.error(f"Error writing npm ver file: {file_path}\n")
            stamp_utils.VMN_LOGGER.debug("Exception info: ", exc_info=True)

            raise IOError(e)
        except Exception as e:
            stamp_utils.VMN_LOGGER.debug(e, exc_info=True)
            raise RuntimeError(e)

    def _write_version_to_cargo(self, verstr):
        backend_conf = self.version_backends["cargo"]
        file_path = os.path.join(self.vmn_root_path, backend_conf["path"])
        try:
            with open(file_path, "r") as f:
                data = tomlkit.loads(f.read())

            data["package"]["version"] = verstr
            with open(file_path, "w") as f:
                data = tomlkit.dumps(data)
                f.write(data)
        except IOError as e:
            stamp_utils.VMN_LOGGER.error(f"Error writing cargo ver file: {file_path}\n")
            stamp_utils.VMN_LOGGER.debug("Exception info: ", exc_info=True)

            raise IOError(e)
        except Exception as e:
            stamp_utils.VMN_LOGGER.debug(e, exc_info=True)
            raise RuntimeError(e)

    def _write_version_to_poetry(self, verstr):
        backend_conf = self.version_backends["poetry"]
        file_path = os.path.join(self.vmn_root_path, backend_conf["path"])
        try:
            with open(file_path, "r") as f:
                data = tomlkit.loads(f.read())

            data["tool"]["poetry"]["version"] = verstr
            with open(file_path, "w") as f:
                data = tomlkit.dumps(data)
                f.write(data)
        except IOError as e:
            stamp_utils.VMN_LOGGER.error(f"Error writing cargo ver file: {file_path}\n")
            stamp_utils.VMN_LOGGER.debug("Exception info: ", exc_info=True)

            raise IOError(e)
        except Exception as e:
            stamp_utils.VMN_LOGGER.debug(e, exc_info=True)
            raise RuntimeError(e)

    def _write_version_to_vmn_version_file(
        self, prerelease, prerelease_count, version_number
    ):
        file_path = self.version_file_path
        if prerelease is None:
            prerelease = "release"
        # this method will write the stamped ver of an app to a file,
        # weather the file pre exists or not
        try:
            with open(file_path, "w") as fid:
                ver_dict = {
                    "version_to_stamp_from": version_number,
                    "prerelease": prerelease,
                    "prerelease_count": prerelease_count,
                }
                yaml.dump(ver_dict, fid)
        except IOError as e:
            stamp_utils.VMN_LOGGER.error(f"Error writing ver file: {file_path}\n")
            stamp_utils.VMN_LOGGER.debug("Exception info: ", exc_info=True)

            raise IOError(e)
        except Exception as e:
            stamp_utils.VMN_LOGGER.debug(e, exc_info=True)
            raise RuntimeError(e)

    @staticmethod
    def parse_template(template: str) -> object:
        match = re.search(stamp_utils.VMN_TEMPLATE_REGEX, template)

        gdict = match.groupdict()

        return gdict

    def get_be_formatted_version(self, version):
        return stamp_utils.VMNBackend.get_utemplate_formatted_version(
            version, self.template, self.hide_zero_hotfix
        )

    def create_config_files(self):
        # If there is no file - create it
        if not self.conf_file_exists:
            pathlib.Path(os.path.dirname(self.app_conf_path)).mkdir(
                parents=True, exist_ok=True
            )

            tmp = copy.deepcopy(self.configured_deps)
            tmp.pop(".")

            ver_conf_yml = {
                "conf": {
                    "template": self.template,
                    "deps": tmp,
                    "extra_info": self.extra_info,
                    "hide_zero_hotfix": self.hide_zero_hotfix,
                    "create_verinfo_files": self.create_verinfo_files,
                    "version_backends": self.version_backends,
                }
            }

            with open(self.app_conf_path, "w+") as f:
                msg = (
                    "# Autogenerated by vmn. You can edit this " "configuration file\n"
                )
                f.write(msg)
                yaml.dump(ver_conf_yml, f, sort_keys=True)

        if self.root_app_name is None:
            return

        if self.root_conf_file_exists:
            return

        pathlib.Path(os.path.dirname(self.app_conf_path)).mkdir(
            parents=True, exist_ok=True
        )

        ver_yml = {"conf": {"external_services": {}}}

        with open(self.root_app_conf_path, "w+") as f:
            f.write("# Autogenerated by vmn\n")
            yaml.dump(ver_yml, f, sort_keys=True)


class VersionControlStamper(IVersionsStamper):
    @stamp_utils.measure_runtime_decorator
    def __init__(self, arg_params):
        IVersionsStamper.__init__(self, arg_params)

    @stamp_utils.measure_runtime_decorator
    def find_matching_version(self, version, prerelease, prerelease_count):
        """
        Try to find any version of the application matching the
        user's repositories local state
        :param version:
        :param prerelease:
        :param prerelease_count:
        :return:
        """

        if version is None:
            return None

        tag_formatted_app_name = stamp_utils.VMNBackend.serialize_vmn_tag_name(
            self.name,
            version,
            self.hide_zero_hotfix,
            prerelease,
            prerelease_count,
        )
        props = stamp_utils.VMNBackend.deserialize_vmn_tag_name(tag_formatted_app_name)
        release_tag_formatted_app_name = stamp_utils.VMNBackend.serialize_vmn_tag_name(
            self.name, props["version"], self.hide_zero_hotfix
        )

        if self.selected_tag != tag_formatted_app_name:
            # Get version info for tag
            tag_formatted_app_name, ver_infos = self.backend.get_tag_version_info(
                tag_formatted_app_name
            )
            if not ver_infos:
                stamp_utils.VMN_LOGGER.error(
                    f"Failed to get version info for tag: {tag_formatted_app_name}"
                )
                return None
        else:
            ver_infos = self.ver_infos_from_repo

        # TODO:: just a sanity? May be removed?
        if (
            tag_formatted_app_name not in ver_infos
            or ver_infos[tag_formatted_app_name] is None
        ):
            return None

        # means we are trying to find a matching version that is in rc state
        if prerelease_count:
            # try to check if there is a release version on it
            for k, v in ver_infos.items():
                if v is None:
                    # TODO: WTF?
                    raise RuntimeError("Bug")

        tmp = ver_infos[tag_formatted_app_name]["ver_info"]
        if release_tag_formatted_app_name in ver_infos:
            tmp = ver_infos[release_tag_formatted_app_name]["ver_info"]

        # Can happen if app's name is a prefix of another app
        if tmp["stamping"]["app"]["name"] != self.name:
            return None

        if tmp["stamping"]["app"]["release_mode"] == "init":
            return None

        found = True
        for k, v in tmp["stamping"]["app"]["changesets"].items():
            if k not in self.actual_deps_state:
                found = False
                break

            # when k is the "main repo" repo
            if self.repo_name == k:
                user_changeset = self.backend.last_user_changeset()

                if v["hash"] != user_changeset:
                    found = False
                    break
            elif v["hash"] != self.actual_deps_state[k]["hash"]:
                found = False
                break

        if found:
            return tmp

        return None

    @staticmethod
    @stamp_utils.measure_runtime_decorator
    def get_version_number_from_file(version_file_path) -> str or None:
        if not os.path.exists(version_file_path):
            return (None, None, None)

        with open(version_file_path, "r") as fid:
            ver_dict = yaml.safe_load(fid)
            if "version_to_stamp_from" in ver_dict:
                if "prerelease" not in ver_dict or "prerelease" not in ver_dict:
                    # Backward for 0.4.0-rc6
                    return (ver_dict["version_to_stamp_from"], "release", {})

                return (
                    ver_dict["version_to_stamp_from"],
                    ver_dict["prerelease"],
                    ver_dict["prerelease_count"],
                )

            # Backward compatible vmn 0.3.9 code
            if "prerelease" not in ver_dict:
                return (ver_dict["last_stamped_version"], "release", {})

            return (
                ver_dict["last_stamped_version"],
                ver_dict["prerelease"],
                ver_dict["prerelease_count"],
            )

    @stamp_utils.measure_runtime_decorator
    def release_app_version(self, tag_name, ver_info):
        if ver_info is None:
            stamp_utils.VMN_LOGGER.error(
                f"Tag {tag_name} doesn't seem to exist. Wrong version specified?"
            )
            raise RuntimeError()

        tmp = ver_info["stamping"]["app"]
        release_tag_name = stamp_utils.VMNBackend.serialize_vmn_tag_name(
            self.name,
            tmp["_version"],
            self.hide_zero_hotfix,
        )
        ver_info["vmn_info"] = self.current_version_info["vmn_info"]

        props = stamp_utils.VMNBackend.deserialize_vmn_tag_name(tag_name)
        ver_info["stamping"]["app"]["_version"] = props["version"]
        ver_info["stamping"]["app"][
            "version"
        ] = stamp_utils.VMNBackend.get_utemplate_formatted_version(
            props["version"], self.template, self.hide_zero_hotfix
        )
        ver_info["stamping"]["app"]["prerelease"] = "release"
        ver_info["stamping"]["app"]["release_mode"] = "release"

        messages = [yaml.dump(ver_info, sort_keys=True)]

        self.backend.tag(
            [release_tag_name],
            messages,
            ref=self.backend.changeset(tag=tag_name),
            push=True,
        )

        return props["version"]

    @stamp_utils.measure_runtime_decorator
    def add_metadata_to_version(self, tag_name, ver_info):
        # TODO:: merge logic with release_app_version and
        #  publish and handle reverting this way
        if ver_info is None:
            stamp_utils.VMN_LOGGER.error(
                f"Tag {tag_name} doesn't seem to exist. Wrong version specified?"
            )
            raise RuntimeError()

        tmp = ver_info["stamping"]["app"]
        res_ver = stamp_utils.VMNBackend.serialize_vmn_version(
            tmp["_version"],
            tmp["prerelease"],
            tmp["prerelease_count"],
            self.hide_zero_hotfix,
            self.params["buildmetadata"],
        )
        buildmetadata_tag_name = stamp_utils.VMNBackend.serialize_vmn_tag_name(
            self.name,
            tmp["_version"],
            self.hide_zero_hotfix,
            tmp["prerelease"],
            tmp["prerelease_count"],
            self.params["buildmetadata"],
        )

        ver_info["vmn_info"] = self.current_version_info["vmn_info"]
        ver_info["stamping"]["app"]["_version"] = res_ver
        ver_info["stamping"]["app"][
            "version"
        ] = stamp_utils.VMNBackend.get_utemplate_formatted_version(
            res_ver, self.template, self.hide_zero_hotfix
        )
        ver_info["stamping"]["app"]["prerelease"] = "metadata"

        if self.params["version_metadata_url"] is not None:
            ver_info["stamping"]["app"]["version_metadata_url"] = self.params[
                "version_metadata_url"
            ]

        if self.params["version_metadata_path"] is not None:
            path = self.params["version_metadata_path"]

            with open(path) as f:
                ver_info["stamping"]["app"]["version_metadata"] = yaml.safe_load(f)

        (
            buildmetadata_tag_name,
            tag_ver_infos,
        ) = self.backend.get_tag_version_info(buildmetadata_tag_name)
        if buildmetadata_tag_name in tag_ver_infos:
            if tag_ver_infos[buildmetadata_tag_name]["ver_info"] != ver_info:
                stamp_utils.VMN_LOGGER.error(
                    f"Tried to add different metadata for the same version."
                )
                raise RuntimeError()

            return res_ver

        messages = [yaml.dump(ver_info, sort_keys=True)]

        self.backend.tag(
            [buildmetadata_tag_name],
            messages,
            ref=self.backend.changeset(tag=tag_name),
            push=True,
        )

        return res_ver

    @stamp_utils.measure_runtime_decorator
    def stamp_app_version(
        self, initial_version, initialprerelease, initialprerelease_count
    ):
        if initialprerelease == "release" and self.release_mode is None:
            stamp_utils.VMN_LOGGER.error(
                "When not in release candidate mode, "
                "a release mode must be specified - use "
                "-r/--release-mode with one of major/minor/patch/hotfix"
            )
            raise RuntimeError()

        if initialprerelease != "release" and self.release_mode is None:
            release_tag_formatted_app_name = (
                stamp_utils.VMNBackend.serialize_vmn_tag_name(
                    self.name, initial_version, self.hide_zero_hotfix
                )
            )
            (
                release_tag_formatted_app_name,
                ver_infos,
            ) = self.backend.get_tag_version_info(release_tag_formatted_app_name)

            if (
                release_tag_formatted_app_name in ver_infos
                and ver_infos[release_tag_formatted_app_name] is not None
            ):
                stamp_utils.VMN_LOGGER.error(
                    f"The version {initial_version} was already released. "
                    "Will refuse to stamp prerelease version "
                )
                raise RuntimeError()

        current_version, prerelease, prerelease_count = self.gen_advanced_version(
            initial_version, initialprerelease, initialprerelease_count
        )

        info = {}
        if self.extra_info:
            info["env"] = dict(os.environ)

        release_mode = self.release_mode
        if prerelease is not None:
            release_mode = "prerelease"

        if prerelease is None:
            prerelease = "release"

        self.update_stamping_info(
            info,
            initial_version,
            initialprerelease,
            initialprerelease_count,
            current_version,
            prerelease,
            prerelease_count,
            release_mode,
        )

        return current_version, prerelease, prerelease_count

    @stamp_utils.measure_runtime_decorator
    def update_stamping_info(
        self,
        info,
        initial_version,
        initialprerelease,
        initialprerelease_count,
        current_version,
        prerelease,
        prerelease_count,
        release_mode,
    ):
        verstr = stamp_utils.VMNBackend.serialize_vmn_version(
            current_version, prerelease, prerelease_count, self.hide_zero_hotfix
        )
        self.current_version_info["stamping"]["app"]["_version"] = verstr
        self.current_version_info["stamping"]["app"]["prerelease"] = prerelease
        initial_verstr = stamp_utils.VMNBackend.serialize_vmn_version(
            initial_version,
            initialprerelease,
            initialprerelease_count,
            self.hide_zero_hotfix,
        )
        self.current_version_info["stamping"]["app"][
            "previous_version"
        ] = initial_verstr
        self.current_version_info["stamping"]["app"]["release_mode"] = release_mode
        self.current_version_info["stamping"]["app"]["info"] = copy.deepcopy(info)
        self.current_version_info["stamping"]["app"][
            "stamped_on_branch"
        ] = self.backend.active_branch
        self.current_version_info["stamping"]["app"][
            "stamped_on_remote_branch"
        ] = self.backend.remote_active_branch
        self.current_version_info["stamping"]["app"][
            "prerelease_count"
        ] = copy.deepcopy(prerelease_count)

    @stamp_utils.measure_runtime_decorator
    def stamp_root_app_version(self, override_version=None):
        if self.root_app_name is None:
            return None

        tag_name, ver_infos = self.backend.get_first_reachable_version_info(
            self.root_app_name,
            root_context=True,
            type=stamp_utils.RELATIVE_TO_CURRENT_VCS_POSITION_TYPE,
        )

        if tag_name not in ver_infos or ver_infos[tag_name]["ver_info"] is None:
            stamp_utils.VMN_LOGGER.error(
                f"Version information for {self.root_app_name} was not found"
            )
            raise RuntimeError()

        # TODO: think about this case
        if "version" not in ver_infos[tag_name]["ver_info"]["stamping"]["root_app"]:
            stamp_utils.VMN_LOGGER.error(
                f"Root app name is {self.root_app_name} and app name is {self.name}. "
                f"However no version information for root was found"
            )
            raise RuntimeError()

        old_version = int(
            ver_infos[tag_name]["ver_info"]["stamping"]["root_app"]["version"]
        )
        if override_version is None:
            override_version = old_version

        root_version = int(override_version) + 1

        root_app = ver_infos[tag_name]["ver_info"]["stamping"]["root_app"]
        services = copy.deepcopy(root_app["services"])

        services[self.name] = self.current_version_info["stamping"]["app"]["_version"]

        self.current_version_info["stamping"]["root_app"].update(
            {
                "version": root_version,
                "services": services,
            }
        )

        return "{0}".format(root_version)

    def get_files_to_add_to_index(self, paths):
        changed = [
            os.path.join(self.vmn_root_path, item.a_path.replace("/", os.sep))
            for item in self.backend._be.index.diff(None)
        ]
        untracked = [
            os.path.join(self.vmn_root_path, item.replace("/", os.sep))
            for item in self.backend._be.untracked_files
        ]

        version_files = []
        for path in paths:
            if path in changed or path in untracked:
                version_files.append(path)

        return version_files

    @stamp_utils.measure_runtime_decorator
    def publish_stamp(
        self, app_version, prerelease, prerelease_count, root_app_version
    ):
        verstr = stamp_utils.VMNBackend.serialize_vmn_version(
            app_version,
            prerelease,
            prerelease_count,
            self.hide_zero_hotfix,
        )
        app_msg = {
            "vmn_info": self.current_version_info["vmn_info"],
            "stamping": {"app": self.current_version_info["stamping"]["app"]},
        }

        if not self.should_publish:
            return 0

        self.write_version_to_file(
            version_number=app_version,
            prerelease=prerelease,
            prerelease_count=prerelease_count,
        )

        version_files_to_add = self.get_files_to_add_to_index(self.version_files)

        for backend in self.version_backends:
            backend_conf = self.version_backends[backend]
            file_path = os.path.join(self.vmn_root_path, backend_conf["path"])
            version_files_to_add.append(file_path)

        if self.create_verinfo_files:
            self.create_verinfo_file(app_msg, version_files_to_add, verstr)

        if self.root_app_name is not None:
            root_app_msg = {
                "stamping": {
                    "root_app": self.current_version_info["stamping"]["root_app"]
                },
                "vmn_info": self.current_version_info["vmn_info"],
            }

            tmp = self.get_files_to_add_to_index([self.root_app_conf_path])
            if tmp:
                version_files_to_add.extend(tmp)

            if self.create_verinfo_files:
                self.create_verinfo_root_file(
                    root_app_msg, root_app_version, version_files_to_add
                )

        commit_msg = None
        if self.current_version_info["stamping"]["app"]["release_mode"] == "init":
            commit_msg = f"{self.name}: Stamped initial version {verstr}\n\n"
        else:
            extra_commit_message = self.params["extra_commit_message"]
            commit_msg = (
                f"{self.name}: Stamped version {verstr}\n{extra_commit_message}\n"
            )

        self.current_version_info["stamping"]["msg"] = commit_msg

        prev_changeset = self.backend.changeset()

        try:
            self.publish_commit(version_files_to_add)
        except Exception as exc:
            stamp_utils.VMN_LOGGER.debug("Logged Exception message: ", exc_info=True)
            stamp_utils.VMN_LOGGER.info(f"Reverting vmn changes... ")
            if self.dry_run:
                stamp_utils.VMN_LOGGER.info(f"Would have tried to revert a vmn commit")
            else:
                self.backend.revert_vmn_commit(prev_changeset, self.version_files)

            # TODO:: turn to error codes. This one means - exit without retries
            return 3

        tag = f'{self.name.replace("/", "-")}_{verstr}'
        match = re.search(stamp_utils.VMN_TAG_REGEX, tag)
        if match is None:
            stamp_utils.VMN_LOGGER.error(
                f"Tag {tag} doesn't comply to vmn version format"
                f"Reverting vmn changes ..."
            )
            if self.dry_run:
                stamp_utils.VMN_LOGGER.info("Would have reverted vmn commit.")
            else:
                self.backend.revert_vmn_commit(prev_changeset, self.version_files)

            return 3

        tags = [tag]
        msgs = [app_msg]

        if self.root_app_name is not None:
            msgs.append(root_app_msg)
            tag = f"{self.root_app_name}_{root_app_version}"
            match = re.search(stamp_utils.VMN_ROOT_TAG_REGEX, tag)
            if match is None:
                stamp_utils.VMN_LOGGER.error(
                    f"Tag {tag} doesn't comply to vmn version format"
                    f"Reverting vmn changes ..."
                )
                if self.dry_run:
                    stamp_utils.VMN_LOGGER.info("Would have reverted vmn commit.")
                else:
                    self.backend.revert_vmn_commit(prev_changeset, self.version_files)

                return 3

            tags.append(tag)

        all_tags = []
        all_tags.extend(tags)

        try:
            for t, m in zip(tags, msgs):
                if self.dry_run:
                    stamp_utils.VMN_LOGGER.info(
                        "Would have created tag:\n"
                        f"{t}\n"
                        f"Tag content:\n{yaml.dump(m, sort_keys=True)}"
                    )
                else:
                    self.backend.tag([t], [yaml.dump(m, sort_keys=True)])
        except Exception as exc:
            stamp_utils.VMN_LOGGER.debug("Logged Exception message:", exc_info=True)
            stamp_utils.VMN_LOGGER.info(f"Reverting vmn changes for tags: {tags} ... ")
            if self.dry_run:
                stamp_utils.VMN_LOGGER.info(
                    f"Would have reverted vmn commit and delete tags:\n{all_tags}"
                )
            else:
                self.backend.revert_vmn_commit(
                    prev_changeset, self.version_files, all_tags
                )

            return 1

        try:
            if self.dry_run:
                stamp_utils.VMN_LOGGER.info(
                    "Would have pushed with tags.\n" f"tags: {all_tags} "
                )
            else:
                self.backend.push(all_tags)

                count = 0
                res = self.backend.check_for_outgoing_changes()
                while count < 5 and res:
                    count += 1
                    stamp_utils.VMN_LOGGER.error(
                        f"BUG: Somehow we have outgoing changes right "
                        f"after publishing:\n{res}"
                    )
                    time.sleep(60)
                    res = self.backend.check_for_outgoing_changes()

                if count == 5 and res:
                    raise RuntimeError(
                        f"BUG: Somehow we have outgoing changes right "
                        f"after publishing:\n{res}"
                    )
        except Exception:
            stamp_utils.VMN_LOGGER.debug("Logged Exception message:", exc_info=True)
            stamp_utils.VMN_LOGGER.info(f"Reverting vmn changes for tags: {tags} ...")
            if self.dry_run:
                stamp_utils.VMN_LOGGER.info(
                    f"Would have reverted vmn commit and delete tags:\n{all_tags}"
                )
            else:
                self.backend.revert_vmn_commit(
                    prev_changeset, self.version_files, all_tags
                )

            return 2

        return 0

    @stamp_utils.measure_runtime_decorator
    def publish_commit(self, version_files_to_add):
        cur_branch = self.backend.active_branch
        path = os.path.join(
            self.app_dir_path,
            f"*_conf.yml",
        )
        list_of_files = glob.glob(path)
        branch_conf_path = os.path.join(self.app_dir_path, f"{cur_branch}_conf.yml")

        if self.dry_run:
            if list_of_files:
                stamp_utils.VMN_LOGGER.info(
                    "Would have removed config files:\n"
                    f"{set(list_of_files) - set([branch_conf_path])}"
                )

            stamp_utils.VMN_LOGGER.info(
                "Would have created commit with message:\n"
                f'{self.current_version_info["stamping"]["msg"]}'
            )
        else:
            for f in set(list_of_files) - set([branch_conf_path]):
                try:
                    self.backend._be.index.remove([f], working_tree=True)
                except Exception as exc:
                    pass

                try:
                    f_to_rem = pathlib.Path(f)
                    f_to_rem.unlink()
                except Exception as exc:
                    pass

            self.backend.commit(
                message=self.current_version_info["stamping"]["msg"],
                user="vmn",
                include=version_files_to_add,
            )

    @stamp_utils.measure_runtime_decorator
    def create_verinfo_root_file(
        self, root_app_msg, root_app_version, version_files_to_add
    ):
        dir_path = os.path.join(self.root_app_dir_path, "root_verinfo")

        if self.dry_run:
            stamp_utils.VMN_LOGGER.info(
                "Would have written to root verinfo file:\n"
                f"path: {dir_path} version: {root_app_version}\n"
                f"message: {root_app_msg}"
            )
        else:
            Path(dir_path).mkdir(parents=True, exist_ok=True)
            path = os.path.join(dir_path, f"{root_app_version}.yml")
            with open(path, "w") as f:
                data = yaml.dump(root_app_msg, sort_keys=True)
                f.write(data)
            version_files_to_add.append(path)

    @stamp_utils.measure_runtime_decorator
    def create_verinfo_file(self, app_msg, version_files_to_add, verstr):
        dir_path = os.path.join(self.app_dir_path, "verinfo")

        if self.dry_run:
            stamp_utils.VMN_LOGGER.info(
                "Would have written to verinfo file:\n"
                f"path: {dir_path} version: {verstr}\n"
                f"message: {app_msg}"
            )
        else:
            Path(dir_path).mkdir(parents=True, exist_ok=True)
            path = os.path.join(dir_path, f"{verstr}.yml")
            with open(path, "w") as f:
                data = yaml.dump(app_msg, sort_keys=True)
                f.write(data)

            version_files_to_add.append(path)

    @stamp_utils.measure_runtime_decorator
    def retrieve_remote_changes(self):
        self.backend.pull()


@stamp_utils.measure_runtime_decorator
def handle_init(vmn_ctx):
    expected_status = {"repos_exist_locally"}
    optional_status = {"deps_synced_with_conf"}

    status = _get_repo_status(vmn_ctx.vcs, expected_status, optional_status)
    if status["error"]:
        stamp_utils.VMN_LOGGER.debug(
            f"Error occured when getting the repo status: {status}", exc_info=True
        )

        return 1

    be = vmn_ctx.vcs.backend

    vmn_path = os.path.join(vmn_ctx.vcs.vmn_root_path, ".vmn")
    Path(vmn_path).mkdir(parents=True, exist_ok=True)
    vmn_init_path = os.path.join(vmn_path, INIT_FILENAME)
    Path(vmn_init_path).touch()
    git_ignore_path = os.path.join(vmn_path, ".gitignore")

    with open(git_ignore_path, "w+") as f:
        for ignored_file in IGNORED_FILES:
            f.write(f"{ignored_file}{os.linesep}")

    # TODO:: revert in case of failure. Use the publish_commit function
    be.commit(
        message=stamp_utils.INIT_COMMIT_MESSAGE,
        user="vmn",
        include=[vmn_init_path, git_ignore_path],
    )
    be.push()

    stamp_utils.VMN_LOGGER.info(
        f"Initialized vmn tracking on {vmn_ctx.vcs.vmn_root_path}"
    )

    return 0


@stamp_utils.measure_runtime_decorator
def handle_init_app(vmn_ctx):
    vmn_ctx.vcs.dry_run = vmn_ctx.args.dry

    # TODO: validate version number is of type major.minor.patch[.hotfix]
    err = _init_app(vmn_ctx.vcs, vmn_ctx.args.version)
    if err:
        return 1

    if vmn_ctx.vcs.dry_run:
        stamp_utils.VMN_LOGGER.info(
            "Would have initialized app tracking on {0}".format(
                vmn_ctx.vcs.root_app_dir_path
            )
        )
    else:
        stamp_utils.VMN_LOGGER.info(
            "Initialized app tracking on {0}".format(vmn_ctx.vcs.root_app_dir_path)
        )

    return 0


@stamp_utils.measure_runtime_decorator
def handle_stamp(vmn_ctx):
    vmn_ctx.vcs.prerelease = vmn_ctx.args.pr
    vmn_ctx.vcs.buildmetadata = None
    vmn_ctx.vcs.release_mode = vmn_ctx.args.release_mode
    vmn_ctx.vcs.override_root_version = vmn_ctx.args.orv
    vmn_ctx.vcs.override_version = vmn_ctx.args.ov
    vmn_ctx.vcs.dry_run = vmn_ctx.args.dry

    # For backward compatibility
    if vmn_ctx.vcs.release_mode == "micro":
        vmn_ctx.vcs.release_mode = "hotfix"

    if vmn_ctx.vcs.tracked and vmn_ctx.vcs.release_mode is None:
        vmn_ctx.vcs.current_version_info["stamping"]["app"][
            "release_mode"
        ] = vmn_ctx.vcs.ver_infos_from_repo[vmn_ctx.vcs.selected_tag]["ver_info"][
            "stamping"
        ][
            "app"
        ][
            "release_mode"
        ]

    optional_status = {"modified", "detached"}
    expected_status = {
        "repos_exist_locally",
        "repo_tracked",
        "app_tracked",
        "deps_synced_with_conf",
    }

    status = _get_repo_status(vmn_ctx.vcs, expected_status, optional_status)
    if status["error"]:
        stamp_utils.VMN_LOGGER.debug(
            f"Error occured when getting the repo status: {status}", exc_info=True
        )

        return 1

    if status["matched_version_info"] is not None:
        # Good we have found an existing version matching
        # the actual_deps_state
        version = vmn_ctx.vcs.get_be_formatted_version(
            status["matched_version_info"]["stamping"]["app"]["_version"]
        )

        stamp_utils.VMN_LOGGER.info(version)

        return 0

    if "detached" in status["state"]:
        stamp_utils.VMN_LOGGER.error("In detached head. Will not stamp new version")
        return 1

    vmn_ctx.vcs.backend.perform_cached_fetch()

    # We didn't find any existing version
    if vmn_ctx.args.pull:
        try:
            vmn_ctx.vcs.backend.perform_cached_fetch(force=True)
            vmn_ctx.vcs.retrieve_remote_changes()
        except Exception as exc:
            stamp_utils.VMN_LOGGER.error(
                "Failed to pull, run with --debug for more details"
            )
            stamp_utils.VMN_LOGGER.debug("Logged Exception message:", exc_info=True)

            return 1

    (
        initial_version,
        prerelease,
        prerelease_count,
    ) = VersionControlStamper.get_version_number_from_file(
        vmn_ctx.vcs.version_file_path
    )

    if vmn_ctx.vcs.override_version:
        initial_version = vmn_ctx.vcs.override_version

    try:
        version = _stamp_version(
            vmn_ctx.vcs,
            vmn_ctx.args.pull,
            vmn_ctx.args.check_vmn_version,
            initial_version,
            prerelease,
            prerelease_count,
        )
    except Exception as exc:
        stamp_utils.VMN_LOGGER.debug("Logged Exception message:", exc_info=True)

        return 1

    if vmn_ctx.vcs.dry_run:
        stamp_utils.VMN_LOGGER.info(f"Would have stamped {version}")
    else:
        stamp_utils.VMN_LOGGER.info(f"{version}")

    return 0


@stamp_utils.measure_runtime_decorator
def handle_release(vmn_ctx):
    expected_status = {"repos_exist_locally", "repo_tracked", "app_tracked"}
    optional_status = {"detached", "modified", "dirty_deps", "deps_synced_with_conf"}

    status = _get_repo_status(vmn_ctx.vcs, expected_status, optional_status)
    if status["error"]:
        stamp_utils.VMN_LOGGER.debug(
            f"Error occured when getting the repo status: {status}", exc_info=True
        )

        return 1

    ver = vmn_ctx.args.version

    if ver:
        # TODO:: extract method
        match = re.search(stamp_utils.VMN_REGEX, ver)
        res = match.groupdict()
        if res["buildmetadata"]:
            stamp_utils.VMN_LOGGER.error(
                f"Failed to release {ver}. "
                f"Releasing metadata versions is not supported"
            )

            return 1

    if ver is None and status["matched_version_info"] is not None:
        # Good we have found an existing version matching
        # the actual_deps_state
        ver = vmn_ctx.vcs.get_be_formatted_version(
            status["matched_version_info"]["stamping"]["app"]["_version"]
        )
    elif ver is None:
        stamp_utils.VMN_LOGGER.error(
            "When running vmn release and not on a version commit, "
            "you must specify a specific version using -v flag"
        )

        return 1

    try:
        tag_name, ver_infos = vmn_ctx.vcs.get_version_info_from_verstr(ver)
        if tag_name not in ver_infos or ver_infos[tag_name]["ver_info"] is None:
            ver_info = None
        else:
            ver_info = ver_infos[tag_name]["ver_info"]

        base_ver = stamp_utils.VMNBackend.get_base_vmn_version(
            ver,
            vmn_ctx.vcs.hide_zero_hotfix,
        )

        tag_formatted_app_name = stamp_utils.VMNBackend.serialize_vmn_tag_name(
            vmn_ctx.vcs.name,
            base_ver,
            vmn_ctx.vcs.hide_zero_hotfix,
        )

        if tag_formatted_app_name in ver_infos:
            stamp_utils.VMN_LOGGER.info(base_ver)
            return 0

        stamp_utils.VMN_LOGGER.info(vmn_ctx.vcs.release_app_version(tag_name, ver_info))
    except Exception as exc:
        stamp_utils.VMN_LOGGER.error(f"Failed to release {ver}")
        stamp_utils.VMN_LOGGER.debug("Logged Exception message:", exc_info=True)

        return 1

    return 0


@stamp_utils.measure_runtime_decorator
def handle_add(vmn_ctx):
    vmn_ctx.params["buildmetadata"] = vmn_ctx.args.bm
    vmn_ctx.params["version_metadata_path"] = vmn_ctx.args.vmp
    vmn_ctx.params["version_metadata_url"] = vmn_ctx.args.vmu

    expected_status = {"repos_exist_locally", "repo_tracked", "app_tracked"}
    optional_status = {"detached", "modified", "dirty_deps", "deps_synced_with_conf"}

    status = _get_repo_status(vmn_ctx.vcs, expected_status, optional_status)
    if status["error"]:
        stamp_utils.VMN_LOGGER.debug(
            f"Error occured when getting the repo status: {status}", exc_info=True
        )

        return 1

    ver = vmn_ctx.args.version

    if ver:
        # TODO:: extract method
        match = re.search(stamp_utils.VMN_REGEX, ver)
        res = match.groupdict()
        if res["buildmetadata"]:
            stamp_utils.VMN_LOGGER.error(
                f"Failed to add to {ver}. "
                f"Adding metadata versions to metadata versions is not supported"
            )

            return 1

    if ver is None and status["matched_version_info"] is not None:
        # Good we have found an existing version matching
        # the actual_deps_state
        ver = vmn_ctx.vcs.get_be_formatted_version(
            status["matched_version_info"]["stamping"]["app"]["_version"]
        )
    elif ver is None:
        stamp_utils.VMN_LOGGER.error(
            "When running vmn add and not on a version commit, "
            "you must specify a specific version using -v flag"
        )

        return 1

    try:
        tag_name, ver_infos = vmn_ctx.vcs.get_version_info_from_verstr(ver)
        if tag_name not in ver_infos or ver_infos[tag_name]["ver_info"] is None:
            ver_info = None
        else:
            ver_info = ver_infos[tag_name]["ver_info"]
        stamp_utils.VMN_LOGGER.info(
            vmn_ctx.vcs.add_metadata_to_version(tag_name, ver_info)
        )
    except Exception as exc:
        stamp_utils.VMN_LOGGER.debug("Logged Exception message:", exc_info=True)

        return 1

    return 0


@stamp_utils.measure_runtime_decorator
def handle_show(vmn_ctx):
    vmn_ctx.params["from_file"] = vmn_ctx.args.from_file

    # root app does not have raw version number
    if vmn_ctx.vcs.root_context:
        vmn_ctx.params["raw"] = False
    else:
        vmn_ctx.params["raw"] = vmn_ctx.args.raw

    vmn_ctx.params["ignore_dirty"] = vmn_ctx.args.ignore_dirty

    vmn_ctx.params["verbose"] = vmn_ctx.args.verbose
    vmn_ctx.params["conf"] = vmn_ctx.args.conf

    if vmn_ctx.args.template is not None:
        vmn_ctx.vcs.set_template(vmn_ctx.args.template)

    vmn_ctx.params["display_unique_id"] = vmn_ctx.args.display_unique_id
    vmn_ctx.params["display_type"] = vmn_ctx.args.display_type

    try:
        out = show(vmn_ctx.vcs, vmn_ctx.params, vmn_ctx.args.version)
    except Exception as exc:
        stamp_utils.VMN_LOGGER.debug("Logged Exception message:", exc_info=True)
        return 1

    return 0


@stamp_utils.measure_runtime_decorator
def handle_gen(vmn_ctx):
    vmn_ctx.params["jinja_template"] = vmn_ctx.args.template
    vmn_ctx.params["verify_version"] = vmn_ctx.args.verify_version
    vmn_ctx.params["output"] = vmn_ctx.args.output
    vmn_ctx.params["custom_values"] = vmn_ctx.args.custom_values

    try:
        out = gen(vmn_ctx.vcs, vmn_ctx.params, vmn_ctx.args.version)
    except Exception as exc:
        stamp_utils.VMN_LOGGER.error("Failed to gen, run with --debug for more details")
        stamp_utils.VMN_LOGGER.debug("Logged Exception message:", exc_info=True)
        return 1

    return 0


@stamp_utils.measure_runtime_decorator
def handle_goto(vmn_ctx):
    expected_status = {"repo_tracked", "app_tracked"}
    optional_status = {
        "detached",
        "repos_exist_locally",
        "modified",
        "deps_synced_with_conf",
    }

    vmn_ctx.params["deps_only"] = vmn_ctx.args.deps_only

    status = _get_repo_status(vmn_ctx.vcs, expected_status, optional_status)
    if status["error"]:
        stamp_utils.VMN_LOGGER.debug(
            f"Error occured when getting the repo status: {status}", exc_info=True
        )

        return 1

    return goto_version(
        vmn_ctx.vcs, vmn_ctx.params, vmn_ctx.args.version, vmn_ctx.args.pull
    )


@stamp_utils.measure_runtime_decorator
def _get_repo_status(vcs, expected_status, optional_status=set()):
    be = vcs.backend
    default_status = {
        "pending": False,
        "detached": False,
        "outgoing": False,
        "state": set(),
        "error": False,
    }
    status = copy.deepcopy(default_status)
    status.update(
        {
            "repos_exist_locally": True,
            "deps_synced_with_conf": True,
            "repo_tracked": True,
            "app_tracked": True,
            # TODO: rename to on_stamped_version and turn to True
            "modified": False,
            "dirty_deps": False,
            "err_msgs": {
                "dirty_deps": "",
                "deps_synced_with_conf": "",
                "repo_tracked": "vmn repo tracking is already initialized",
                "app_tracked": "vmn app tracking is already initialized",
            },
            "repos": {},
            "matched_version_info": None,
            # Assumed state
            "state": {
                "repos_exist_locally",
                "deps_synced_with_conf",
                "repo_tracked",
                "app_tracked",
            },
            "local_repos_diff": set(),
        }
    )

    path = os.path.join(vcs.vmn_root_path, ".vmn")
    if not vcs.tracked:
        status["app_tracked"] = False
        status["err_msgs"]["app_tracked"] = "Untracked app. Run vmn init-app first"
        status["state"].remove("app_tracked")

        if not vcs.backend.is_path_tracked(path):
            status["repo_tracked"] = False
            status["err_msgs"][
                "repo_tracked"
            ] = "vmn tracking is not yet initialized. Run vmn init on the repository"
            status["state"].remove("repo_tracked")

    err = be.check_for_pending_changes()
    if err:
        status["pending"] = True
        status["err_msgs"]["pending"] = err
        status["state"].add("pending")

    err = be.check_for_outgoing_changes()
    if err:
        # TODO:: Check for errcode instead of startswith
        if err.startswith("Detached head"):
            status["detached"] = True
            status["err_msgs"]["detached"] = err
            status["state"].add("detached")
        else:
            # Outgoing changes cannot be in detached head
            # TODO: is it really?
            status["outgoing"] = True
            status["err_msgs"]["outgoing"] = err
            status["state"].add("outgoing")

    if "name" in vcs.current_version_info["stamping"]["app"]:
        (
            initial_version,
            prerelease,
            prerelease_count,
        ) = VersionControlStamper.get_version_number_from_file(vcs.version_file_path)
        matched_version_info = vcs.find_matching_version(
            initial_version, prerelease, prerelease_count
        )
        if matched_version_info is None:
            status["modified"] = True
            status["state"].add("modified")
        else:
            status["matched_version_info"] = matched_version_info

        configured_repos = set(vcs.configured_deps.keys())
        local_repos = set(vcs.actual_deps_state.keys())

        missing_deps = configured_repos - local_repos
        if missing_deps:
            paths = []
            for path in missing_deps:
                paths.append(os.path.join(vcs.vmn_root_path, path))

            status["repos_exist_locally"] = False
            status["err_msgs"]["repos_exist_locally"] = (
                f"Dependency repository were specified in conf.yml file. "
                f"However repos: {paths} do not exist. Please clone and rerun"
            )
            status["local_repos_diff"] = missing_deps
            status["state"].remove("repos_exist_locally")

        err = 0
        common_deps = configured_repos & local_repos
        for repo in common_deps:
            # Skip local repo
            if repo == ".":
                continue

            status["repos"][repo] = copy.deepcopy(default_status)
            full_path = os.path.join(vcs.vmn_root_path, repo)

            dep_be, err = stamp_utils.get_client(full_path, vcs.be_type)
            if err:
                err_str = "Failed to create backend {0}. Exiting".format(err)
                stamp_utils.VMN_LOGGER.error(err)
                raise RuntimeError(err)

            err = dep_be.check_for_pending_changes()
            if err:
                status["dirty_deps"] = True
                status["err_msgs"][
                    "dirty_deps"
                ] = f"{status['err_msgs']['dirty_deps']}\n{err}"
                status["state"].add("dirty_deps")
                status["repos"][repo]["pending"] = True
                status["repos"][repo]["state"].add("pending")

            if "branch" in vcs.configured_deps[repo]:
                try:
                    branch_name = dep_be.get_active_branch()
                    err_msg = (
                        f"{repo} repository is on a different branch: "
                        f"{branch_name} than what is required by the configuration: "
                        f"{vcs.configured_deps[repo]['branch']}"
                    )
                    assert branch_name == vcs.configured_deps[repo]["branch"]
                except Exception as exc:
                    status["deps_synced_with_conf"] = False
                    status["err_msgs"][
                        "deps_synced_with_conf"
                    ] = f"{status['err_msgs']['deps_synced_with_conf']}\n{err_msg}"
                    if "deps_synced_with_conf" in status["state"]:
                        status["state"].remove("deps_synced_with_conf")

                    status["repos"][repo]["branch_synced_error"] = True
                    status["repos"][repo]["state"].add("not_synced_with_conf")

            if "tag" in vcs.configured_deps[repo]:
                try:
                    err_msg = (
                        f"Repository in not on the requested tag by the configuration "
                        f"for {repo}."
                    )
                    c1 = dep_be.changeset(tag=vcs.configured_deps[repo]["tag"])
                    c2 = dep_be.changeset()
                    assert c1 == c2
                except Exception as exc:
                    status["deps_synced_with_conf"] = False
                    status["err_msgs"][
                        "deps_synced_with_conf"
                    ] = f"{status['err_msgs']['deps_synced_with_conf']}\n{err_msg}"
                    if "deps_synced_with_conf" in status["state"]:
                        status["state"].remove("deps_synced_with_conf")

                    status["repos"][repo]["tag_synced_error"] = True
                    status["repos"][repo]["state"].add("not_synced_with_conf")

            if "hash" in vcs.configured_deps[repo]:
                try:
                    err_msg = (
                        f"Repository in not on the requested hash by the configuration "
                        f"for {repo}."
                    )
                    assert vcs.configured_deps[repo]["hash"] == dep_be.changeset()
                except Exception as exc:
                    status["deps_synced_with_conf"] = False
                    status["err_msgs"][
                        "deps_synced_with_conf"
                    ] = f"{status['err_msgs']['deps_synced_with_conf']}\n{err_msg}"
                    if "deps_synced_with_conf" in status["state"]:
                        status["state"].remove("deps_synced_with_conf")

                    status["repos"][repo]["hash_synced_error"] = True
                    status["repos"][repo]["state"].add("not_synced_with_conf")

            if not dep_be.in_detached_head():
                err = dep_be.check_for_outgoing_changes()
                if err:
                    status["dirty_deps"] = True
                    status["err_msgs"][
                        "dirty_deps"
                    ] = f"{status['err_msgs']['dirty_deps']}\n{err}"
                    status["state"].add("dirty_deps")
                    status["repos"][repo]["outgoing"] = True
                    status["repos"][repo]["state"].add("outgoing")
            else:
                status["repos"][repo]["detached"] = True
                status["repos"][repo]["state"].add("detached")

    if (expected_status & status["state"]) != expected_status:
        for msg in expected_status - status["state"]:
            if msg in status["err_msgs"] and status["err_msgs"][msg]:
                stamp_utils.VMN_LOGGER.error(status["err_msgs"][msg])

        status["error"] = True

        return status

    if ((optional_status | status["state"]) - expected_status) != optional_status:
        for msg in (optional_status | status["state"]) - expected_status:
            if msg in status["err_msgs"] and status["err_msgs"][msg]:
                stamp_utils.VMN_LOGGER.error(status["err_msgs"][msg])

        stamp_utils.VMN_LOGGER.error(
            f"Repository status is in unexpected state:\n"
            f"{((optional_status | status['state']) - expected_status)}\n"
            f"versus optional:\n{optional_status}"
        )

        status["error"] = True

        return status

    return status


@stamp_utils.measure_runtime_decorator
def _init_app(versions_be_ifc, starting_version):
    optional_status = {"modified", "detached"}
    expected_status = {"repos_exist_locally", "repo_tracked", "deps_synced_with_conf"}

    status = _get_repo_status(versions_be_ifc, expected_status, optional_status)
    if status["error"]:
        stamp_utils.VMN_LOGGER.debug(
            f"Error occured when getting the repo status: {status}", exc_info=True
        )

        return 1

    versions_be_ifc.create_config_files()

    info = {}
    versions_be_ifc.update_stamping_info(
        info, starting_version, "release", {}, starting_version, "release", {}, "init"
    )

    versions_be_ifc.backend.perform_cached_fetch()

    root_app_version = 0
    services = {}
    if versions_be_ifc.root_app_name is not None:
        tag_name, ver_infos = versions_be_ifc.backend.get_first_reachable_version_info(
            versions_be_ifc.root_app_name,
            root_context=True,
            type=stamp_utils.RELATIVE_TO_GLOBAL_TYPE,
        )
        if tag_name in ver_infos and ver_infos[tag_name]["ver_info"]:
            root_app_version = (
                int(ver_infos[tag_name]["ver_info"]["stamping"]["root_app"]["version"])
                + 1
            )
            root_app = ver_infos[tag_name]["ver_info"]["stamping"]["root_app"]
            services = copy.deepcopy(root_app["services"])

        versions_be_ifc.current_version_info["stamping"]["root_app"].update(
            {
                "version": root_app_version,
                "services": services,
            }
        )

        msg_root_app = versions_be_ifc.current_version_info["stamping"]["root_app"]
        msg_app = versions_be_ifc.current_version_info["stamping"]["app"]
        msg_root_app["services"][versions_be_ifc.name] = msg_app["_version"]

    try:
        err = versions_be_ifc.publish_stamp(
            starting_version, "release", {}, root_app_version
        )
    except Exception as exc:
        stamp_utils.VMN_LOGGER.debug("Logged Exception message: ", exc_info=True)
        versions_be_ifc.backend.revert_local_changes(versions_be_ifc.version_files)
        err = -1

    if err:
        stamp_utils.VMN_LOGGER.error("Failed to init app")
        raise RuntimeError()

    return 0


@stamp_utils.measure_runtime_decorator
def _stamp_version(
    versions_be_ifc,
    pull,
    check_vmn_version,
    initial_version,
    initialprerelease,
    initialprerelease_count,
):
    stamped = False
    retries = 3
    override_initial_version = initial_version
    override_initialprerelease = initialprerelease
    override_initialprerelease_count = initialprerelease_count
    override_main_current_version = versions_be_ifc.override_root_version

    if check_vmn_version:
        newer_stamping = version_mod.version != "dev" and (
            pversion.parse(
                versions_be_ifc.current_version_info["vmn_info"]["vmn_version"]
            )
            > pversion.parse(version_mod.version)
        )

        if newer_stamping:
            stamp_utils.VMN_LOGGER.error(
                "Refusing to stamp with old vmn. Please upgrade"
            )
            raise RuntimeError()

    if versions_be_ifc.bad_format_template:
        stamp_utils.VMN_LOGGER.warning(versions_be_ifc.template_err_str)

    while retries:
        retries -= 1

        (
            current_version,
            prerelease,
            prerelease_count,
        ) = versions_be_ifc.stamp_app_version(
            override_initial_version,
            override_initialprerelease,
            override_initialprerelease_count,
        )
        main_ver = versions_be_ifc.stamp_root_app_version(override_main_current_version)

        try:
            err = versions_be_ifc.publish_stamp(
                current_version, prerelease, prerelease_count, main_ver
            )
        except Exception as exc:
            stamp_utils.VMN_LOGGER.error(
                f"Failed to publish. Will revert local changes {exc}\nFor more details use --debug"
            )
            stamp_utils.VMN_LOGGER.debug("Exception info: ", exc_info=True)
            versions_be_ifc.backend.revert_local_changes(versions_be_ifc.version_files)
            err = -1

        if not err:
            stamped = True
            break

        if err == 1:
            override_initial_version = current_version
            override_initialprerelease = prerelease
            override_initialprerelease_count = prerelease_count
            override_main_current_version = main_ver

            stamp_utils.VMN_LOGGER.warning(
                "Failed to publish. Will try to auto-increase "
                "from {0} to {1}".format(
                    current_version,
                    versions_be_ifc.gen_advanced_version(
                        override_initial_version,
                        override_initialprerelease,
                        override_initialprerelease_count,
                    )[0],
                )
            )
        elif err == 2:
            if not pull:
                break

            time.sleep(random.randint(1, 5))
            try:
                versions_be_ifc.retrieve_remote_changes()
            except Exception as exc:
                stamp_utils.VMN_LOGGER.error("Failed to pull", exc_info=True)
        else:
            break

    if not stamped:
        err = "Failed to stamp"
        stamp_utils.VMN_LOGGER.error(err)
        raise RuntimeError(err)

    verstr = stamp_utils.VMNBackend.serialize_vmn_version(
        current_version, prerelease, prerelease_count, versions_be_ifc.hide_zero_hotfix
    )

    return versions_be_ifc.get_be_formatted_version(verstr)


@stamp_utils.measure_runtime_decorator
def show(vcs, params, verstr=None):
    dirty_states = None
    ver_infos = vcs.ver_infos_from_repo
    tag_name = vcs.selected_tag
    if verstr:
        tag_name, ver_infos = vcs.get_version_info_from_verstr(verstr)

    if not params["from_file"]:
        expected_status = {"repo_tracked", "app_tracked"}
        optional_status = {
            "repos_exist_locally",
            "detached",
            "pending",
            "outgoing",
            "modified",
            "dirty_deps",
            "deps_synced_with_conf",
        }
        status = _get_repo_status(vcs, expected_status, optional_status)
        if status["error"]:
            stamp_utils.VMN_LOGGER.error("Error occured when getting the repo status")
            stamp_utils.VMN_LOGGER.debug(status, exc_info=True)

            raise RuntimeError()

        if tag_name in ver_infos:
            dirty_states = list(get_dirty_states(optional_status, status))

            if params["ignore_dirty"]:
                dirty_states = None

            vers = []
            for i in ver_infos.keys():
                vers.append(i.split("_")[-1])

            ver_infos[tag_name]["ver_info"]["stamping"]["app"]["versions"] = []
            ver_infos[tag_name]["ver_info"]["stamping"]["app"]["versions"].extend(vers)

    if tag_name not in ver_infos:
        ver_info = None
    else:
        ver_info = ver_infos[tag_name]["ver_info"]

    if ver_info is None:
        stamp_utils.VMN_LOGGER.info(
            "Version information was not found " "for {0}.".format(vcs.name)
        )

        raise RuntimeError()

    data = {}

    if params["conf"]:
        data["conf"] = {
            "raw_deps": copy.deepcopy(vcs.raw_configured_deps),
            "deps": copy.deepcopy(vcs.configured_deps),
            "template": vcs.template,
            "hide_zero_hotfix": vcs.hide_zero_hotfix,
            "version_backends": copy.deepcopy(vcs.version_backends),
        }

    if vcs.root_context:
        data.update(ver_info["stamping"]["root_app"])
        if not data:
            stamp_utils.VMN_LOGGER.info(
                "App {0} does not have a root app ".format(vcs.name)
            )

            raise RuntimeError()

        out = None
        if params.get("verbose"):
            out = yaml.dump(data)
        else:
            out = data["version"]

        if dirty_states:
            out = yaml.dump(dirty_states)

        print(out)

        return 0

    data.update(ver_info["stamping"]["app"])
    data["version"] = stamp_utils.VMNBackend.get_utemplate_formatted_version(
        data["_version"], vcs.template, vcs.hide_zero_hotfix
    )
    data["unique_id"] = stamp_utils.VMNBackend.gen_unique_id(
        data["_version"], data["changesets"]["."]["hash"]
    )

    if params.get("verbose"):
        if dirty_states:
            data["dirty"] = dirty_states
        out = yaml.dump(data)
    else:
        out = data["version"]

        if params.get("raw"):
            out = data["_version"]

        if params.get("display_unique_id"):
            out = stamp_utils.VMNBackend.gen_unique_id(
                out, data["changesets"]["."]["hash"]
            )

        d_out = {}
        if dirty_states:
            d_out.update(
                {
                    "out": out,
                    "dirty": dirty_states,
                }
            )
        if params.get("display_type"):
            d_out.update(
                {
                    "out": out,
                    "type": data["prerelease"],
                }
            )

        if params.get("conf"):
            d_out.update(
                {
                    "out": out,
                    "conf": data["conf"],
                }
            )

        if d_out:
            out = yaml.safe_dump(d_out)

    print(out)

    return out


@stamp_utils.measure_runtime_decorator
def gen(vcs, params, verstr=None):
    expected_status = {"repo_tracked", "app_tracked"}
    optional_status = {
        "repos_exist_locally",
        "detached",
        "pending",
        "outgoing",
        "modified",
        "dirty_deps",
        "deps_synced_with_conf",
    }
    status = _get_repo_status(vcs, expected_status, optional_status)
    if status["error"]:
        stamp_utils.VMN_LOGGER.error("Error occured when getting the repo status")
        stamp_utils.VMN_LOGGER.debug(status, exc_info=True)

        raise RuntimeError()

    if verstr is None:
        ver_infos = vcs.ver_infos_from_repo
        tag_name = vcs.selected_tag
    else:
        tag_name, ver_infos = vcs.get_version_info_from_verstr(verstr)

    if tag_name not in ver_infos or ver_infos[tag_name]["ver_info"] is None:
        stamp_utils.VMN_LOGGER.error(
            "Version information was not found " "for {0}.".format(vcs.name)
        )

        raise RuntimeError()

    dirty_states = get_dirty_states(optional_status, status)
    if params["verify_version"]:
        # TODO: check here what will happen when using "hotfix" octa
        if dirty_states:
            stamp_utils.VMN_LOGGER.error(
                f"The repository and maybe some of its dependencies are in dirty state."
                f"Dirty states found: {dirty_states}.\nError messages collected for dependencies:\n"
                f"{status['err_msgs']['dirty_deps']}\n"
                f"Refusing to gen."
            )
            raise RuntimeError()

        if (
            status["matched_version_info"] is not None
            and verstr is not None
            and status["matched_version_info"]["stamping"]["app"]["_version"]
        ):
            stamp_utils.VMN_LOGGER.error(
                f"The repository is not exactly at version: {verstr}. "
                f"You can use `vmn goto` in order to jump to that version.\n"
                f"Refusing to gen."
            )
            raise RuntimeError()

    data = ver_infos[tag_name]["ver_info"]["stamping"]["app"]
    if verstr is None:
        data["changesets"] = {}

        for k, v in vcs.configured_deps.items():
            if k not in vcs.actual_deps_state:
                stamp_utils.VMN_LOGGER.error(
                    f"{k} doesn't exist locally. Use vmn goto and rerun"
                )
                raise RuntimeError()

            data["changesets"][k] = copy.deepcopy(vcs.actual_deps_state[k])
            data["changesets"][k]["state"] = {"clean"}

            if status["repos"] and vcs.repo_name != k:
                data["changesets"][k]["state"] = status["repos"][k]["state"]
            elif vcs.repo_name == k:
                data["changesets"][k]["state"] = dirty_states

    data["version"] = stamp_utils.VMNBackend.get_utemplate_formatted_version(
        data["_version"], vcs.template, vcs.hide_zero_hotfix
    )

    tmplt_value = {}
    tmplt_value.update(data)
    tmplt_value["base_version"] = stamp_utils.VMNBackend.get_base_vmn_version(
        data["_version"],
        vcs.hide_zero_hotfix,
    )
    if "root_app" in ver_infos[tag_name]["ver_info"]["stamping"]:
        for key, v in ver_infos[tag_name]["ver_info"]["stamping"]["root_app"].items():
            tmplt_value[f"root_{key}"] = v

    if params["custom_values"] is not None:
        with open(params["custom_values"], "r") as f:
            ret = yaml.safe_load(f)
            tmplt_value.update(ret)

    with open(params["jinja_template"]) as file_:
        template = jinja2.Template(file_.read())

    stamp_utils.VMN_LOGGER.debug(
        f"Possible keywords for your Jinja template:\n" f"{pformat(tmplt_value)}"
    )
    out = template.render(tmplt_value)

    out_path = params["output"]

    if os.path.exists(out_path):
        with open(out_path) as file_:
            current_out_content = file_.read()
            if current_out_content == out:
                return 0

    with open(out_path, "w") as f:
        f.write(out)

    return 0


def get_dirty_states(optional_status, status):
    dirty_states = (optional_status & status["state"]) | {
        "repos_exist_locally",
        "detached",
    }
    dirty_states -= {"detached", "repos_exist_locally", "deps_synced_with_conf"}

    try:
        debug_msg = ""
        for k in status["err_msgs"].keys():
            if k in dirty_states:
                debug_msg = f"{debug_msg}\n{status['err_msgs'][k]}"

        if debug_msg:
            stamp_utils.VMN_LOGGER.debug(f"Debug for dirty states call:{debug_msg}")
    except Exception as exc:
        stamp_utils.VMN_LOGGER.debug("Logged Exception message: ", exc_info=True)
        pass

    return dirty_states


@stamp_utils.measure_runtime_decorator
def goto_version(vcs, params, version, pull):
    unique_id = None
    check_unique = False
    status_str = ""

    if version is None:
        if not params["deps_only"]:
            ret = vcs.backend.checkout_branch()
            assert ret is not None

            if pull:
                try:
                    vcs.retrieve_remote_changes()
                except Exception as exc:
                    stamp_utils.VMN_LOGGER.error(
                        "Failed to pull, run with --debug for more details"
                    )
                    stamp_utils.VMN_LOGGER.debug(
                        "Logged Exception message:", exc_info=True
                    )

                    return 1

                ret = vcs.backend.checkout_branch()
                assert ret is not None

            del vcs
            vcs = VersionControlStamper(params)

        tag_name, ver_infos = vcs.backend.get_first_reachable_version_info(
            vcs.name, vcs.root_context, stamp_utils.RELATIVE_TO_CURRENT_VCS_BRANCH_TYPE
        )
        if tag_name not in ver_infos or ver_infos[tag_name]["ver_info"] is None:
            stamp_utils.VMN_LOGGER.error(f"No such app: {vcs.name}")
            return 1

        data = ver_infos[tag_name]["ver_info"]["stamping"]["app"]
        deps = copy.deepcopy(vcs.configured_deps)

        if not params["deps_only"]:
            if vcs.root_context:
                verstr = ver_infos[tag_name]["ver_info"]["stamping"]["root_app"][
                    "version"
                ]
                status_str = f"You are at the tip of the branch of version {verstr} for {vcs.name}"
            else:
                status_str = f"You are at the tip of the branch of version {data['_version']} for {vcs.name}"
    else:
        # check for unique id
        res = version.split("+")
        if len(res) > 1:
            version, unique_id = res
            check_unique = True

        if not params["deps_only"] and pull:
            try:
                vcs.retrieve_remote_changes()
            except Exception as exc:
                stamp_utils.VMN_LOGGER.error(
                    "Failed to pull, run with --debug for more details"
                )
                stamp_utils.VMN_LOGGER.debug("Logged Exception message:", exc_info=True)

                return 1

        tag_name, ver_infos = vcs.get_version_info_from_verstr(version)
        if tag_name not in ver_infos or ver_infos[tag_name]["ver_info"] is None:
            stamp_utils.VMN_LOGGER.error(f"No such app: {vcs.name}")
            return 1

        data = ver_infos[tag_name]["ver_info"]["stamping"]["app"]
        deps = copy.deepcopy(data["changesets"])

        if not params["deps_only"]:
            try:
                vcs.backend.checkout(tag=tag_name)
                status_str = f"You are at version {version} of {vcs.name}"
            except Exception:
                stamp_utils.VMN_LOGGER.error(
                    "App: {0} with version: {1} was "
                    "not found".format(vcs.name, version)
                )

                return 1

    if check_unique:
        if not deps["."]["hash"].startswith(unique_id):
            stamp_utils.VMN_LOGGER.error(f"Wrong unique id")
            return 1

    deps.pop(".")
    if deps:
        if version is None:
            for rel_path, v in deps.items():
                v["hash"] = None

                if "branch" in vcs.configured_deps[rel_path]:
                    v["branch"] = vcs.configured_deps[rel_path]["branch"]
                if "tag" in vcs.configured_deps[rel_path]:
                    v["branch"] = None
                    v["tag"] = vcs.configured_deps[rel_path]["tag"]
                if "hash" in vcs.configured_deps[rel_path]:
                    v["branch"] = None
                    v["tag"] = None
                    v["hash"] = vcs.configured_deps[rel_path]["hash"]
        try:
            _goto_version(deps, vcs.vmn_root_path, pull)
        except Exception as exc:
            stamp_utils.VMN_LOGGER.error(f"goto failed: {exc}")
            stamp_utils.VMN_LOGGER.debug(f"", exc_info=True)

            return 1

    if status_str:
        stamp_utils.VMN_LOGGER.info(status_str)

    return 0


@stamp_utils.measure_runtime_decorator
def _update_repo(args):
    root_path = stamp_utils.resolve_root_path()
    vmn_path = os.path.join(root_path, ".vmn")

    stamp_utils.init_stamp_logger(os.path.join(vmn_path, LOG_FILENAME))

    path, rel_path, branch_name, tag, changeset, pull = args

    client = None
    try:
        if path == root_path:
            client, err = stamp_utils.get_client(path, "git", inherit_env=True)
        else:
            client, err = stamp_utils.get_client(path, "git")

        # TODO:: why this is not an error?
        if client is None:
            return {"repo": rel_path, "status": 0, "description": err}
    except Exception as exc:
        stamp_utils.VMN_LOGGER.exception(
            "Unexpected behaviour:\nAborting update " f"operation in {path} Reason:\n"
        )

        return {"repo": rel_path, "status": 1, "description": None}

    try:
        err = client.check_for_pending_changes()
        if err:
            stamp_utils.VMN_LOGGER.info("{0}. Aborting update operation ".format(err))
            return {"repo": rel_path, "status": 1, "description": err}

    except Exception as exc:
        stamp_utils.VMN_LOGGER.debug(f'Skipping "{path}"')
        stamp_utils.VMN_LOGGER.debug("Exception info: ", exc_info=True)

        return {"repo": rel_path, "status": 0, "description": None}

    cur_changeset = client.changeset()
    try:
        if not client.in_detached_head():
            err = client.check_for_outgoing_changes()
            if err:
                stamp_utils.VMN_LOGGER.info(
                    "{0}. Aborting update operation".format(err)
                )
                return {"repo": rel_path, "status": 1, "description": err}

        stamp_utils.VMN_LOGGER.info("Updating {0}".format(rel_path))

        if pull:
            try:
                client.checkout_branch()
                client.pull()
            except Exception as exc:
                stamp_utils.VMN_LOGGER.exception("Failed to pull:", exc_info=True)
                return {"repo": rel_path, "status": 1, "description": "Failed to pull"}

        if changeset is None:
            if tag is not None:
                client.checkout(tag=tag)
                stamp_utils.VMN_LOGGER.info(
                    "Updated {0} to tag {1}".format(rel_path, tag)
                )
            else:
                rev = client.checkout_branch(branch_name=branch_name)
                if rev is None:
                    raise RuntimeError(f"Failed to checkout to branch {branch_name}")

                if branch_name is not None:
                    stamp_utils.VMN_LOGGER.info(
                        "Updated {0} to branch {1}".format(rel_path, branch_name)
                    )
                else:
                    stamp_utils.VMN_LOGGER.info(
                        "Updated {0} to changeset {1}".format(rel_path, rev)
                    )
        else:
            client.checkout(rev=changeset)

            stamp_utils.VMN_LOGGER.info(
                "Updated {0} to {1}".format(rel_path, changeset)
            )
    except Exception as exc:
        stamp_utils.VMN_LOGGER.exception(
            f"Unexpected behaviour:\n"
            f"Trying to abort update operation in {path} "
            "Reason:\n",
            exc_info=True,
        )

        try:
            client.checkout(rev=cur_changeset)
        except Exception as exc:
            stamp_utils.VMN_LOGGER.exception(
                "Unexpected behaviour when tried to revert:", exc_info=True
            )

        return {"repo": rel_path, "status": 1, "description": None}

    return {"repo": rel_path, "status": 0, "description": None}


@stamp_utils.measure_runtime_decorator
def _clone_repo(args):
    root_path = stamp_utils.resolve_root_path()
    vmn_path = os.path.join(root_path, ".vmn")

    stamp_utils.init_stamp_logger(os.path.join(vmn_path, LOG_FILENAME))

    path, rel_path, remote, vcs_type = args
    if os.path.exists(path):
        return {"repo": rel_path, "status": 0, "description": None}

    stamp_utils.VMN_LOGGER.info("Cloning {0}..".format(rel_path))
    try:
        if vcs_type == "git":
            stamp_utils.GitBackend.clone(path, remote)
    except Exception as exc:
        try:
            str = "already exists and is not an empty directory."
            if str in exc.stderr:
                return {"repo": rel_path, "status": 0, "description": None}
        except Exception:
            pass

        err = "Failed to clone {0} repository. " "Description: {1}".format(
            rel_path, exc.args
        )
        return {"repo": rel_path, "status": 1, "description": err}

    return {"repo": rel_path, "status": 0, "description": None}


@stamp_utils.measure_runtime_decorator
def _goto_version(deps, vmn_root_path, pull):
    args = []
    for rel_path, v in deps.items():
        if "remote" not in v or not v["remote"]:
            stamp_utils.VMN_LOGGER.error(
                "Failed to find a remote for a configured repository. Failing goto"
            )
            raise RuntimeError()

        # In case the remote is a local dir
        if v["remote"].startswith("."):
            v["remote"] = os.path.join(vmn_root_path, v["remote"])

        args.append(
            (
                os.path.join(vmn_root_path, rel_path),
                rel_path,
                v["remote"],
                v["vcs_type"],
            )
        )
    with Pool(min(len(args), 10)) as p:
        results = p.map(_clone_repo, args)

    err = False
    failed_repos = set()
    for res in results:
        if res["status"] == 1:
            err = True

            if res["repo"] is None and res["description"] is None:
                continue

            msg = "Failed to clone "
            if res["repo"] is not None:
                failed_repos.add(res["repo"])
                msg += "from {0} ".format(res["repo"])
            if res["description"] is not None:
                msg += "because {0}".format(res["description"])

            stamp_utils.VMN_LOGGER.info(msg)

    args = []
    for rel_path, v in deps.items():
        if rel_path in failed_repos:
            continue

        branch = None
        if "branch" in v and v["branch"] is not None:
            branch = v["branch"]
        tag = None
        if "tag" in v and v["tag"] is not None:
            tag = v["tag"]

        args.append(
            (
                os.path.join(vmn_root_path, rel_path),
                rel_path,
                branch,
                tag,
                v["hash"],
                pull,
            )
        )

    with Pool(min(len(args), 20)) as p:
        results = p.map(_update_repo, args)

    for res in results:
        if res["status"] == 1:
            err = True
            if res["repo"] is None and res["description"] is None:
                continue

            msg = "Failed to update "
            if res["repo"] is not None:
                msg += " {0} ".format(res["repo"])
            if res["description"] is not None:
                msg += "because {0}".format(res["description"])

            stamp_utils.VMN_LOGGER.warning(msg)

    if err:
        stamp_utils.VMN_LOGGER.error(
            "Failed to update one or more " "of the required repos. See log above"
        )
        raise RuntimeError()


@stamp_utils.measure_runtime_decorator
def main(command_line=None):
    # Please KEEP this function exactly like this
    # The purpose of this function is to keep the return
    # value to be an integer
    res, _ = vmn_run(command_line)

    return res


@stamp_utils.measure_runtime_decorator
def vmn_run(command_line=None):
    try:
        stamp_utils.init_stamp_logger()
        args = parse_user_commands(command_line)
    except Exception as exc:
        stamp_utils.VMN_LOGGER.debug("Logged exception: ", exc_info=True)
        return 1, None

    try:
        stamp_utils.init_stamp_logger(debug=args.debug)

        root_path = stamp_utils.resolve_root_path()
        vmn_path = os.path.join(root_path, ".vmn")
        pathlib.Path(vmn_path).mkdir(parents=True, exist_ok=True)

    except Exception as exc:
        stamp_utils.VMN_LOGGER.error(
            "Failed to init logger. "
            "Maybe you are running from a non-managed directory?"
        )
        stamp_utils.VMN_LOGGER.debug("Logged exception: ", exc_info=True)

        return 1, None

    err = 0
    vmnc = None
    try:
        lock_file_path = os.path.join(vmn_path, LOCK_FILENAME)
        if LOCK_FILE_ENV in os.environ:
            lock_file_path = os.environ[LOCK_FILE_ENV]

        lock = FileLock(lock_file_path)

        # start of non-parallel code section
        lock.acquire()

        stamp_utils.init_stamp_logger(os.path.join(vmn_path, LOG_FILENAME), args.debug)
        command_line = copy.deepcopy(command_line)

        if command_line is None or not command_line:
            command_line = sys.argv
            if command_line is None:
                command_line = ["vmn"]

        if not command_line[0].endswith("vmn"):
            command_line.insert(0, "vmn")

        bold_char = "\033[1m"
        end_char = "\033[0m"
        stamp_utils.VMN_LOGGER.debug(
            f"\n{bold_char}Command line: {' '.join(command_line)}{end_char}"
        )

        err, vmnc = _vmn_run(args, root_path)

        # We only need it here. In other, Exception cases -
        # the unlock will happen naturally because the process will exit
        lock.release()

    except Exception as exc:
        stamp_utils.VMN_LOGGER.error(
            "vmn_run raised exception. Run vmn --debug for details"
        )
        stamp_utils.VMN_LOGGER.debug("Exception info: ", exc_info=True)

        err = 1
    except:
        stamp_utils.VMN_LOGGER.debug("Exception info: ", exc_info=True)
        err = 1

    return err, vmnc


@stamp_utils.measure_runtime_decorator
def _vmn_run(args, root_path):
    err = 0
    vmnc = VMNContainer(args, root_path)
    if vmnc.args.command in VMN_ARGS:
        if VMN_ARGS[vmnc.args.command] == "remote" or (
            "pull" in vmnc.args and vmnc.args.pull
        ):
            err = vmnc.vcs.backend.prepare_for_remote_operation()
            if err:
                stamp_utils.VMN_LOGGER.error(
                    "Failed to run prepare for remote operation.\n"
                    "Check the log. Aborting remote operation."
                )
                return err, vmnc

            if vmnc.vcs.name is not None:
                # If there is no remote branch set, it is impossible
                # to understand if there are outgoing changes. Thus this is required for
                # remote operations.
                # TODO:: verify that this assumaption is correct
                configured_repos = set(vmnc.vcs.configured_deps.keys())
                local_repos = set(vmnc.vcs.actual_deps_state.keys())
                common_deps = configured_repos & local_repos
                common_deps.remove(".")

                for repo in common_deps:
                    full_path = os.path.join(vmnc.vcs.vmn_root_path, repo)

                    dep_be, err = stamp_utils.get_client(full_path, vmnc.vcs.be_type)
                    if err:
                        err_str = "Failed to create backend {0}. Exiting".format(err)
                        stamp_utils.VMN_LOGGER.error(err)
                        raise RuntimeError(err)

                    dep_be.prepare_for_remote_operation()
                    del dep_be

        cmd = vmnc.args.command.replace("-", "_")
        err = getattr(sys.modules[__name__], f"handle_{cmd}")(vmnc)
    else:
        stamp_utils.VMN_LOGGER.info("Run vmn -h for help")

    return err, vmnc


def validate_app_name(args):
    if args.name.startswith("/"):
        stamp_utils.VMN_LOGGER.error("App name cannot start with /")
        raise RuntimeError()
    if "-" in args.name:
        stamp_utils.VMN_LOGGER.error("App name cannot include -")
        raise RuntimeError()


def parse_user_commands(command_line):
    parser = argparse.ArgumentParser("vmn")
    parser.add_argument(
        "--version", "-v", action="version", version=version_mod.version
    )
    parser.add_argument("--debug", required=False, action="store_true")
    parser.set_defaults(debug=False)
    subprasers = parser.add_subparsers(dest="command")

    for arg in VMN_ARGS.keys():
        arg = arg.replace("-", "_")
        getattr(sys.modules[__name__], f"add_arg_{arg}")(subprasers)

    args = parser.parse_args(command_line)

    verify_user_input_version(args, "version")
    verify_user_input_version(args, "ov")
    verify_user_input_version(args, "orv")

    return args


def add_arg_gen(subprasers):
    pgen = subprasers.add_parser(
        "gen", help="Generate version file based on jinja2 template"
    )
    pgen.add_argument(
        "-v",
        "--version",
        default=None,
        required=False,
        help=f"The version to generate the file for in the format:"
        f" {stamp_utils.VMN_VERSION_FORMAT}",
    )
    pgen.add_argument(
        "-t", "--template", required=True, help=f"Path to the jinja2 template"
    )
    pgen.add_argument("-o", "--output", required=True, help=f"Path for the output file")
    pgen.add_argument("--verify-version", dest="verify_version", action="store_true")
    pgen.set_defaults(verify_version=False)
    pgen.add_argument("name", help="The application's name")
    pgen.add_argument(
        "-c",
        "--custom-values",
        default=None,
        required=False,
        help=f"Path to a yml file with custom keys and values",
    )


def add_arg_release(subprasers):
    prelease = subprasers.add_parser("release", help="Release app version")
    prelease.add_argument(
        "-v",
        "--version",
        default=None,
        required=False,
        help=f"The version to release in the format: "
        f" {stamp_utils.VMN_VERSION_FORMAT}",
    )
    prelease.add_argument("name", help="The application's name")


def add_arg_goto(subprasers):
    pgoto = subprasers.add_parser("goto", help="go to version")
    pgoto.add_argument(
        "-v",
        "--version",
        default=None,
        required=False,
        help=f"The version to go to in the format: "
        f" {stamp_utils.VMN_VERSION_FORMAT}",
    )
    pgoto.add_argument("--root", dest="root", action="store_true")
    pgoto.set_defaults(root=False)
    pgoto.add_argument("--deps-only", dest="deps_only", action="store_true")
    pgoto.set_defaults(deps_only=False)
    pgoto.add_argument("name", help="The application's name")
    pgoto.add_argument("--pull", dest="pull", action="store_true")
    pgoto.set_defaults(pull=False)


def add_arg_stamp(subprasers):
    pstamp = subprasers.add_parser("stamp", help="stamp version")
    pstamp.add_argument(
        "-r",
        "--release-mode",
        choices=["major", "minor", "patch", "hotfix", "micro"],
        default=None,
        help="major / minor / patch / hotfix",
        metavar="",
    )
    pstamp.add_argument(
        "--pr",
        "--prerelease",
        default=None,
        help="Prerelease version. Can be anything really until you decide "
        "to release the version",
    )
    pstamp.add_argument("--pull", dest="pull", action="store_true")
    pstamp.set_defaults(pull=False)
    pstamp.add_argument(
        "--dont-check-vmn-version", dest="check_vmn_version", action="store_false"
    )
    pstamp.set_defaults(check_vmn_version=True)
    pstamp.add_argument(
        "--orv",
        "--override-root-version",
        default=None,
        help="Override current root version with any integer of your choice",
    )
    pstamp.add_argument(
        "--ov",
        "--override-version",
        default=None,
        help=f"Override current version with any version in the "
        f"format: {stamp_utils.VMN_VER_REGEX}",
    )
    pstamp.add_argument("--dry-run", dest="dry", action="store_true")
    pstamp.set_defaults(dry=False)
    pstamp.add_argument("name", help="The application's name")
    pstamp.add_argument(
        "-e",
        "--extra-commit-message",
        default="",
        help="add more information to the commit message."
        "example: adding --extra-commit-message '[ci-skip]' "
        "will add the string '[ci-skip]' to the commit message",
    )


def add_arg_show(subprasers):
    pshow = subprasers.add_parser("show", help="show app version")
    pshow.add_argument("name", help="The application's name to show the version for")
    pshow.add_argument(
        "-v",
        "--version",
        default=None,
        help=f"The version to show. Must be specified in the raw version format:"
        f" {stamp_utils.VMN_VERSION_FORMAT}",
    )
    pshow.add_argument(
        "-t", "--template", default=None, help="The template to use in show"
    )
    pshow.add_argument("--root", dest="root", action="store_true")
    pshow.set_defaults(root=False)
    pshow.add_argument("--verbose", dest="verbose", action="store_true")
    pshow.set_defaults(verbose=False)
    pshow.add_argument("--conf", dest="conf", action="store_true")
    pshow.set_defaults(conf=False)
    pshow.add_argument("--raw", dest="raw", action="store_true")
    pshow.set_defaults(raw=False)
    pshow.add_argument("--from-file", dest="from_file", action="store_true")
    pshow.set_defaults(from_file=False)
    pshow.add_argument("--ignore-dirty", dest="ignore_dirty", action="store_true")
    pshow.set_defaults(ignore_dirty=False)
    pshow.add_argument("-u", "--unique", dest="display_unique_id", action="store_true")
    pshow.set_defaults(display_unique_id=False)
    pshow.add_argument("--type", dest="display_type", action="store_true")
    pshow.set_defaults(display_type=False)


def add_arg_init_app(subprasers):
    pinitapp = subprasers.add_parser(
        "init-app",
        help="initialize version tracking for application. "
        "This command should be called only once per application",
    )

    pinitapp.add_argument(
        "-v",
        "--version",
        default="0.0.0",
        help="The version to init from. Must be specified in the raw version format: "
        "{major}.{minor}.{patch}",
    )
    pinitapp.add_argument("--dry-run", dest="dry", action="store_true")
    pinitapp.set_defaults(dry=False)
    pinitapp.add_argument(
        "name", help="The application's name to initialize version tracking for"
    )


def add_arg_init(subprasers):
    subprasers.add_parser(
        "init",
        help="initialize version tracking for the repository. "
        "This command should be called only once per repository",
    )


def add_arg_add(subprasers):
    padd = subprasers.add_parser(
        "add", help="Add additional metadata for already stamped version"
    )
    padd.add_argument(
        "-v",
        "--version",
        default=None,
        required=False,
        help=f"The version to add the 'buildmetadata' in the format:"
        f" {stamp_utils.VMN_VERSION_FORMAT}",
    )
    padd.add_argument(
        "--bm",
        "--buildmetadata",
        required=True,
        help=f"String for the 'buildmetadata' version extension "
        f"without the '+' sign complying with the regex:"
        f" {stamp_utils.SEMVER_BUILDMETADATA_REGEX}",
    )
    padd.add_argument(
        "--vmp",
        "--version-metadata-path",
        required=False,
        help=f"A path to a YML file which is associated with the specific build version",
    )
    padd.add_argument(
        "--vmu",
        "--version-metadata-url",
        required=False,
        help=f"A URL which is associated with the specific build version",
    )
    padd.add_argument("name", help="The application's name")


def verify_user_input_version(args, key):
    if key not in args or getattr(args, key) is None:
        return

    if key == "ov":
        match = re.search(stamp_utils.VMN_VER_REGEX, getattr(args, key))
    elif key == "orv":
        match = re.search(stamp_utils.VMN_ROOT_REGEX, getattr(args, key))
    elif "root" not in args or not args.root:
        match = re.search(stamp_utils.VMN_REGEX, getattr(args, key))
    else:
        match = re.search(stamp_utils.VMN_ROOT_REGEX, getattr(args, key))

    if match is None:
        if "root" not in args or not args.root:
            err = f"Version must be in format: {stamp_utils.VMN_VERSION_FORMAT}"
        else:
            err = f"Root version must be an integer"

        stamp_utils.VMN_LOGGER.error(err)

        raise RuntimeError(err)


if __name__ == "__main__":
    ret_err = main()
    if ret_err:
        sys.exit(1)

    sys.exit(0)
