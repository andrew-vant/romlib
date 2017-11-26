#!/usr/bin/python3

""" CLI frontend to romlib.

Performs as many functions useful to romhackers as I can come up with.
"""


import argparse
import sys
import hashlib
import logging
import os
import shutil
import textwrap
import itertools
from pprint import pprint
from itertools import chain
from collections import OrderedDict

import yaml

import romlib
import romlib.charset

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
        logging.error("Couldn't autodetect ROM map for %s", self.filename)
        logging.error("%s", self)
        logging.error("The rom may be unsupported, or your copy may "
                      "be modified, or this may be a save file")
        logging.error("You will probably have to explicitly supply --map")

# FIXME: Add subcommand to list available maps on the default search paths.
# Probably its output should advice the user that it's only what shipped with
# romtool and they're free to supply their own.

def detect(romfile, maproot=None):
    """ Detect the map to use with a given ROM.

    maproot -- A root directory containing a set of rom maps and a hashdb.txt
               file associating sha1 hashes with map names.
    """
    if maproot is None:
        maproot = _get_path("maps")

    dbfile = os.path.join(maproot, 'hashdb.txt')
    with open(dbfile) as hashdb, open(romfile, "rb") as rom:
        # FIXME: Reads whole file into memory, likely to fail on giant images,
        # e.g cds/dvds.
        logging.info("Detecting ROM map for: %s.", romfile)
        romhash = hashlib.sha1(rom.read()).hexdigest()
        logging.info("sha1 hash is: %s.", romhash)
        try:
            line = next(line for line in hashdb if line.startswith(romhash))
        except StopIteration:
            raise RomDetectionError(romhash, romfile)
        name = line.split(maxsplit=1)[1].strip()
        logging.info("ROM map found: %s", name)
        return os.path.join(maproot, name)


def dump(args):
    """ Dump all known data from a ROM."""
    if args.map is None:
        try:
            args.map = detect(args.rom)
        except RomDetectionError as e:
            e.log()
            sys.exit(2)

    rmap = romlib.RomMap(args.map)
    try:
        logging.info("Opening ROM file: %s", args.rom)
        rom = open(args.rom, "rb")
        if args.save:
            logging.info("Opening SAVE file: %s", args.save)
            save = open(args.save, "rb")
        else:
            logging.debug("No save file specified, skipping")
            save = None

        if args.patch is not None:
            raise NotImplementedError("Autopatched dumps not implemented")
            # FIXME: Patch rom in-memory with an ips so you can dump a mod without
            # applying it.

        data = rmap.read(rom, save)
    finally:
        rom.close()
        if save:
            save.close()

    logging.info("Dumping ROM data to: %s", args.datafolder)
    output = rmap.dump(data)
    os.makedirs(args.datafolder, exist_ok=True)
    for entity, dicts in output.items():
        filename = "{}/{}.tsv".format(args.datafolder, entity)
        logging.info("Writing output file: %s", filename)
        romlib.util.writetsv(filename, dicts, args.force)
    logging.info("Dump finished")


def build(args):
    """ Build patches from a data set containing changes.

    Intended to be applied to a directory created by the dump subcommand.
    """
    # FIXME: Really ought to support --include for auto-merging other patches.
    # Have it do the equivalent of build and then merge.

    if args.map is None and args.source is None:
        logging.error("At least one of -s or -m must be provided.")
        sys.exit(1)
    if args.map is None:
        try:
            args.map = detect(args.source)
        except RomDetectionError as e:
            e.log()
            sys.exit(2)

    rmap = romlib.RomMap(args.map)
    msg = "Loading mod dir %s using map %s."
    logging.info(msg, args.moddir, args.map)
    data = rmap.load(args.moddir)
    source = "save" if args.save else "rom"
    patch = romlib.Patch(rmap.bytemap(data, source))
    _filterpatch(patch, args.source)
    _writepatch(patch, args.out)


