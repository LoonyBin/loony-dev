"""Enable ``python -m loony_dev`` to invoke the CLI.

The persisted Claude Code hook command (see
:func:`loony_dev.agents.session_hooks.hook_command`) is built as
``{sys.executable} -m loony_dev hook <event>`` so the hook resolves via the
current interpreter rather than depending on ``loony-dev`` being on ``PATH``
(which it is not when loony-dev is installed in a venv). This module is what
makes that invocation form work.
"""
from __future__ import annotations

from loony_dev.cli import cli

if __name__ == "__main__":
    cli()
