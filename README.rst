romtool/romlib
==============

A game-independent ROM-editing API.

(and a simple frontend for using it)

(status: alpha as hell. It works, three cheers for proof of concept, but
the implementation is ugly and fragile. I'm trying to come up with
something better in the restruct branch)

What is this?
-------------

Some people like to modify old games from the Age of Cartridges. Some
games have tools available for exactly that, such as `Lunar
Magic <http://fusoya.eludevisibility.org/lm/index.html>`__ for Super
Mario World. Such tools are usually for some specific game and do not
work for other games.

There are some tasks that are common to *any* game, though. For example,
the format of binary data structures may differ from one ROM to another,
but the act of digging that structure out and putting it in a form
someone can use is universal. Reading a string is not fundamentally
different from one game to another, either.

romlib (and its command line interface, romtool) is an attempt to collect
game-independent functionality into an API and a very basic patch-making tool.
The idea is to make simple hacks possible for non-programmers, and provide a
useful set of primitives for more complex game-specific hacking tools.

Here are some things you can do with it that might be useful:

- Inspect ROM data tables, e.g. monster stats, dialogue strings, or spell
  lists.
- Edit the same in a spreadsheet, then make a patch implementing the changes.
- Version-control rom hacking projects. Romtool's dump format is TSV, and it
  supports a textualized, commentable IPS-ish input format. Both are quite
  friendly to version control tools.
- Merge multiple existing patches together.
- See what an existing patch actually changes.

Romlib does need to be told where to find the data it is going to extract or
patch. For this it needs a "rom map", which at the moment is a set of .tsv
files specifying the location and format of the data structures in the ROM. My
hope is that maps for romlib can eventually serve as thorough, standardized,
machine-readable documentation of how different games' internals are
structured.

romlib and romtool are still very, very alpha. At the moment it mainly
converts data tables from a rom into tsv files, which the user
can edit in any spreadsheet program and then generate an IPS patch from
their changes. This functionality is most likely to be useful for
balance patches to RPGs and other statistics-heavy games. The program is
smart enough to assemble multiple related data tables into joint
objects, and also to attach the appropriate name strings to them, as
long as the necessary locations and relations are included in the map.

Some additional features that have not been added yet, but that I want
to support. This isn't a complete list, just the things that come to
mind as I am writing:

-  ROM expansion
-  Header manipulation
-  Empty-space search
-  Game documentation generation
   -  monster/item/equipment lists (for players)
   -  data table specs (for romhackers)
-  Alternate input and output formats
   -  More than just tsv for maps.
   -  More than just IPS for patches
-  Archaeological tools
   -  working out text encodings
   -  locating strings
   -  locating data arrays

Installation
------------

Romlib requires Python 3.4 or later. Install that first.

I haven't built pip or OS packages yet, mainly because I'm still not sure if
I want to break it into multiple packages. For now you can install it using the
provided setup.py script:

::

    python setup.py install


If you want to fiddle with the code yourself, you should install in development
mode. Consider forking the repo on github, then working from your fork:

::

    git clone git@github.com:yourghname/romlib.git
    cd romlib
    python setup.py develop

The installation process should place the ``romtool`` command somewhere in
your PATH, but I don't trust this yet and would like feedback.

Usage
-----

You can get help at any time with ``romtool --help`` or ``romtool <subcommand>
--help``. Trust the help output more than this section, because I may forget to
update it from time to time.

You can add ``--verbose`` to any of the commands below to show more information
about what they're doing.

The following command will extract all known data from the ROM named
``some_game.smc``, and save it as .tsv files in ``moddir``. Each file
contains a list of entities; for example, there may be a monsters.tsv
containing information about monsters, or a characters.tsv with character
stats.

::

    romtool dump some_game.rom moddir

Note that you do not usually need to specify which rom map to use; romtool
will attempt to autodetect it. (for the time being, detection is based on the
No-Intro rom sets) If autodetection fails, you can force romtool to use a map
in a particular folder by appending ``-m <mapdir>`` to the command.

