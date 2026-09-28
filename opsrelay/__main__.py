"""`python -m opsrelay`: same as the `opsrelay` command, for machines that block console-script launchers."""

import sys

from .cli import main

sys.exit(main())
