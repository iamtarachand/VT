import os
import tempfile
from types import SimpleNamespace

try:
    import pytomlpp as toml
except ModuleNotFoundError:
    import toml
from appdirs import AppDirs
from vinetrimmer.objects.vaults import Vault
from vinetrimmer.utils.collections import merge_dict
from pathlib import Path

class Config:
    @staticmethod
    def load_vault(vault):
        return Vault(**{
            "vault_type" if k == "type" else k: v for k, v in vault.items()
        })


class Directories:
    def __init__(self):
        self.app_dirs = AppDirs("vinetrimmer", False)
        self.package_root = Path(__file__).resolve().parent.parent
        self.configuration = self.package_root / "config"
        self.user_configs = self.package_root
        self.service_configs = self.user_configs / "Services"
        self.data = self.package_root
        self.downloads = Path(__file__).resolve().parents[2] / "Downloads"
        self.temp = Path(__file__).resolve().parents[2] / "Temp"
        self.cache = self.package_root / "Cache"
        self.cookies = self.data / "Cookies"
        self.logs = self.package_root / "Logs"
        self.devices = self.data / "devices"


class Filenames:
    def __init__(self):
        self.log = os.path.join(directories.logs, "vinetrimmer_{name}_{time}.log")
        self.root_config = os.path.join(directories.configuration, "vinetrimmer.toml")
        self.user_root_config = os.path.join(directories.user_configs, "vinetrimmer.toml")
        self.service_config = os.path.join(directories.configuration, "services", "{service}.toml")
        self.user_service_config = os.path.join(directories.service_configs, "{service}.toml")
        self.subtitles = os.path.join(directories.temp, "TextTrack_{id}_{language_code}.srt")
        self.chapters = os.path.join(directories.temp, "{filename}_chapters.txt")


directories = Directories()
filenames = Filenames()
config = toml.load(filenames.root_config)
user_config = toml.load(filenames.root_config)
merge_dict(config, user_config)
config = SimpleNamespace(**config)
config.key_vaults = [Config.load_vault(x) for x in config.key_vaults]
credentials = config.credentials

# This serves two purposes:
# - Allow `range` to be used in the arguments section in the config rather than just `range_`
# - Allow sections like [arguments.Amazon] to work even if an alias (e.g. AMZN or amzn) is used.
#   CaseInsensitiveDict is used for `arguments` above to achieve case insensitivity.
# NOTE: The import cannot be moved to the top of the file, it will cause a circular import error.
from vinetrimmer.services import SERVICE_MAP  # noqa: E402

if "range_" not in config.arguments:
    config.arguments["range_"] = config.arguments.get("range_")
for service, aliases in SERVICE_MAP.items():
    for alias in aliases:
        config.arguments[alias] = config.arguments.get(service)

for directory in ("downloads", "temp"):
    if config.directories.get(directory):
        setattr(directories, directory, os.path.expanduser(config.directories[directory]))
