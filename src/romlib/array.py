import struct
import collections
import logging
from collections import OrderedDict
from itertools import chain

from . import util, struct


log = logging.getLogger(__name__)

# When arrays are serialized to an external file, the user will probably want
# to be able to rearrange them to suit whatever they're working on. But they
# need to be in their original order for patching to work correctly. Array
# serialization adds a column recording the original order; IDXHDR is the
# string that column's header will use.
IDXHDR = "_idx_"

class FixedIndex(object):
    def __init__(self, offset, stride, length, **kwargs):
        # kwargs is just there to eat extras so you can double splat a spec
        # dict.
        convert = lambda i: int(i, 0) if isinstance(i, str) else i
        self.offset = convert(offset)
        self.stride = convert(stride)
        self.length = convert(length)

    def indices(self):
        for i in range(self.length):
            yield self.stride * i + self.offset


class CrossIndex(object):
    def __init__(self, data, attr=None):
        """ Index on a list of structures.

        `data` should contain a list of structures as returned by Array.read or
        Array.load. `attr` should be the field id of the piece of the structure
        that serves as the index. attr defaults to the first field
        alphabetically. For single-element indexes (i.e. most of them), it's
        okay not to provide attr.

        Be aware that changing the items in data will also change what this
        index returns.
        """
        if not attr:
            attr = sorted(data[0].ids())[0]
        self.attr = attr
        self.array = data

    def indices(self):
        for item in self.array:
            yield item[self.attr]

class Array(object):
    """ Really just an unpacker for specs"""
    def __init__(self, spec, struct=None):
        self.name = spec['name']
        self.set = spec['set']
        self.struct = struct if struct else primitive(spec)
        self.index = spec['index']
        self.priority = util.intify(spec['priority'], 0)
        self.source = spec.get('source', 'rom')
        if not self.index:
            self.index = FixedIndex(**spec)

    def read(self, rom, index=None):
        if not index:
            index = self.index
        bs = util.bsify(rom)
        for i, offset in enumerate(index.indices()):
            log.debug("Reading %s #%s", self.name, i)
            bs.pos = offset * 8
            yield self.struct(bs)


    def load(self, dicts):
        """ Deserialize this array from an iterable of dicts."""
        sorter = lambda d: util.intify(d.get('_idx_', None))
        for dct in sorted(dicts, key=sorter):
            yield self.struct(dct)


    def dump(self, structs):
        # This could be top-level but it's here for API consistency
        for struct in structs:
            yield struct.dump()


    def bytemap(self, structs, index=None):
        if not index:
            index = self.index
        bytemap = {}
        for offset, struct in zip(index.indices(), structs):
            bytemap.update(struct.bytemap(offset))
        return bytemap

def primitive(aspec):
    """ Create a structure for use by an array of primitives"""
    sspec = {
            "id": aspec['name'],
            "label": aspec['label'],
            "type": aspec['type'],
            "size": aspec['stride'],
            "mod": aspec['mod'],
            "display": aspec['display']
            }
    name = aspec['name']
    return struct.define_struct(name, [sspec])


def mergedump(arraydata, use_labels=True, record_order=True):
    """ Splice and dump multiple arrays that are part of a set.

    record_order adds an extra key recording the original order of the items in
    the arrays, so it can be preserved when reading back a re-sorted file.

    The returned items are OrderedDicts. This means .keys() can be used to get
    the appropriate headers for exporting to tsv.
    """
    keys = None
    for i, structs in enumerate(zip(*arraydata)):
        if keys is None:
            classes = [type(structure) for structure in structs]
            keys = struct.output_fields(*classes)
        merged = dict(chain.from_iterable(s.dump().items() for s in structs))
        od = OrderedDict((key, merged.get(key, "")) for key in keys)
        od['_idx_'] = i
        yield od

def from_tsv(path, structs):
    with open(path) as f:
        specs = list(util.OrderedDictReader(f, dialect='romtool'))

    # The order in which arrays are processed matters. Indexes need to be
    # loaded before the arrays that require them. Also, in the event that
    # pointers in different arrays go to the same place and only one is
    # later edited, the last one loaded wins. The 'priority' column lets
    # the map-maker specify the winner of such conflicts by ensuring
    # higher-priority arrays get processed last.

    indexnames = set([spec['index'] for spec in specs if spec['index']])
    sorter = lambda spec: spec['name'] not in indexnames
    specs.sort(key=lambda spec: (spec['name'] not in indexnames,
                                 util.intify(spec.get('priority', 0))))
    arrays = []
    for spec in specs:
        log.debug("Loading array: '%s'", spec['name'])
        structure = structs.get(spec['type'], None)
        arrays.append(Array(spec, structure))
    sorter = lambda arr: (isinstance(arr.index, str), arr.priority)
    return sorted(arrays, key=sorter)
