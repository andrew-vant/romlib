import logging
import os
import csv
import sys
import itertools

import yaml

from bitstring import ConstBitStream, Bits
from collections import OrderedDict
from csv import DictWriter
from pprint import pprint

from . import text
from .util import tobits, OrderedDictReader, merge_dicts, flatten
from .exceptions import RomMapError
from .patch import Patch


class Address(object):
    """ Manage and convert between rom offsets and pointer formats."""

    def __init__(self, offset, schema="offset"):
        funcname = "_from_{}".format(schema)
        converter = getattr(self, funcname)
        self._address = converter(offset)

    @property
    def rom(self):
        """ Use this address as a ROM offset. """
        return self._address

    @property
    def hirom(self, mirror=0xC00000):
        """ Use this address as a hirom pointer.

        Use this when writing pointers back to a hirom image. There are
        multiple rom mirrors in hirom; this defaults to using the C0-FF
        mirror, since it contains all possible banks.
        """
        # hirom has multiple possible re-referencings, but C0-FF should
        # always be safe.
        return self._address | mirror

    @classmethod
    def _from_offset(cls, offset):
        """ Initialize an address from a ROM offset. """
        return offset

    @classmethod
    def _from_hirom(cls, offset):
        """ Initialize an address from a hirom pointer. """
        # hirom has multiple mirrors, but I *think* this covers all of them...
        return offset % 0x400000


