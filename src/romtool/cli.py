import os
import sys
import logging
import logging.config
import argparse
import textwrap

import yaml

import romtool.commands
from romtool import util
from romtool.util import pkgfile
from romtool.version import version

log = logging.getLogger(__name__)

def parser_setup(parser, spec, defaults):
    """ Create parser arguments from an args.yaml spec

    This is split out to make it easy to add the global argument set to
    each subparser.

    FIXME: that is what `parents` is for. Also, this setup only "works" by
    making all arguments optional, which loses the default argparse error
    checking. Better solution: parse twice, once with everything set to
    optional (to get -v, --debug, and especially --conf), then load the conf
    file, then re-parse with the conf file contents populating defaults.

    This might actually be a good feature to submit upstream. a conffile_arg
    option to argumentparser (and a conffile_callback that is expected to read
    the file and pass back a defaults dict -- perhaps defaulting to "assume
    configparser format)

    A similar use_envvars option might be nice too.

    Project: arg library that implements /etc /home envvar cli-conffile
    cli-args cascading of options. Interface similar to argparse, maybe just
    passes arguments to argparse and/or inherits argumentparser directly. User
    provides option spec as dict, along with a mapping of file types to loader
    functions.

    What about subcommands? Should the option spec be a nested dict, one for
    each subcommand, or should the ui be one call/construction per subcommand?
    (probably one call per subcommand -- avoids complexity)

    Read confs from json or configparser by default; read yaml if available
    (and suggest it if it's not with loglevel:error).

    Should spec format match the argparse interface? That would make
    implementation easier, at the cost of duplication (same arg specced
    multiple times in different commands...but wait, yaml has anchors!)

    How to map envvars to options? (PROG_[OPTION], probably)
    """

    argtypes = yaml.safe_load("""
    args: {}
    args+:
      nargs: '+'
    opts: {}
    ropts:
      action: append
    flags:
      action: store_true
    """)

    for argtype, metaargs in argtypes.items():
        for name, desc in spec.get(argtype, {}).items():
            names = name.split("|")
            conf_key = names[-1].lstrip("-")  # Turn `--argname` into `argname`
            default = defaults.get(conf_key, None)
            if argtype == "args" and default is not None:
                # Prevent complaints about missing positional args if a
                # default was provided
                metaargs['nargs'] = '?'
            parser.add_argument(*names, **metaargs, default=default, help=desc)

def conf_load(argv):
    """ Get the --conf argument and do the needful with it

    This copies argv before parsing, so it doesn't actually consume
    args. It returns the dict from the --conf file, or an empty dict if
    none was provided.
    """
    argv = argv.copy()
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--conf")
    filename = parser.parse_known_args(argv)[0].conf

    # If conf wasn't provided, return an empty dict
    if not filename:
        return {}

    with open(filename) as f:
        options = util.loadyaml(f)
    # Command line opts get tilde expansion automatically. If we want
    # it, we have to do it ourselves.
    options = {k: os.path.expanduser(v) for k, v in options.items()}
    return options


def debug_input(conffile_dict, args_object):
    """ Print the effective arguments and their sources """
    fmt = "%s:\t%s\t(%s)"
    for k, v in vars(args_object).items():
        if v is None:
            source = "default"
        elif v == conffile_dict.get(k, None):
            source = "conf"
        else:
            source = "args"
        log.debug(fmt, k, v, source)


def main(argv=None):
    """ Entry point for romtool."""
    if argv is None:
        argv = sys.argv[1:]

    # It's irritating to keep all the help information as string literals in
    # the source, so argument details are loaded from a yaml file that's
    # easier to maintain. See args.yaml for the actual available arguments.
    # FIXME: After some thought, probably better to use one big string in the
    # source. :-(

    with open(pkgfile("args.yaml")) as argfile:
        argspecs = yaml.load(argfile, Loader=yaml.SafeLoader)

    # Get arguments from conf file, if provided. This has to be done
    # before the parsers get built because the stuff in a conf file
    # affects how they need to *be* built. This is aggravating as hell.
    defaults = conf_load(argv)

    # Set up CLI parser
    globalargs = argspecs.pop("global")
    topparser = argparse.ArgumentParser(**globalargs.get('spec', {}))
    parser_setup(topparser, globalargs, defaults)

    subparsers = topparser.add_subparsers(title="commands")
    for cmd, argspec in sorted(argspecs.items()):
        sp = subparsers.add_parser(cmd, conflict_handler='resolve',
                                   **argspec.get("spec", {}))
        parser_setup(sp, argspec, defaults)
        parser_setup(sp, globalargs, defaults)
        sp.set_defaults(func=getattr(romtool.commands, cmd))

    # Parse arguments
    args = topparser.parse_args(argv)
    if args.version:
        print(version)
        sys.exit(0)

    # Set up logging
    logconf_key = ('debug' if args.debug
                    else 'verbose' if args.verbose
                    else 'default')
    with open(pkgfile('logging.yaml')) as f:
        logconf = util.loadyaml(f.read())
        logging.config.dictConfig(logconf[logconf_key])

    # Debug args/conf file data
    log.debug("Options loaded from conf, if any:")
    util.debug_structure(defaults)
    log.debug("Final input args:")
    util.debug_structure(vars(args))

    # If no subcommand supplied, print help.
    if not hasattr(args, 'func'):
        topparser.print_help()
        sys.exit(1)

    # Probable crash behavior: Normally, log exception message as CRITICAL. If
    # --debug is enabled, also print the full trace. If --pdb is enabled, print
    # the trace and also break into the debugger.
    try:
        args.func(args)
    except FileNotFoundError as e:
        log.critical(str(e))
        sys.exit(2)
    except Exception as e:
        # log.critical("Unhandled exception: '{}'".format(str(e)))
        log.exception(e)
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
