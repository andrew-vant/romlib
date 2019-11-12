import argparse
import sys
import hashlib
import logging
import os
import shutil
import textwrap
import itertools
import csv
from pprint import pprint
from itertools import chain
from importlib.machinery import SourceFileLoader

import yaml

import romlib
import romlib.charset
from romtool import util
from romtool.util import pkgfile

log = logging.getLogger(__name__)

try:
    # Try to do the right thing when piping to head, etc.
    from signal import signal, SIGPIPE, SIG_DFL
    signal(SIGPIPE, SIG_DFL)
except ImportError:
    # SIGPIPE isn't available on Windows, at least not on my machine. For now
    # just ignore it, but I should probably test piping on windows at some
    # point.
    pass

class RomDetectionError(Exception):
    """ Indicates that we couldn't autodetect the map to use for a ROM."""
    def __init__(self, _hash=None, filename=None):
        self.hash = _hash
        self.filename = filename
    def __str__(self):
        return "ROM sha1 hash not in db: {}".format(self.hash)
    def log(self):
        log.error("Couldn't autodetect ROM map for %s", self.filename)
        log.error("%s", self)
        log.error("The rom may be unsupported, or your copy may "
                      "be modified, or this may be a save file")
        log.error("You will probably have to explicitly supply --map")

# FIXME: Add subcommand to list available maps on the default search paths.
# Probably its output should advice the user that it's only what shipped with
# romtool and they're free to supply their own.

def detect(romfile, maproot=None):
    """ Detect the map to use with a given ROM.

    maproot -- A root directory containing a set of rom maps and a hashdb.txt
               file associating sha1 hashes with map names.
    """
    if maproot is None:
        maproot = pkgfile("maps")

    dbfile = os.path.join(maproot, 'hashdb.txt')
    with open(dbfile) as hashdb, open(romfile, "rb") as rom:
        # FIXME: Reads whole file into memory, likely to fail on giant images,
        # e.g cds/dvds.
        log.info("Detecting ROM map for: %s.", romfile)
        romhash = hashlib.sha1(rom.read()).hexdigest()
        log.info("sha1 hash is: %s.", romhash)
        try:
            line = next(line for line in hashdb if line.startswith(romhash))
        except StopIteration:
            raise RomDetectionError(romhash, romfile)
        name = line.split(maxsplit=1)[1].strip()
        log.info("ROM map found: %s", name)
        return os.path.join(maproot, name)


def dump(args):
    """ Dump all known data from a ROM."""
    if args.map is None:
        try:
            args.map = detect(args.rom)
        except RomDetectionError as e:
            e.log()
            sys.exit(2)

    if args.include is not None:
        raise NotImplementedError("Autopatched dumps not implemented")
        # FIXME: Patch rom in-memory with an ips so you can dump a mod without
        # applying it.

    rmap = romlib.RomMap(args.map)

    # This gets awkward since we want to open ROM always but open SAVE
    # only sometimes. I suspect this means the design needs some work.
    # Can't they be loaded separately? (maybe not, saves may have
    # pointers to stuff in the rom that need dereferencing?)
    log.info("Opening ROM file: %s", args.rom)
    with open(args.rom, "rb") as rom:
        if args.save:
            log.info("Opening SAVE file: %s", args.save)
            with open(args.save, "rb") as save:
                data = rmap.read(rom, save)
        else:
            log.debug("No save file specified, skipping")
            data = rmap.read(rom, None)

    log.info("Dumping ROM data to: %s", args.moddir)
    output = rmap.dump(data)
    os.makedirs(args.moddir, exist_ok=True)
    for entity, dicts in output.items():
        filename = "{}/{}.tsv".format(args.moddir, entity)
        log.info("Writing output file: %s", filename)
        try:
            romlib.util.writetsv(filename, dicts, args.force)
        except FileExistsError as err:
            log.error(err)
            dest = os.path.abspath(args.moddir)
            log.error("Aborting, dump would overwrite files in " + dest)
            advice = "(you can use --force if you really mean it)"
            log.error(advice)
            sys.exit(2)

    log.info("Dump finished")


