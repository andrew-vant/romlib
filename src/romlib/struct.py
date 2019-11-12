# A structure type needs a list of fields and some way of describing how they
# relate to each other for pointer purposes.

# A structure instance gets an attribute for each field in the structure type.

# There must be some way for modules to create their own structure types for
# purposes like loading extra data after the main struct. Field descriptions
# need some way of indicating whether they should be loaded or not

# Perhaps a meta field indicating data, pointer, optional? No, pointer is
# already implied by the pointer field, so I need default-vs-optional.

# Possibility: structure type contains a list of fields to treat as
# baseload/postload/dereference, defaults to all except
# pointers/nothing/pointers

# No, should be explicit in map. Just not sure which field it belongs in.

import io
import csv
import pathlib
import logging
import inspect
from itertools import chain, permutations
from collections import OrderedDict, namedtuple
from importlib.machinery import SourceFileLoader
from pprint import pprint

import bitstring
from bitstring import ConstBitStream, Bits

import romlib
from . import util
from . import field


log = logging.getLogger(__name__)


class MetaStruct(type):
    def __init__(cls, name, bases, dct):
        # Self.fields will get set by super().__init__; this is more for
        # documentation's sake than anything else.
        super().__init__(name, bases, dct)
        if not hasattr(cls, "fields"):
            raise ValueError("Fields class variable is required.")
        if not isinstance(cls.fields, OrderedDict):
            raise ValueError("Fields class variable must be an ordereddict")

        cls.fieldmap = {}
        for field in cls.fields.values():
            # Forbid shadowing
            if field.id in dir(cls):
                msg = "Field id {}.{} conflicts with builtin method."
                raise ValueError(msg, name, field.id)

            # If a field in this array serves as an index, note it
            if field.meta == "index":
                cls.indexer = field
            else:
                cls.indexer = None
            # Make it easy to dereference labels
            #
            # I couldn't get these to work as @property @classmethods, either
            # in the class definition or (as just plain @property) in the
            # metaclass definition. It is supposedly possible but it is a giant
            # pain. I might make them instance propertymethods in
            # Should these be @property methods on instances? Maybe. They
            # should really be @property methods on classes, but I can't get
            # that to work. Descriptors MRO doesn't include metaclasses.
            cls.fieldmap[field.id] = field
            cls.fieldmap[field.label] = field

    # Deep magic begins here.
    #
    # I was trying to make @classproperty convenience methods in Structure for
    # getting useful subsets of Structure.fields. It seems there is no such
    # decorator, nor a good way to fake it. Placing them in the metaclass as
    # @properties works, though; they become class-level properties of whatever
    # class is being created, in the same way that regular classes' @properties
    # become instance-level property attributes.
    #
    # That makes perfect sense, but it feels like deep magic to me. It's not
    # enough on its own; accessing them from an instance requires
    # instance-level dispatchers, because the MRO on instances does not by
    # default include metaclass properties. See the Structure properties of the
    # same name.

    @property
    def base_fields(cls):
        """ Get a list of primary data fields in the structure.

        The list will be in the same order that they should be read from a rom
        file, so you can loop over it when reading.
        """
        return [field for field in cls.fields.values()
                if not field.pointer and field.meta != "extra"]

    @property
    def extra_fields(cls):
        """ Get a list of extended data fields after the structure.

        These are optional data fields for variable-length structures. They
        should be read after primary data, but before links. They will be
        listed in the order they appear in the structure definition file.
        """
        return [field for field in cls.fields.values()
                if field.meta == "extra"]

    @property
    def link_fields(cls):
        """ Get a list of data links in the structure.

        These are values that are pointed-to by something in the structure's
        primary data, so they have to be loaded after the primary data fields.
        """
        return [field for field in cls.fields.values()
                if field.pointer]


