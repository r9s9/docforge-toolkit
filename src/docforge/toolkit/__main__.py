"""`python3 -m docforge.toolkit`: the same as the `docforge-live` command.

The skills use this form because it works even when pip's script folder isn't
on PATH.
"""

from .cli import main

raise SystemExit(main())
