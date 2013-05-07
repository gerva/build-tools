#! /usr/bin/env python

from setuptools import setup, find_packages
from setuptools.command.test import test as TestCommand

import sys

class Tox(TestCommand):
    def finalize_options(self):
        TestCommand.finalize_options(self)
        self.test_args = []
        self.test_suite = True
    def run_tests(self):
        #import here, cause outside the eggs aren't loaded
        import tox
        errno = tox.cmdline(self.test_args)
        sys.exit(errno)


setup(
    name="buildtools",
    version="1.0",
    description="Mozilla RelEng Toolkit",
    author="Release Engineers",
    author_email="release@mozilla.com",

    # python packages are under lib/python.  Note that there are several
    # top-level packages here -- not just a buildtools package

    packages=find_packages("lib/python"),
    package_dir={'': "lib/python"},

    test_suite='mozilla_buildtools.test',

    install_requires=[
        'sqlalchemy',
        'argparse',
        'twisted',
        'simplejson',
    ],

    entry_points={
        'console_scripts': [
            'slavealloc = slavealloc.scripts.main:main'
        ],
    },

    # include files listed in MANIFEST.in
    include_package_data=True,

    #tox
    tests_require=['tox'],
    cmdclass = {'test': Tox},
)
