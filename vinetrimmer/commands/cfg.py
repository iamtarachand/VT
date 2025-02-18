import ast
import os

import click

try:
    import pytomlpp as toml
except ModuleNotFoundError:
    import toml

from vinetrimmer.config import filenames
from vinetrimmer.services import get_service_key
from vinetrimmer.utils import Logger


@click.command(name="cfg", short_help="Manage configuration values for the program and its services.")
@click.argument("key", type=str, required=False)
@click.argument("value", type=str, required=False)
@click.option("-s", "--service", type=str, default=None,
              help="Manage configuration of a Service (Secrets file only). Don't specify to edit the main config.")
@click.option("--unset", is_flag=True, default=False, help="Unset/remove the configuration value.")
@click.option("--list", "list_", is_flag=True, default=False, help="List all set configuration values.")
@click.pass_context
def cfg(ctx, key, value, service, unset, list_):
    if not key and not value and not list_:
        raise click.UsageError("Nothing to do.", ctx)

    if value:
        try:
            value = ast.literal_eval(value)
        except ValueError:
            pass  # probably a str without quotes or similar, assume it's a string value

    log = Logger.getLogger("cfg")

    if service:
        service = get_service_key(service)
        config_path = filenames.service_config.format(service=service)
    else:
        config_path = filenames.root_config

    data = {}
    if os.path.isfile(config_path):
        data = toml.load(config_path)

    if not data:
        log.warning(f"{config_path} has no configuration data, yet")

    if list_:
        print(toml.dumps(data).rstrip())
        return

    tree = key.split(".")
    temp = data
    for t in tree[:-1]:
        if temp.get(t) is None:
            temp[t] = {}
        temp = temp[t]

    if unset:
        if tree[-1] in temp:
            del temp[tree[-1]]
        log.info(f"Unset {key}")
    else:
        if value is None:
            if tree[-1] not in temp:
                raise click.ClickException(f"Key {key} does not exist in the config.")
            print(f"{key}: {temp[tree[-1]]}")
        else:
            temp[tree[-1]] = value
            log.info(f"Set {key} to {repr(value)}")
            os.makedirs(os.path.dirname(config_path), exist_ok=True)
            toml.dump(data, config_path)
