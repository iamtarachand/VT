import importlib
import os

SERVICE_MAP = {}

for service in os.listdir(os.path.dirname(__file__)):
    service = os.path.splitext(service)[0]
    if service.startswith("_"):
        continue

    module = importlib.import_module(f"vinetrimmer.services.{service}")

    for x in dir(module):
        x = getattr(module, x)

        if isinstance(x, type) and issubclass(x, module.BaseService) and x != module.BaseService:
            globals()[x.__name__] = x
            SERVICE_MAP[x.__name__] = x.ALIASES

            break


def get_service_key(value):
    """
    Get the Service Key name (e.g. DisneyPlus, not dsnp, disney+, etc.) from the SERVICE_MAP.
    Input value can be of any case-sensitivity and can be either the key itself or an alias.
    """
    value = value.lower()
    for key, aliases in SERVICE_MAP.items():
        if value in map(str.lower, aliases) or value == key.lower():
            return key
    raise ValueError(f"Failed to find a matching Service Key for '{value}'")