After dumping is finished, make whatever changes you want to the files
in ``moddir``. Any spreadsheet application should do the job; I use
LibreOffice, but Excel should also work. Be aware that you may have to turn off
any auto-formatting features, especially if you plan to edit textual elements
such as names.

When saving your changes, be sure you save them in place in .tsv format, *not*
.xls, .ods, .csv, or anything else. It's probably a good idea to turn off
autocorrect or similar features, too.

There are some limits to what you can change. For example, if a
monster's attack power is stored in the ROM as a single byte, you cannot
give it >255 attack (the maximum single-byte value). If you're lucky, romtool
will complain. Not so lucky, and it will cheerfully make a patch that breaks
the game.

When you're done making changes, do this to preview the patch:

::

    romtool build moddir --rom some_game.rom

This prints out a textualized version of the IPS patch that will result from
your changes. If you made a small change but get a huge patch, something is
wrong.

If it looks okay, you can build the actual patch with:

::

    romtool build moddir --rom some_game.rom --out some_game.ips

That will create an IPS patch containing your changes. Give it the same name as
the ROM, but with a .ips extension (this will probably be the default in future
versions). The reason the name should match is that it will cause some emulators
(ZSNES and SNES9x at least, probably others) to automatically make use of it,
without physically modifying the original ROM.

Now you can fire up an emulator and point it to the ROM. Assuming the emulator
supports implicit patching and your patch is named correctly, you should see
your changes in-game.

Troubleshooting
---------------

**Q. ROM map detection failed. Why?**

Possible causes:

1. There may not be an available map for your ROM. At the moment only a few
   games are supported out of the box. If there isn't an existing map, you
   will have to create your own. I haven't written documentation for this
   process yet, but looking at the contents of the data/maps directory in the
   repo will probably be informative.
2. You may have an SNES ROM with an SMC header. The header changes the sha1
   hash of the rom, which is what romlib uses to identify it.  Remove the
   header and try again (eventually romtool will be able to do this for you).
3. The ROM may have been physically modified, perhaps by applying a
   patch to the file instead of relying on an emulator's implicit
   patching. If you are trying to dump data from a modified ROM, you can
   specify a map with the -m option.

**Q. My system doesn't know what program to use to open .tsv files.**

The tsv file type may not be associated with anything. The method for associating
filetypes differs by OS. On Windows 7 you can do it from the file properties;
look for "Opens With <something>" followed by a button marked 'Change'. Other
Windows versions should be pretty similar. On Linux you're on your own, but you
probably Googled the answer before you got here anyway.

**Q. The changes in my patch don't show up in-game.**

1. Your patch may be named incorrectly. It should usually have the same filename
   as the ROM, but with a .ips extension.
2. Your emulator may not support implicit patching. Either physically apply
   the patch with :code:`romtool apply`, or use an emulator that does support it.
   Here is a list of emulators known to support implicit patching:

   -  ZSNES
   -  snes9x
   -  FCEUX (name as romname.nes.ips instead of romname.ips)

   -  FIXME: Add more here....

**Q. My patch changes produce garbage.**

Probably your spreadsheet application's autoformat function is trying to be
smart. Turn it off.

**Q. I already have a modified ROM and want to make a patch from it.**

Do this:

::

    romtool diff original.rom modified.rom -o patch.ips

**Q. I have an IPS patch and want to see what's in it.**

Do this:

::

    romtool merge patch.ips

(yes, I know that doesn't make sense. It's taking advantage of the fact that the
merge command accepts any number of patches, even just one; and that by default
it prints the merged changes to stdout. Needs syntactic sugar.)


Map Files
---------

Notes on creating map files properly go here...

Notes: the various map spec files may have any number of extra columns not
used by romlib.  This is intentional; extensions or client applications can
implement UI hints by looking for extra columns in the spec.

(there probably needs to be a naming convention for app-specific columns vs
extension columns vs official columns...)

Maps in this repo that actually work:

- 7th Saga works fine
- FF1 works fine
- Lufia 2 dumps okay but I would be surprised if it creates patches okay.
- I think SMRPG worked last time I checked, not sure if it still does