def merge(args):
    """ Merge multiple patches.

    This can accept and merge changes from any number of existing patches and
    formats. Overlapping changes will produce a warning. Last changeset
    specified on the command line wins.
    """
    changeset = romlib.util.CheckedDict()
    for patchfile in args.patches:
        msg = "Importing changes from %s."
        logging.info(msg, patchfile)
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
    logging.info("Applying patch")
    with open(tgt, "r+b") as f:
        patch.apply(f)
    logging.warning("Patch applied. Note: You may want to run `romtool "
                    "sanitize` next, especially if this is a save file.")


def sanitize(args):
    """ Sanitize a ROM or save file

    This uses map-specific hooks to correct any checksum errors or similar
    file-level issues in a rom or savegame.
    """
    if args.map is None:
        try:
            args.map = detect(args.rom)
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
        logging.info("Loading map hooks from %s", path)
        hooks = SourceFileLoader("hooks", path)
    except FileNotFoundError:
        logging.info("%s not present", path)
        logging.info("Nothing needs to be done")
        sys.exit(0)
    else:
        logging.debug("Done loading hooks.")

    if not args.type:
        args.type = "rom"
    funcname = "sanitize_" + args.type
    try:
        sanitize = getattr(hooks, funcname)
    except AttributeError:
        logging.info("No hook for %s", funcname)
        logging.info("Nothing needs to be done")
        sys.exit(0)
    else:
        logging.debug("Found hook %s", funcname)

    # Well that was ugly. Here's the actual work:

    _backup(args.target, args.nobackup)
    with open(args.target, "r+b") as f:
        logging.info("Sanitizing %s")
        sanitize(f)


def charmap(args):
    # FIXME: Much of this should probably be moved into the text module or
    # something.
    logging.info("Loading strings")
    with open(args.strings) as f:
        strings = [s.strip() for s in f]

    logging.info("Loading rom")
    with open(args.rom, "rb") as rom:
        data = rom.read()
        view = memoryview(data)
    logging.debug("rom length: %s bytes", len(data))

    logging.info("Starting search")
    maps = {s: [] for s in strings}
    for s in strings:
        logging.debug("Searching for %s", s)
        pattern = romlib.charset.Pattern(s)
        for i in range(len(data) - len(s) + 1):
            chunk = view[i:i+len(s)]
            try:
                cmap = pattern.buildmap(chunk)
            except romlib.charset.NoMapping:
                continue
            logging.debug("Found match for %s at %s", s, i)
            if cmap in maps[s]:
                logging.debug("Duplicate mapping, skipping")
            else:
                logging.info("New mapping found for '%s' at %s", s, i)
                maps[s].append(cmap)

        found = len(maps[s])
        msg = "Found %s possible mappings for '%s'"
        logging.info(msg, found, s)

    charsets = []
    for m in itertools.product(*maps.values()):
        try:
            merged = romlib.charset.merge(*m)
        except romlib.charset.MappingConflictError:
            logging.debug("Mapping conflict")
            pass
        else:
            logging.info("Found consistent character map.")
            charsets.append(merged)

    if len(charsets) == 0:
        logging.error("Could not find any consistent character set")
    else:
        logging.info("Found %s consistent character sets", len(charsets))

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

    logging.info("Loading rom")
    with open(args.rom, "rb") as rom:
        data = rom.read()

    logging.debug("rom length: %s bytes", len(data))
    logging.info("Starting search")

    blocks = []
    blocklen = 1
    last = None
    for i, byte in enumerate(data):
        if last is not None and byte != last:
            # End of block. Add to the list if it's long enough to care. If the
            # user specified a byte to search for and this isn't it, skip it.
            if blocklen > args.min:
                if args.byte is None or last == args.byte:
                    logging.debug("Noting block at %s", i-blocklen)
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


def _backup(filename, skip=False):
    """ Make a backup, or warn if no backup."""
    bak = filename + ".bak"
    if not skip:
        logging.info("Backing up %s as %s", filename, bak)
        os.replace(filename, bak)
        shutil.copyfile(bak, filename)
    else:
        logging.warning("Backup suppressed")


def _filterpatch(patch, romfile):
    # Fixme: Ask forgiveness, not permission here? And should the check be
    # handled by the caller?
    if romfile is not None:
        msg = "Filtering changes against %s."
        logging.info(msg, romfile)
        with open(romfile, "rb") as rom:
                patch.filter(rom)