def build(args):
    """ Build patches from a data set containing changes.

    Intended to be applied to a directory created by the dump subcommand.
    """
    # FIXME: Really ought to support --include for auto-merging other patches.
    # Have it do the equivalent of build and then merge.

    if args.map is None and args.rom is None:
        log.error("At least one of -s or -m must be provided.")
        sys.exit(1)
    if args.map is None:
        try:
            args.map = detect(args.rom)
        except RomDetectionError as e:
            e.log()
            sys.exit(2)

    rmap = romlib.RomMap(args.map)
    msg = "Loading mod dir %s using map %s."
    log.info(msg, args.moddir, args.map)
    data = rmap.load(args.moddir)
    source = "save" if args.save else "rom"
    patch = romlib.Patch(rmap.bytemap(data, source))
    _filterpatch(patch, args.rom)
    _writepatch(patch, args.patch)


def merge(args):
    """ Merge multiple patches.

    This can accept and merge changes from any number of existing patches and
    formats. Overlapping changes will produce a warning. Last changeset
    specified on the command line wins.
    """
    changeset = romlib.util.CheckedDict()
    for patchfile in args.patches:
        msg = "Importing changes from %s."
        log.info(msg, patchfile)
        changeset.update(romlib.Patch.load(patchfile).changes)

    # Filter the complete changeset against a target ROM if asked.
    patch = romlib.Patch(changeset)
    _filterpatch(patch, args.rom)
    _writepatch(patch, args.out)


def convert(args):
    """ Convert one patch format to another.

    This is just syntactic sugar over merge.
    """
    args.patches = [args.infile]
    args.out = args.outfile
    args.rom = None
    merge(args)


def diff(args):
    """ Build a patch by diffing two roms.

    If someone has been making their changes in-place, they can use this to get
    a patch.
    """
    with open(args.original, "rb") as original:
        with open(args.modified, "rb") as changed:
            patch = romlib.Patch.from_diff(original, changed)
    _writepatch(patch, args.out)

def apply(args):
    """ Apply a patch to a file, such as a rom or a save.

    By default this makes a backup of the existing file at filename.bak.
    """
    # Move the target to a backup name first to preserve its metadata, then
    # copy it back to its original name, then patch it there.
    patch = romlib.Patch.load(args.patch)
    tgt = args.target
    _backup(args.target, args.nobackup)
    log.info("Applying patch")
    with open(tgt, "r+b") as f:
        patch.apply(f)
    log.warning("Patch applied. Note: You may want to run `romtool "
                    "sanitize` next, especially if this is a save file.")


def sanitize(args):
    """ Sanitize a ROM or save file

    This uses map-specific hooks to correct any checksum errors or similar
    file-level issues in a rom or savegame.
    """
    if args.map is None:
        try:
            args.map = detect(args.target)
        except RomDetectionError as e:
            e.log()
            sys.exit(2)
    rmap = romlib.RomMap(args.map)

    # Maps must supply sanitize_save and sanitize_rom hooks. If they're not
    # found, assume nothing needs to be done. FIXME: separate sanatization into
    # mandatory parts (e.g checksums) and linting (e.g. hp > max hp, oops).
    # Call the latter lint_save, lint_rom, etc.

    try:
        path = args.map + "/hooks.py"
        log.info("Loading map hooks from %s", path)
        hooks = SourceFileLoader("hooks", path).load_module()
    except FileNotFoundError:
        log.info("%s not present", path)
        log.info("Nothing needs to be done")
        sys.exit(0)
    else:
        log.debug("Done loading hooks.")

    if not args.type:
        args.type = "rom"
    funcname = "sanitize_" + args.type
    log.info("Looking for %s hook", funcname)
    try:
        sanitize = getattr(hooks, funcname)
    except AttributeError:
        log.info("No hook for %s", funcname)
        log.info("Nothing needs to be done")
        sys.exit(0)
    else:
        log.debug("Found hook %s", funcname)

    # Well that was ugly. Here's the actual work:

    _backup(args.target, args.nobackup)
    with open(args.target, "r+b") as f:
        log.info("Sanitizing '%s'", args.target)
        sanitize(f)


