#!/usr/bin/env python

import keyword
import re
from typing import Dict

from docker_image.reference import Reference

DockerReference = Reference


def make_python_identifier(string):
    """
    Takes an arbitrary string and creates a valid Python identifier.

    Identifiers must follow the convention outlined here:
        https://docs.python.org/2/reference/lexical_analysis.html#identifiers
    """

    # create a working copy (and make it lowercase, while we're at it)
    s = string.lower()

    # remove leading and trailing whitespace
    s = s.strip()

    # Make spaces into underscores
    s = re.sub('[\\s\\t\\n]+', '_', s)

    # Remove invalid characters
    s = re.sub('[^0-9a-zA-Z_]', '', s)

    # Remove leading characters until we find a letter or underscore
    s = re.sub('^[^a-zA-Z_]+', '', s)

    # Check that the string is not a python identifier
    while s in keyword.kwlist:
        if re.match(".*?_\d+$", s):
            i = re.match(".*?_(\d+)$", s).groups()[0]
            s = s.strip('_'+i) + '_'+str(int(i)+1)
        else:
            s += '_1'

    return s


def deep_update(dst: Dict, src: Dict) -> Dict:
    """ Deep update dst dictionary with src dictionary.

    Args:
        dst: Dictionary to update
        src: Dictionary to update with

    Returns:
        New merged dictionary.
    """

    for key, value in src.items():
        if isinstance(value, dict):
            node = dst.setdefault(key, {})
            deep_update(node, value)
        else:
            dst[key] = value
    return dst