class RomMap(object):
    def __init__(self, root):
        self.structs = {}
        self.texttables = {}
        self.arrays = {}
        self.arraysets = {}

        # Find all the csv files in the structs directory and load them into
        # a dictionary keyed by their base name.
        structfiles = [f for f
                       in os.listdir("{}/structs".format(root))
                       if f.endswith(".csv")]
        for sf in structfiles:
            typename = os.path.splitext(sf)[0]
            struct = StructDef.from_file("{}/structs/{}".format(root, sf))
            self.structs[typename] = struct

        # Repeat for text tables.
        try:
            ttfiles = [f for f
                       in os.listdir("{}/texttables".format(root))
                       if f.endswith(".tbl")]
        except FileNotFoundError:
            # FIXME: Log warning here?
            ttfiles = []

        for tf in ttfiles:
            tblname = os.path.splitext(tf)[0]
            tbl = text.TextTable("{}/texttables/{}".format(root, tf))
            self.texttables[tblname] = tbl

        # Now load the array definitions.
        with open("{}/arrays.csv".format(root)) as f:
            arrays = [ArrayDef(od, self.structs)
                      for od in OrderedDictReader(f)]
            self.arrays = {a['name']: a for a in arrays}
            arraysets = set([a['set'] for a in arrays])
            for _set in arraysets:
                self.arraysets[_set] = [a for a in arrays if a['set'] == _set]

    def dump(self, rom, folder, allow_overwrite=False):
        """ Look at a ROM and export all known data to folder."""
        stream = ConstBitStream(rom)
        mode = "w" if allow_overwrite else "x"

        # Black magic begins here. We want to go through each set of arrays
        # and merge the corresponding structures from each, then output the
        # result. We want the output fields to remain well-ordered, and we
        # want the name field at the front if it is there.

        for entity, arrays in self.arraysets.items():

            # Read in each array, then dereference any pointers in their
            # respective items, then merge them so we get a single dict
            # for each object.
            data = [array.read(stream) for array in arrays]
            data = [[item.struct_def.dereference_pointers(item, self, rom)
                     for item in array] for array in data]
            try:
                data = [merge_dicts(parts) for parts in zip(*data)]
            except ValueError as e:
                # FIXME: These checks should really be done in init.
                msg = "The arrays in set {} have overlapping keys: {}"
                raise RomMapError(msg.format(entity, e.overlap))

            # Now work out what field IDs need to be included in the output.
            # This is the union of the IDs available in each array.
            # We also need to know what human-readable labels to print on
            # the first row.

            headermap = OrderedDict()
            for array in arrays:
                s = array.struct
                allfields = itertools.chain(s.values(), s.pointers)
                od = OrderedDict((field['id'], field['label'])
                                 for field in allfields)
                headermap.update(od)

            # If the object has a name, move it to the front of the output.
            name = next((k for k, v in headermap.items() if v == "Name"), None)
            if name is not None:
                headermap.move_to_end(name, False)

            # Now dump.
            fname = "{}/{}.csv".format(folder, entity)
            with open(fname, mode, newline='') as f:
                writer = DictWriter(f, headermap.keys(), quoting=csv.QUOTE_ALL)
                writer.writerow(headermap)
                for item in data:
                    writer.writerow(item)

    def makepatch(self, romfile, modfolder, patchfile):
        # Get the filenames for all objects. Assemble a dictionary mapping
        # object types to all objects of that type.
        files = [f for f in os.listdir(modfolder)
                 if f.endswith(".csv")]
        paths = [os.path.join(modfolder, f) for f in files]
        objnames = [os.path.splitext(f)[0] for f in files]
        objects = {}
        for name, path in zip(objnames, paths):
            with open(path) as f:
                objects[name] = list(OrderedDictReader(f))

        # This mess splits the object-to-data mapping into an array-to-data
        # mapping. This should really be functioned out and unit tested
        # because it is confusing as hell.
        data = {array['name']: [] for array in self.arrays.values()}
        for otype, objects in objects.items():
            for array in self.arraysets[otype]:
                for o in objects:
                    data[array['name']].append(array.struct.extract(o))

        # Now get a list of bytes to change.
        changed = {}
        for arrayname, contents in data.items():
            a = self.arrays[arrayname]
            offset = int(a['offset'] // 8)
            stride = int(a['stride'] // 8)
            for item in contents:
                changed.update(a.struct.changeset(item, offset))
                offset += stride
        # Generate the patch
        p = Patch(changed)
        with open(romfile, "rb") as rom:
            p.filter(rom)
        with open(patchfile, "wb+") as patch:
            p.to_ips(patch)


class ArrayDef(OrderedDict):
    requiredproperties = ["name", "type", "offset",
                          "length", "stride", "comment"]

    def __init__(self, od, structtypes={}):
        super().__init__(od)
        if self['type'] in structtypes:
            self.struct = structtypes[self['type']]
        else:
            self.struct = StructDef.from_primitive_array(self)
        self['offset'] = tobits(self['offset'])
        self['stride'] = tobits(self['stride'])
        if not self['set']:
            self['set'] = self['name']
        if not self['label']:
            self['label'] = self['name']

    def read(self, stream):
        for i in range(int(self['length'])):
            pos = i*self['stride'] + self['offset']
            yield self.struct.read(stream, pos)


class Struct(object):
    def __init__(self, definition):
        """ Create an empty structure of a given type."""
        self.definition = definition
        self.data = definition._datant()
        self.calculated = definition._pointersnt()

    @classmethod
    def from_dict(cls, definition, d):
        """ Create a structure with data taken from a dictionary. """
        out = Struct(definition)
        allfields = definition.fields + definition.pointers
        unlabel = {f['label']: f['id'] for f
                   in definition.fields + definition.pointers}
        for name, data in d.items():
            name = unlabel.get(name, name)
            if name in vars(self.data):
                setattr(self.data, data)
            elif name in vars(self.calculated):
                setattr(self.calculated, data)
        return out

    def read(self, stream, offset=None):
        """ Read data into a structure from a bitstream.

        The offset is the location in the stream where the structure begins. If
        the stream was created from a file, then it's the offset in the file.
        If offset is None, the current stream position will be used.

        Returns an object with attributes for each field of the structure.
        """
        if offset is not None:
            stream.seek(offset)
        fmt = ["{}:{}".format(f['type'], f['size'])
               for f in self.definition.fields]
        self.data = self.definition._datant(bitstream.readlist(fmt))
        for f in self.definition.fields:
            value = stream.read("{}:{}".format(f['type'], f['size']))
            setattr(self.data, value)

    def to_od(self):
        """ Create an ordered dict from the struct's properties.
        
        The output will be human readable and suitable for saving to a csv
        file. The name will come first, then regular properties in definition-
        order, then computed properties in definition-order, then pointer
        properties in definition-order.
        """
        out = OrderedDict()
        for fid, label in self.definition._output_fields():
            value = getattr(self.data, fid,
                    getattr(self.calculated, fid.lstrip('*'), ""))
            out[label] = value
        return out            

    def to_bytes(self):
        """ Generate a bytes object from the struct's properties.
        
        The output will be suitable for writing back to the ROM or generating
        a patch. Currently it outputs normal data fields only.
        """
        bitinit = []
        for field in self.definition.fields:
            tp = field['type']
            size = field['size']
            value = getattr(self.data, field['id'])
            bitinit.append("{}:{}={}".format(tp, size, value))
        return Bits(", ".join(bitinit)).bytes

    def calculate(self, f):
        """ Dereference pointers and populate calculated properties. """
        for ptr in definition.pointers:
            # Take off the asterisk to get the id of the pointer field. Convert
            # that pointer to a ROM address, then read from that address.
            fid = ptr['id'][1:]
            archaddr = getattr(self.data, fid)
            romaddr = Address(archaddr, ptr['ptype']).rom
            if ptr['type'] == "strz":
                ttable = self.tbl[ptr['display']]
                s = ttable.readstring(f, romaddr)
                setattr(self.calculated, fid, s)


class StructDef(object):
    def __init__(self, name, fields, texttables=None):
        """ Create a structure definition.
        
        name: The class name of this type of structure.
        fields: A list of dictionaries defining this structure's fields.
        texttables: A dictionary of text tables for decoding strings.
        """
        self.tbl = texttables
        fields = list(fields)  # In case we were passed a generator
        self.pointers = [f for f in fields if self.ispointer(f)]
        self.fields = [f for f in fields if self.isdata(f)]
        for f in self.fields:
            f['size'] = tobits(f['size'])

        fids = [f['id'] for f in self.fields]
        pids = [f['id'] for f in self.pointers]
        self._datant = namedtuple(name, ",".join(fids))
        self._pointersnt = namedtuple(name, ",".join(pids))

    @classmethod
    def from_file(cls, name, f):
        return StructDef(name, OrderedDictReader(f))

    @classmethod
    def from_primitive_array(cls, arrayspec):
        spec = OrderedDict()
        spec['id'] = arrayspec['name']
        spec['label'] = arrayspec['label']
        spec['size'] = arrayspec['stride']
        spec['type'] = arrayspec['type']
        spec['display'] = arrayspec['display']
        spec['tags'] = arrayspec['tags']
        spec['comment'] = arrayspec['comment']
        spec['order'] = ""
        return StructDef(arrayspec['name'], [spec])

    def _output_fields(self):
        """ Get the ordering for data fields in csv output."""
        allfields = self.fields + self.pointers
        name = next(f for f in allfields if self.isname(f))
        normal = [f for f in self.fields if not self.isname(f)]
        pointers = [f for f in self.pointers if not self.isname(f)]
        return [(f['id'], f['label']) for f in [name, *normal, *pointers]]

    @classmethod
    def isdata(cls, field):
        return field['id'].isalnum()

    @classmethod
    def ispointer(cls, field):
        return field['id'][0] == "*"

    @classmethod
    def isname(cls, field):
        return field['label'] == "Name"

    @property
    def bytes(self):
        """ The size of this structure in bytes."""
        return sum(f['size'] / 8 for f in self.fields)
    
    @property
    def bits(self):
        """ The size of this structure in bits."""
        return sum(f['size'] for f in self.fields)

    def changeset(self, item, offset):
        """ Get all changes made by item """
        initializers = []
        for fid, value in item.items():
            size = self[fid]['size']
            ftype = self[fid]['type']
            # bitstring can't implicitly convert ints expressed as hex
            # strings, so let's do it ourselves.
            if "int" in ftype:
                value = int(value, 0)
            initializers.append("{}:{}={}".format(ftype, size, value))
        itemdata = Bits(", ".join(initializers))
        return {offset+i: b for i, b in enumerate(itemdata.bytes)}


def hexify(i, length=None):
    """ Converts an integer to a hex string.

    If bitlength is provided, the string will be padded enough to represent
    at least bitlength bits, even if those bits are all zero.
    """
    if length is None:
        return hex(i)

    numbytes = length // 8
    if length % 8 != 0:  # Check for partial bytes
        numbytes += 1
    digits = numbytes * 2  # Two hex digits per byte
    fmtstr = "0x{{:0{}X}}".format(digits)
    return fmtstr.format(i)

display = {
    "hexify": lambda value, field: hexify(value, field['size']),
    "hex": lambda value, field: hexify(value, field['size']),
    "": lambda value, field: value
}