def charmap(args):
    # FIXME: Much of this should probably be moved into the text module or
    # something.
    log.info("Loading strings")
    with open(args.strings) as f:
        strings = [s.strip() for s in f]

    log.info("Loading rom")
    with open(args.rom, "rb") as rom:
        data = rom.read()
        view = memoryview(data)
    log.debug("rom length: %s bytes", len(data))

    log.info("Starting search")
    maps = {s: [] for s in strings}
    for s in strings:
        log.debug("Searching for %s", s)
        pattern = romlib.charset.Pattern(s)
        for i in range(len(data) - len(s) + 1):
            chunk = view[i:i+len(s)]
            try:
                cmap = pattern.buildmap(chunk)
            except romlib.charset.NoMapping:
                continue
            log.debug("Found match for %s at %s", s, i)
            if cmap in maps[s]:
                log.debug("Duplicate mapping, skipping")
            else:
                log.info("New mapping found for '%s' at %s", s, i)
                maps[s].append(cmap)

        found = len(maps[s])
        msg = "Found %s possible mappings for '%s'"
        log.info(msg, found, s)

    charsets = []
    for m in itertools.product(*maps.values()):
        try:
            merged = romlib.charset.merge(*m)
        except romlib.charset.MappingConflictError:
            log.debug("Mapping conflict")
            pass
        else:
            log.info("Found consistent character map.")
            charsets.append(merged)

    if len(charsets) == 0:
        log.error("Could not find any consistent character set")
    else:
        log.info("Found %s consistent character sets", len(charsets))

    for i, cs in enumerate(charsets):
        print("### {} ###".format(i))
        out = sorted((byte, char) for char, byte in cs.items())
        for byte, char in out:
            print("{:02X}={}".format(byte, char))

def blocks(args):
    # Most users likely use Windows, so I can't rely on them having sort, head,
    # etc or equivalents available, nor that they'll know how to use them.
    # Hence some extra args that shouldn't be necessary but are.
    args.byte = romlib.util.intify(args.byte, None)
    args.num = romlib.util.intify(args.num, None)
    args.min = romlib.util.intify(args.min, 16)

    log.info("Loading rom")
    with open(args.rom, "rb") as rom:
        data = rom.read()

    log.debug("rom length: %s bytes", len(data))
    log.info("Starting search")

    blocks = []
    blocklen = 1
    last = None
    for i, byte in enumerate(data):
        if last is not None and byte != last:
            # End of block. Add to the list if it's long enough to care. If the
            # user specified a byte to search for and this isn't it, skip it.
            if blocklen > args.min:
                if args.byte is None or last == args.byte:
                    log.debug("Noting block at %s", i-blocklen)
                    blocks.append((blocklen, i-blocklen, last))
            blocklen = 1
            last = None
        else:
            blocklen += 1
            last = byte

    blocks.sort(reverse=True)
    print("offset\tblkbyte\tlength\thexlen")
    for length, offset, byte in blocks[0:args.num]:
        fmt = "{:06X}\t0x{:02X}\t{}\t{:X}"
        print(fmt.format(offset, byte, length, length))

def meta(args):
    """ Print rom metadata, e.g. console and header info"""

    writer = None
    for filename in args.rom:
        log.info("Inspecting ROM: %s", filename)
        with open(filename, 'rb') as romfile:
            try:
                rom = romlib.rom.Rom.make(romfile)
            except romlib.rom.RomFormatError as e:
                log.error("Error inspecting %s: %s", filename, str(e))
                continue
        header_data = {"File": filename}
        header_data.update(rom.header.dump())
        columns = ['File'] + romlib.struct.output_fields(rom.header)
        if not writer:
            writer = csv.DictWriter(sys.stdout, columns, dialect='romtool')
            writer.writeheader()
        writer.writerow(header_data)


def identify(args):
    for filename in args.rom:
        with open(filename, 'rb') as romfile:
            print(romlib.rom.identify(romfile) + "\t" + filename)


def _backup(filename, skip=False):
    """ Make a backup, or warn if no backup."""
    bak = filename + ".bak"
    if not skip:
        log.info("Backing up '%s' as '%s'", filename, bak)
        os.replace(filename, bak)
        shutil.copyfile(bak, filename)
    else:
        log.warning("Backup suppressed")


def _filterpatch(patch, romfile):
    # Fixme: Ask forgiveness, not permission here? And should the check be
    # handled by the caller?
    if romfile is not None:
        msg = "Filtering changes against %s."
        log.info(msg, romfile)
        with open(romfile, "rb") as rom:
                patch.filter(rom)


def _writepatch(patch, outfile):
    """ Write a patch to a file.

    Logs a bit if needed and redirects to stdout if needed.
    """
    if outfile:
        log.info("Creating patch at %s.", outfile)
        patch.save(outfile)
    else:
        patch.to_ipst(sys.stdout)
    log.info("There were %s changes.", len(patch.changes))
