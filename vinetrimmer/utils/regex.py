import re


def find(pattern, string):
    return next(iter(re.findall(pattern, string)), None)