def _writepatch(patch, outfile):
    """ Write a patch to a file.

    Logs a bit if needed and redirects to stdout if needed.
    """
    if outfile:
        logging.info("Creating patch at %s.", outfile)
        patch.save(outfile)
    else:
        patch.to_ipst(sys.stdout)
    logging.info("There were %s changes.", len(patch.changes))


def _add_yaml_omap():
    """ Register the omap type with libyaml.

    I'm not actually sure this is necessary anymore. Pretty sure the
    list-of-pairs construction returns tuples that I can use to initialize an
    odict.
    """
    def omap_constructor(loader, node):
        """ libyaml constructor for ordered dictionaries."""
        return OrderedDict(loader.construct_pairs(node))
    yaml.add_constructor("!omap", omap_constructor)


def _get_path(subfile=None):
    """ Get the full path to the containing directory of this file.

    Optionally get a path to a file within same. This is a utility function to
    avoid having to do a bunch of distracting __file__ juggling.
    """
    # FIXME: should this go in util? Maybe not, nothing in romlib uses it.
    path = os.path.realpath(__file__)
    path = os.path.dirname(path)
    if subfile is not None:
        path = os.path.join(path, subfile)
    return path


def main():
    """ Entry point for romtool."""

    # It's irritating to keep all the help information as string literals in
    # the source, so argument details are loaded from a yaml file that's
    # easier to maintain. See args.yaml for the actual available arguments.
    # FIXME: After some thought, probably better to use one big string in the
    # source. :-(

    _add_yaml_omap()

    with open(_get_path("args.yaml")) as argfile:
        argdetails = yaml.load(argfile)

    def argument_setup(parser, details):
        """ Add arguments to a parser.

        This is split out to make it easy to add the global argument set to
        each subparser.
        """
        # FIXME: Should this be done with parent?
        for name, desc in details.get("args", {}).items():
            names = name.split("|")
            parser.add_argument(*names, help=desc)
        for name, desc in details.get("args+", {}).items():
            names = name.split("|")
            parser.add_argument(*names, nargs="+", help=desc)
        for name, desc in details.get("opts", {}).items():
            names = name.split("|")
            parser.add_argument(*names, help=desc)
        for name, desc in details.get("ropts", {}).items():
            names = name.split("|")
            parser.add_argument(*names, help=desc, action="append")
        for name, desc in details.get("flags", {}).items():
            names = name.split("|")
            parser.add_argument(*names, help=desc, action="store_true")

    topdetails = argdetails.pop("global")
    topparser = argparse.ArgumentParser(**topdetails.get('spec', {}))
    subparsers = topparser.add_subparsers(title="commands")
    argument_setup(topparser, topdetails)
    for command, details in sorted(argdetails.items()):
        subparser = subparsers.add_parser(command,
                                          conflict_handler='resolve',
                                          **details.get("spec", {}))
        subparser.set_defaults(func=globals()[command])
        argument_setup(subparser, details)
        argument_setup(subparser, topdetails)

    # Parse whatever came in.
    args = topparser.parse_args()

    # Set up logging.
    if args.verbose:
        logging.basicConfig(level=logging.INFO)
    if args.debug:
        logging.basicConfig(level=logging.DEBUG)

    # If no subcommand supplied, print help.
    if not hasattr(args, 'func'):
        topparser.print_help()
        sys.exit(1)

    # Probable crash behavior: Normally, log exception message as CRITICAL. If
    # --debug is enabled, also print the full trace. If --pdb is enabled, print
    # the trace and also break into the debugger.
    try:
        args.func(args)
    except Exception as e:
        # logging.critical("Unhandled exception: '{}'".format(str(e)))
        logging.exception(e)
        if args.pdb:
            import pdb, traceback
            print("\n\nCRASH -- UNHANDLED EXCEPTION")
            msg = ("Starting debugger post-mortem. If you got here by "
                   "accident (perhaps by trying to see what --pdb does), "
                   "you can get out with 'quit'.\n\n")
            print("\n{}\n\n".format("\n".join(textwrap.wrap(msg))))
            pdb.post_mortem()

if __name__ == "__main__":
    main()
