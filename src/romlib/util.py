""" Various utility functions used in romlib."""

import csv
import contextlib
import logging
import os
from collections import OrderedDict
from os.path import dirname, realpath
from os.path import join as pathjoin

from bitstring import ConstBitStream


log = logging.getLogger(__name__)
libroot = dirname(realpath(__file__))

# romtool's expected format is tab-separated values, no quoting, no
# escaping (i.e. tab literals aren't allowed)

csv.register_dialect(
        'romtool',
        delimiter='\t',
        lineterminator=os.linesep,
        quoting=csv.QUOTE_NONE,
        strict=True,
        )


class OrderedDictReader(csv.DictReader):  # pylint: disable=R0903
    """ Read a csv file as a list of ordered dictionaries.

    This has one additional option over DictReader, "orderfunc", which
    specifies a sorting key over a dictionary's items. If it is not provided,
    the dictionary items will be sorted in the same order as the csv's columns.
    This makes it possible to re-write the same file without interfering with
    its column order.

    Note that a corresponding OrderedDictWriter is not needed; supply an
    ordered dictionary's .keys() as the fieldnames for a regular DictWriter.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._orderfunc = kwargs.get("orderfunc",
                                     lambda t: self.fieldnames.index(t[0]))

    def __next__(self):
        d = super().__next__()  # pylint: disable=invalid-name
        return OrderedDict(sorted(d.items(), key=self._orderfunc))


class CheckedDict(dict):
    """ A dictionary that warns you if you overwrite keys."""

    cmsg = "Conflict: %s: %s replaced with %s."

    def __setitem__(self, key, value):
        self._check_conflict(key, value)
        super().__setitem__(key, value)

    def update(self, *args, **kwargs):
        d = dict(*args, **kwargs)
        for k, v in d.items():
            self._check_conflict(k, v)
        super().update(d)

    def _check_conflict(self, key, value):
        if key in self and value != self[key]:
            log.debug(self.cmsg, key, self[key], value)


@contextlib.contextmanager
def loading_context(listname, name, index=None):
    """ Context manager for loading lists or files.

        listname -- a name for the list or file being iterated over.
        name     -- a name for the specific item being loaded.
        index    -- The index of the item being loaded. Typically a linenum.
    """
    if index is None:
        index = "Unknown"
    try:
        yield
    except Exception as ex:
        msg = "{}\nProblem loading {} #{}: {}"
        msg = msg.format(ex.args[0], listname, index, name)
        ex.args = (msg,) + ex.args[1:]
        raise


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


def displaybits(bits, display):
    # FIXME: The chronic problem here is that spreadsheets insist on
    # interpreting bitfields like 00001000 as 1000. Fields that are all
    # numerals are treated as integers, and tsv provides no method of
    # indicating otherwise.
    #
    # For now I'm going to use the 0b prefix on unformatted strings to force
    # spreadsheets to treat it as a string (because it has a letter in it)
    # while still being implicitly binary.
    out = ""
    if not display:
        out += "0b"
        display = "?" * len(bits)
    if len(bits) != len(display):
        raise ValueError("display length doesn't match bitfield length.")

    for bit, letter in zip(bits, display):
        trtable = {False: letter.lower(),
                   True: letter.upper(),
                   '0': letter.lower(),
                   '1': letter.upper()}
        if letter == "?":
            out += "1" if bit else "0"
        else:
            out += trtable[bit]
    return out


def undisplaybits(s, display):
    if not display:
        if not s.startswith("0b"):
            raise ValueError("Unformatted bitfields must start with 0b")
        return s[2:]
    if not len(s) == len(display):
        raise ValueError("display length doesn't match string length.")

    out = ""
    for i, (char, letter) in enumerate(zip(s, display)):
        if letter == "?":
            trtable = {'0': '0',
                       '1': '1'}
        else:
            trtable = {letter.lower(): '0',
                       letter.upper(): '1'}
        try:
            out += trtable[char]
        except KeyError:
            errstr = "{}[{}]{}".format(s[:i], char , s[i+1:])
            msg = "Unrecognized or out of order bitfield character: {}"
            raise ValueError(msg.format(errstr))
    return out


def str_reverse(s):
    return s[::-1]


def lbin_reverse(bs):
    """ Reverse the bits in each byte of a bitstring.

    Used when the source data assumes LSB-0. This may not do what you expect
    if the input is both >1 byte and not an even number of bytes.
    """
    substrings = [bs[i:i+8] for i in range(0, len(bs), 8)]
    revstrings = [bs[::-1] for bs in substrings]
    # This makes it work both before and after string conversion
    return type(bs)().join(revstrings)


def remap_od(odict, keymap):
    """ Rename the keys in an ordereddict while preserving their order.

    keymap: A dictionary mapping old key names to new key names.

    keys not in keymap will be left alone.
    """
    newkeys = (keymap.get(k, k) for k in odict.keys())
    return OrderedDict(zip(newkeys, odict.values()))


def merge_dicts(dicts, allow_overlap=False):
    """ Merge an arbitrary number of dictionaries.

    Optionally, raise an exception on key overlap.
    """
    if not dicts:
        return {}
    if len(dicts) == 1:
        return dicts[0]
    if not allow_overlap:
        keys = [set(d.keys()) for d in dicts]
        overlap = keys[0].intersection(*keys)
        if overlap:
            msg = "Attempt to merge dicts with overlapping keys, keys were: {}"
            err = ValueError(msg.format(overlap))
            err.overlap = overlap
            raise err

    out = type(dicts[0])()  # To account for OrderedDicts
    for d in dicts:  # pylint: disable=invalid-name
        out.update(d)
    return out


def tobits(size, default=-1):
    """ Convert a size specifier to number of bits.

    size should be a string containing a size specifier. e.g. 4 (4 bytes),
    or b4 (4 bits). If size is not a valid size specifier and default is
    not set, ValueError will be raised. If default is set, it will be returned
    for strings that are invalid sizes, such as empty strings.
    """

    isbits = size.startswith('b')
    try:
        if isbits:
            bits = int(size[1:])
        else:
            bits = int(size, 0) * 8
    except ValueError:
        # Note: I use -1 as the default rather than None because I want to
        # allow None as a legitimate default value.
        if default != -1:
            bits = default
        else:
            raise
    return bits


def filebytes(f):
    """ Get an iterator over the bytes in a file."""
    byte = f.read(1)
    while byte:
        yield byte[0]
        byte = f.read(1)


def bit_offset(source):
    """ Find the current read position of *source*, in bits.

    Used for cases where *source* may be a file, a bitstring, or a bytes.
    Returns f.tell(), bs.pos, or 0 respectively.
    """
    # Don't like this if chain but try/except comes out worse...
    if hasattr(source, 'tell'):
        return source.tell() * 8
    elif hasattr(source, 'pos'):
        return source.pos
    else:
        return 0


def bsify(source, cls=ConstBitStream):
    """ Convert source to a bitstream if, and only if, necessary.

    Current read position is preserved if possible.

    This turned out to be necessary because apparently creating a
    ConstBitStream is expensive. Field converting a data source to CBS on every
    read was taking up 80% of runtime.
    """
    if isinstance(source, cls):
        return source
    else:
        pos = bit_offset(source)
        bs = cls(source)
        bs.pos = pos
        return bs


def divup(a, b):  # pylint: disable=invalid-name
    """ Divide A by B with integer division, rounding up instead of down."""
    # Credit to stackoverflow: http://stackoverflow.com/a/7181952/4638839
    return (a + (-a % b)) // b


def intify(x, default=0):  # pylint: disable=invalid-name
    """ A forgiving int() cast; returns default if typecast fails."""
    try:
        return int(x, 0)
    except (ValueError, TypeError):
        return default


def get_subfiles(root, folder, extension):
    try:
        filenames = [filename for filename
                     in os.listdir("{}/{}".format(root, folder))
                     if filename.endswith(extension)]
        names = [os.path.splitext(filename)[0]
                 for filename in filenames]
        paths = ["{}/{}/{}".format(root, folder, filename)
                 for filename in filenames]
        return zip(names, paths)
    except FileNotFoundError:
        # FIXME: Subfolder missing. Log warning here?
        return []

def libpath(path):
    return pathjoin(libroot, path)

def libwalk(path):
    for dirname, dirs, files in os.walk(libpath(path)):
        for filename in (dirs + files):
            yield pathjoin(dirname, filename)

def load_builtins(path, extension, loader):
    path = libpath(path)
    builtins = {}
    for filename in os.listdir(path):
        base, ext = os.path.splitext(filename)
        if ext == extension:
            builtins[base] = loader(pathjoin(path, filename))
    return builtins

def int_format_str(displaymode, bitsize):
    hexfmt = "0x{{:0{}X}}"
    ifmt = {
            "pointer": hexfmt.format(divup(bitsize, 4)),
            "hex": hexfmt.format(divup(bitsize, 4))
            }
    return ifmt.get(displaymode, "{}")

def writetsv(path, data, force=False, headers=None):
    mode = "w" if force else "x"
    data = list(data)
    if headers is None:
        headers = data[0].keys()
    with open(path, mode, newline='') as f:
        # FIXME: Wonder if I can auto-generate per-struct dialects that do the
        # right thing with validate() on loading, so we find out about size or
        # type mismatches right away.
        writer = csv.DictWriter(f, headers, dialect='romtool')
        writer.writeheader()
        for item in data:
            writer.writerow(item)

def readtsv(path):
    with open(path, newline='') as f:
        return list(csv.DictReader(f, dialect='romtool'))

def filesize(f):
    """ Get the size of a file """
    pos = f.tell()
    f.seek(0, 2)
    size = f.tell()
    f.seek(pos)
    return size
