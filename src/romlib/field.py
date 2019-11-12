import codecs
import abc
import logging
import inspect
from itertools import product, chain
from pprint import pprint

from bitstring import Bits, BitArray, ConstBitStream

import romlib
from . import util


log = logging.getLogger(__name__)

class Field(abc.ABCMeta):
    def __new__(cls, name, bases, dct):
        # If a field base class defines a propertymethod shadowing part
        # of the field's tsv definition, the propertymethod takes
        # priority and the value in the definition is prepended with an
        # underscore (so the propmethod has access to it in a standard
        # place).  Right now this is only useful for unions, which need
        # methods for the type and mod fields (because they're not
        # fixed)
        for base in bases:
            # We *do* want to be able to override property methods with *other
            # propertymethods* in explicitly derived classes...just not raw
            # strings. So skip anything that's already of property type.
            shadows = [attr for attr, value in dct.items()
                       # Not overridden by class being constructed....
                       if not isinstance(value, property)
                       # But *is* overridden in the base class.
                       and isinstance(getattr(base, attr, None), property)]

            # So e.g. if a union base class defines .type(), move any .type
            # primitive in the tsv to ._type (where it can be retrieved if
            # needed)
            for attr in shadows:
                dct['_'+attr] = dct.pop(attr)

        return super().__new__(cls, name, bases, dct)

    def __init__(cls, name, bases, dct):
        super().__init__(name, bases, dct)

    @property
    def bytesize(cls):
        if cls.size is None:
            return None
        else:
            return util.divup(cls.size, 8)



class Value(object, metaclass=Field):
    # Default values
    #
    # The mandatory ones should probably be defined as abstract propertymethods
    # or something similar, but I'm not sure exactly what, or how, or whether
    # it's worth bothering.
    id = None
    label = None
    type = None
    size = None
    order = 0
    mod = None
    display = None
    pointer = None
    comment = ""
    meta = None

    def __init__(self, parent, auto=None, value=None, bs=None, string=None):
        self.data = BitArray(self.size)
        if not isinstance(parent, romlib.struct.Structure):
            raise ValueError("Invalid field parent: {}".format(parent))
        self.parent = parent
        if isinstance(auto, ConstBitStream):
            bs = auto
        elif isinstance(auto, str):
            string = auto
        else:
            value = auto
        numargs = sum(1 for arg in (value, bs, string) if arg is not None)
        assert numargs == 1
        if value is not None:
            self.value = value
        elif bs is not None:
            self.bits = bs
        elif string is not None:
            self.string = string

    @property
    def bits(self):
        return Bits(self.data)

    @bits.setter
    def bits(self, bs):
        self.data = bs.read(self.size)

    @property
    @abc.abstractmethod
    def value(self):
        raise NotImplementedError

    @value.setter
    @abc.abstractmethod
    def value(self, value):
        raise NotImplementedError


    # FIXME: Implement __str__ and maybe __repr__ instead?
    @property
    @abc.abstractmethod
    def string(self):
        raise NotImplementedError

    @string.setter
    @abc.abstractmethod
    def string(self, s):
        raise NotImplementedError

class Number(Value):
    size = 8
    mod = 0
    display = ""

    @property
    def value(self):
        return getattr(self.data, self.type) + self.mod

    @value.setter
    def value(self, value):
        args = {self.type: value - self.mod,
                "length": self.size}
        self.bits = ConstBitStream(**args)

    @property
    def string(self):
        fstr = util.int_format_str(self.display, self.size)
        return fstr.format(self.value)

    @string.setter
    def string(self, s):
        self.value = int(s, 0)

class Array(Value):
    # Note, this is neither able nor intended to act as a "real" list, or any
    # other kind of iterable. If you want that, take .value.

    size = 8
    mod = "uint:8" #type and bits of items
    separator = " "

    def __init__(self, *args, **kwargs):
        tp, width = self.mod.split(":")
        self.itemwidth = int(width)
        self.itemtype = tp
        assert self.size % self.itemwidth == 0
        super().__init__(*args, **kwargs)

    def __len__(self):
        return self.size // self.itemwidth

    @property
    def value(self):
        fmt = [self.mod] * len(self)
        return self.data.unpack(fmt)

    @value.setter
    def value(self, value):
        """ update the array with a list of stuff """
        initializers = ["{}={}".format(self.mod, item)
                        for item in value]
        self.data = BitArray(",".join(initializers))

    @property
    def string(self):
        return self.separator.join(str(i) for i in self.value)

    @string.setter
    def string(self, s):
        initializers = ["{}={}".format(self.mod, item)
                        for item in s.split(self.separator)]
        if len(initializers) != len(self):
            raise ValueError("Array length doesn't match.")
        self.data = BitArray(",".join(initializers))


