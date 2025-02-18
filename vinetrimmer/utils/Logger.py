import logging
import sys
from io import IOBase

import coloredlogs

LOG_FORMAT = "{asctime} [{levelname[0]}] {name} : {message}"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
LOG_STYLE = "{"
LOG_FORMATTER = logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT, LOG_STYLE)


class Logger(logging.Logger):
    def __init__(self, name="vt", level=logging.NOTSET, color=True):
        """Initialize the logger with a name and an optional level."""
        super().__init__(name, level)
        if self.name == "vt":
            self.add_stream_handler()
        if color:
            self.install_color()

    def exit(self, msg, *args, code=1, **kwargs):
        """
        Log 'msg % args' with severity 'CRITICAL' and terminate the program
        with a default exit code of 1.

        To pass exception information, use the keyword argument exc_info with
        a true value, e.g.

        logger.exit("Houston, we have a %s", "major disaster", exc_info=1)
        """
        self.critical(msg, *args, **kwargs)
        sys.exit(code)

    def add_stream_handler(self, stream=None):
        """Add a stream handler to log. Stream defaults to stdout."""
        sh = logging.StreamHandler(stream)
        sh.setFormatter(LOG_FORMATTER)
        self.addHandler(sh)

    def add_file_handler(self, fp):
        """Convenience alias func for add_stream_handler, deals with type of fp object input."""
        if not isinstance(fp, IOBase):
            fp = open(fp, "w", encoding="utf-8")
        self.add_stream_handler(fp)

    def install_color(self):
        """Use coloredlogs to set up colors on the log output."""
        if self.level == logging.DEBUG:
            coloredlogs.install(level=self.level, fmt=LOG_FORMAT, datefmt=LOG_DATE_FORMAT, style=LOG_STYLE)
        coloredlogs.install(level=self.level, logger=self, fmt=LOG_FORMAT, datefmt=LOG_DATE_FORMAT, style=LOG_STYLE)


# Cache already used loggers to make sure their level is preserved
_loggers = {}


# noinspection PyPep8Naming
def getLogger(name=None, level=logging.NOTSET):
    name = name or "vt"
    _log = _loggers.get(name, Logger(name))
    _log.setLevel(level)
    return _log