class Structure(object, metaclass=MetaStruct):
    # Map of field names to field classes
    # Problem: inherting must specify explicitly?
    fields = OrderedDict()

    @classmethod
    def _realkey(cls, key):
        """ Dereference labels to ids if needed."""
        return cls.fieldmap[key].id

    @property
    def base_fields(self):
        return type(self).base_fields

    @property
    def extra_fields(self):
        return type(self).extra_fields

    @property
    def link_fields(self):
        return type(self).link_fields


    def __init__(self, auto=None):
        # Non-present optional fields are represented by "None." It is an
        # error for non-optional fields to remain None at the end of
        # initialization.
        #
        # FIXME: Should structs remember their address? If they did, it would
        # make serializing and deserializing things like nested structs and
        # pointers easier, I think. Right now address saving/loading is kludged
        # in by arrays, I think.
        #
        # If I do it that way init would probably need an optional address
        # argument; otherwise it would be difficult to handle things like a
        # struct built from a bitstring slice rather than a whole file.

        data = {field.id: None for field in self.fields.values()}
        super().__setattr__("data", data)

        # Initializing using whichever method is called for by the type of
        # input.
        dispatch = {bitstring.Bits: self._init_from_bitstring,
                    io.IOBase: self._init_from_file,
                    dict: self._init_from_dict}
        for tp, func in dispatch.items():
            if isinstance(auto, tp):
                func(auto)
                break
        else:
            msg = "Invalid struct initializer of type %s"
            raise ValueError(msg, type(auto))

        # Make sure no non-optional fields are still None, which would indicate
        # somebody made a mistake.
        mandatory = chain(self.base_fields, self.link_fields)
        assert(not any(self[field.id] is None for field in mandatory))

    @classmethod
    def _delabel(cls, dct):
        for field in cls.fields.values():
            if field.label in dct:
                dct[field.id] = dct.pop(field.label)

    def _init_from_dict(self, dct):
        dct = dct.copy()
        self._delabel(dct)
        # Unions and extra fields must be loaded last, because they may rely on
        # data from other fields.
        sorter = lambda fld: (issubclass(fld, field.Union),
                              fld not in self.extra_fields)
        for fld in sorted(self.fields.values(), key=sorter):
            string = dct[fld.id]
            if fld in self.extra_fields and not string:
                self.data[fld.id] = None
            else:
                self.data[fld.id] = fld(self, string)

    def _init_from_file(self, f):
        bs = bitstring.ConstBitStream(f)
        self._init_from_bitstring(bs)

    def _init_from_bitstring(self, bs):
        self.read_base(bs)
        self.read_extra(bs)
        self.read_links(bs)

    @property
    def base_size(self):
        """ Get size of base structure in bytes."""
        bitsize = sum(field.size for field in self.base_fields)
        assert bitsize % 8 == 0
        return bitsize // 8

    def read_base(self, bs):
        """ Read primary data

        This reads the main, fixed portion of a data structure. After reading,
        the bit position of `bs` will be one bit past the end of the data read.
        """
        pos = bs.pos
        for field in type(self).base_fields:
            self.data[field.id] = field(self, bs)
        # FIXME: The following block doesn't behave as expected for indexed
        # primitive arrays, e.g. strings with no meaningful base_fields
        # Still works but I'm pretty sure a bug is waiting in it.
        """

        try:
            assert bs.pos == pos + sum(field.size for field in self.base_fields)
        except TypeError:
            pprint(self.base_fields)
            pprint(list(inspect.getmro(field) for field in self.base_fields))
            pprint(list(field.size for field in self.base_fields))
            raise
        """

    def read_extra(self, bs):
        """ Read optional data

        This is a hook for variable-length structs or structs with
        context-dependent data. It must be overridden to be of any use.
        After reading, the bit position of `bs` must be set one bit past the
        end of the data read. This will happen automatically with repeated
        bs.read calls.
        """
        if len(self.extra_fields) > 0:
            msg = "'{}' has extra fields but didn't implement them"
            raise NotImplementedError(msg, type(self))

    def read_links(self, bs):
        """ Read linked data.

        Order matters; this shouldn't be run until after pointers are read in.
        FIXME: Recursive pointers would be nice to support.

        read_links should restore the read position of `bs` before returning, but
        this is not guaranteed if an exception is thrown.
        """
        oldpos = bs.pos # Save this to reset it after reading links.
        for field in type(self).link_fields:
            offset = self[field.pointer]
            bs.pos = offset * 8
            self.data[field.id] = field(self, bs)

    @property
    def _linkmap(self):
        """ Get an offset-to-value-instance map"""
        linkmap = {}
        for field in self.link_fields:
            pointer = self.fieldmap[field.pointer]
            offset = self[pointer.id]
            linkmap[offset] = self.data[field.id]
        return linkmap


    def __setitem__(self, key, value):
        """ Set an attribute using dictionary syntax.

        Attributes can be looked by by label as well as ID.
        """
        key = self._realkey(key)
        if self.data[key] is None:
            self.data[key] = self.fields[key](self, value)
        else:
            self.data[key].value = value

    def __getitem__(self, key):
        """ Get an attribute using dictionary syntax.

        Attributes can be looked by by label as well as ID.
        """
        key = self._realkey(key)
        if self.data[key] is None:
            return None
        else:
            return self.data[key].value

    def __delitem__(self, key):
        """ Unset a field value.

        For variable-length structures or other weird edge cases, this
        indicates that a given field is unused or omitted and should be skipped
        or ignored when generating patches.

        This doesn't actually delete the underlying attribute, just sets it to
        None.
        """
        self.data[_realkey(key)] = None

    def __getattr__(self, name):
        if name in self.data:
            return self[name]
        elif hasattr(super(), "__getattr__"):
            return super().__getattr__(name)
        else:
            raise AttributeError(name)

    def __setattr__(self, name, value):
        if name in self.data:
            self[name] = value
        else:
            super().__setattr__(name, value)

    def __delattr__(self, name):
        """ Remove a field by id.

        This sets the underlying data to None, which is interpreted as
        "this field isn't present in the structure in ROM. Use for
        'optional' fields.
        """
        if name in self.ids():
            del self[name]
        else:
            super().__delattr__(name)

    def __contains__(self, key):
        try:
            key = self._realkey(key)
        except KeyError: # FIXME: Should probably use custom exception.
            return False
        return self[key] is not None

    def keys(self, *, labels=False):
        return self.labels() if labels else self.ids()

    def ids(self):
        return iter(self.fields)

    def labels(self):
        return (field.label for field in self.fields)

    def values(self):
        return (self[field.id] for field in self.fields)

    def items(self, labels=False):
        for field in self.fields.values():
            if labels:
                yield field.label, self[field.label]
            else:
                yield field.id, self[field.id]

    def offset(self, fieldname):
        """ Get the offset of a field from the start of the structure."""
        raise NotImplementedError

    def dump(self):
        """ Get a string-to-string dictionary of the structure's contents.

        Suitable for putting in a csv file or similar. All display conversions
        are handled by the fields' .string methods.
        """
        return {field.label: (field.string if field is not None else "")
                for field in self.data.values()
                if field is not None}

    def bytemap(self, offset):
        # Both the main structure and linked objects are assumed to be an even
        # number of bytes total. This has been the case for everything so
        # far...but I'm pretty sure this assumption won't hold forever.
        bytemap = {}
        bytemap.update(self.base_bytes(offset))
        bytemap.update(self.extra_bytes(offset))
        bytemap.update(self.link_bytes(offset))
        return bytemap

    # FIXME: Implement some of the below as __bytes__ instead?
    def base_bytes(self, offset):
        bits = [self.data[field.id].bits
                for field in self.base_fields]
        bits = Bits().join(bits)
        return {offset + i: byte
                for i, byte
                in enumerate(bits.bytes)}

    def extra_bytes(self, offset):
        if len(self.extra_fields) > 0:
            msg = "'{}' has extra fields but didn't implement them"
            raise NotImplementedError(msg, type(self))
        else:
            return {}

    def link_bytes(self, offset):
        bytemap = {}
        for offset, valobj in self._linkmap.items():
            for i, byte in enumerate(valobj.bits.bytes):
                bytemap[offset+i] = byte
        return bytemap



