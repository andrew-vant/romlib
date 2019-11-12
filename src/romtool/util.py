from os.path import realpath, dirname
from os.path import join as pathjoin

import yaml
import logging
import logging.config

log = logging.getLogger(__name__)

def whereami(path):
    """ Get the full path to the containing directory of a file.

    Intended to be called with __file__, mostly
    """
    # FIXME: should this go in util? Maybe not, nothing in romlib uses it.
    return dirname(realpath(path))

def pkgfile(filename):
    return pathjoin(whereami(__file__), filename)

def loadyaml(data):
    # Just so I don't have to remember the extra argument everywhere.
    # Should take anything yaml.load will take.
    return yaml.load(data, Loader=yaml.SafeLoader)

def debug_structure(data, loglevel=logging.DEBUG):
    """ yamlize a data structure and log it as debug """
    for line in yaml.dump(data).splitlines():
        log.log(loglevel, line)