class String(Value):
    display = "ascii"

    @property
    def bits(self):
        return super().bits

    @bits.setter
    def bits(self, bs):
        # The issue with reading in strings is that we don't necessarily know
        # how long the string actually is...and the only way we have of finding
        # out is decoding it
        #
        # This is an ugly hack, the correct was is probably to build a real
        # bitstring-to-string-and-back codec to go along with the
        # bytes-to-string codec

        codec = codecs.lookup(self.display)
        size = self.size if self.size is not None else 1024*8
        pos = bs.pos
        tmp = bs.read(size)
        string, length = codec.decode(tmp.bytes)
        # What we actually want is not the decoded string but the bits that
        # made it up...
        bs.pos = pos
        self.data = bs.read(length * 8)


    @property
    def string(self):
        return codecs.decode(self.data.bytes, self.display)

    @string.setter
    def string(self, value):
        data = codecs.encode(value, self.display)
        if self.size is not None:
            bytesize = self.size // 8
            if len(data) > bytesize:
                msg = "String '%s' (%s bytes) too long for field '%s' (%s bytes)"
                raise ValueError(msg, value, len(data), self.name, bytesize)
            if len(data) < bytesize:
                # Pad short strings with spaces. Note that padbyte here is a
                # bytes object (i.e. an iterable), even though it's only a
                # single byte long.
                padbyte = codecs.encode(" ", self.display)
                padding = padbyte * (bytesize - len(data))
                data += bytes(padding)
        bs = BitArray(bytes=data)
        assert self.size is None or len(bs) == self.size
        self.data = bs

    @property
    def value(self):
        return self.string

    @value.setter
    def value(self, value):
        self.string = value


class Bitfield(Value):
    mod = "msb0"

    @property
    def string(self):
        return util.displaybits(self.value, self.display)

    @string.setter
    def string(self, s):
        self.value = ConstBitStream(bin=util.undisplaybits(s, self.display))

    @property
    def value(self):
        bs = self.bits
        if self.mod == "msb0":
            return self.bits
        elif self.mod == "lsb0":
            return util.lbin_reverse(self.bits)
        else:
            msg = "Bit ordering '%s' is not a thing"
            raise NotImplementedError(msg, self.mod)

    @value.setter
    def value(self, value):
        if self.mod == "msb0":
            self.bits = value
        elif self.mod == "lsb0":
            self.bits = util.lbin_reverse(value)
        else:
            msg = "Bit ordering '%s' is not a thing"
            raise NotImplementedError(msg, self.mod)


class Union(Number, String, Bitfield):
    # These need sane defaults if subclasses don't override them.
    _type = "uint"
    _mod = 0

    @property
    @abc.abstractmethod
    def type(self):
        """ Get the currently-applicable type of this field."""
        raise NotImplementedError
        return self._type

    @property
    def mod(self):
        # Derived classes may or may not need to override this. The default
        # tries to treat the mod attribute as an integer if the current type is
        # an integer, and tries to treat it as msb0/lsb0 if the current type is
        # a bitfield; otherwise it gives up and returns None. This will work
        # for int/bin unions as long as you don't need to mod both forms.
        if "int" in self.type:
            return util.intify(self._mod, 0)
        elif "bin" in self.type:
            return self._mod if self._mod else "msb0"
        else:
            return None

    @property
    def bits(self):
        return lookup(self.type).bits.fget(self)

    @bits.setter
    def bits(self, bs):
        lookup(self.type).bits.fset(self, bs)

    @property
    def value(self):
        return lookup(self.type).value.fget(self)

    @value.setter
    def value(self, value):
        lookup(self.type).value.fset(self, value)

    @property
    def string(self):
        return lookup(self.type).string.fget(self)

    @string.setter
    def string(self, s):
        lookup(self.type).string.fset(self, s)


def define_field(name, spec):
    spec = fixspec(spec.copy())
    base = lookup(spec['type'])
    cls = type(name, (base,), spec)
    return cls

_registered_fields = {}

def lookup(tp):
    if tp in _registered_fields:
        return _registered_fields[tp]
    elif "int" in tp or "float" in tp:
        return Number
    elif "bin" in tp:
        return Bitfield
    elif "str" in tp:
        return String
    elif "union" in tp:
        return Union
    elif "array" in tp:
        return Array
    else:
        return Value

def register(field):
    """ Make a custom field type available as a base."""
    _registered_fields[field.__name__] = field

def fixspec(spec):
    """ Unstringify any properties from spec that need it"""
    # FIXME: Move this into MetaField.__new__?
    unstring = {
            'size': lambda s: util.tobits(s, 0),
            'order': lambda s: util.intify(s),
            'mod': lambda s: util.intify(s, s)
            }

    # empty strings get stripped out; the parent provides defaults.
    for k, v in spec.copy().items():
        if v == "":
            del(spec[k])

    # Uncast string values get cast
    for key, func in unstring.items():
        if key in spec and isinstance(spec[key], str):
            spec[key] = func(spec[key])
    return spec