def conflict_check(structure_classes):
    """ Verify that all ids and labels in a list of structure classes are unique. """
    for cls1, cls2 in permutations(structure_classes, r=2):
        for element in ("id", "label"):
            list1 = (getattr(field, element) for field in cls1.fields.values())
            list2 = (getattr(field, element) for field in cls2.fields.values())
            dupes = set(list1).intersection(list2)
            if len(dupes) > 0:
                msg = "Duplicate {}s '{}' appear in structures '{}' and '{}'."
                msg = msg.format(element, dupes, cls1.__name__, cls2.__name__)
                raise ValueError(msg)


def output_fields(*structure_classes, use_labels=True):
    """ Return a list of field names in a sensible order for tabular output.

    This sorts based on a number of properties, in order of priority

    1. Name vs. non-name (name comes first).
    2. Explicit order in field spec.
    3. Pointer vs. non-pointer (non-pointer comes first).
    4. Order of parent structure in structure_classes.
    5. Order the field appears in the structure spec.
    """
    conflict_check(structure_classes)
    # Build a dict mapping field ids/labels to ordering tuples.
    headers = []
    record = namedtuple("record", ["header", "order"])

    for structorder, structure in enumerate(structure_classes):
        for specorder, field in enumerate(structure.fields.values()):
            nameorder = 0 if field.label == "Name" else 1
            # Cheap kludge, should really check for other fields pointing to it.
            ptrorder = 1 if field.display == "pointer" else 0
            header = field.label if use_labels else field.id
            ordering = (nameorder, field.order, ptrorder,
                        structorder, specorder)
            headers.append(record(header, ordering))

    # Sort by the ordering tuple and return the corresponding keys.
    sorter = lambda record: record.order
    return [header for header, order
            in sorted(headers, key=sorter)]


