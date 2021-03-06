#!/usr/bin/env python3

import os
import os.path
from os.path import dirname, join
from setuptools import setup, find_packages
from pprint import pprint


def read(relative_path):
    with open(join(dirname(__file__), relative_path)) as f:
        return f.read()

dependencies = ["bitstring>=3.1.3",
                "patricia-trie>=10",
                "pyyaml>=3.10"]

scm_version_options = {
        'write_to': 'src/romtool/version.py',
        'fallback_version': 'UNKNOWN',
        }

classifiers = ["Development Status :: 3 - Alpha",
               "Intended Audience :: Developers",
               "Natural Language :: English",
               "Operating System :: OS Independent",
               "Programming Language :: Python :: 3",
               "Topic :: Software Development :: Libraries :: Python Modules"]

setup(name="romtool",
      packages=find_packages("src") + ['romtool.maps'],
      package_dir={'': "src"},
      include_package_data=True,
      install_requires=dependencies,
      setup_requires=['setuptools-scm>=3.3.0'],
      use_scm_version=scm_version_options,
      tests_require=['tox'],
      author="Andrew Vant",
      author_email="ajvant@gmail.com",
      description="Library and tool for manipulating video game ROMs.",
      long_description=read("README.rst"),
      classifiers=classifiers,
      zip_safe=False,
      keywords="rom roms snes",
      url="https://github.com/andrew-vant/romlib",
      test_suite="tests",
      entry_points={"console_scripts": ["romtool = romtool.cli:main"]},
      )