def define_struct(name, specs):
    # spec should be an iterable of dictionaries, each in the format used by
    # romlib.Field.
    # Can this be done by mucking with the input dictionary in MetaStruct
    # safely?

    fields = OrderedDict()
    for spec in specs:
        log.debug("Processing field '%s'", spec['id'])
        fid = spec['id']
        fields[fid] = field.define_field(fid, spec)
    bases = (Structure,)
    clsdict = {"fields": fields}
    cls = type(name, bases, clsdict)
    return cls


def load(path):
    path = pathlib.Path(path)  # I hate lines like this so much.
    name = path.stem
    log.debug("Loading '%s' definition from %s", name, path)
    with path.open() as f:
        specs = list(csv.DictReader(f, dialect='romtool'))
    base = define_struct(name, specs)
    modpath = path.parent.joinpath(name + '.py')
    log.debug("Looking for make_struct hook in %s", modpath)
    try:
        module = SourceFileLoader(name, str(modpath)).load_module()
    except FileNotFoundError:
        msg = "Nothing at %s, skipping"
        log.debug(msg, modpath)
        return base
    if not hasattr(module, "make_struct"):
        msg = "%s doesn't contain make_struct, skipping"
        log.debug(msg, modpath)
        return base
    msg = "Processing hook %s.make_struct for '%s' in %s"
    log.debug(msg, module.__name__, name, path)
    newbase = module.make_struct(base)
    if not isinstance(newbase, type) or not issubclass(newbase, base):
        msg = ("{}.make_struct() in {} failed to return subclass of {}; "
               "actually returned: {}")
        msg = msg.format(module.__name__, path, base.__name__, newbase)
        raise Exception(msg)
    return newbase
