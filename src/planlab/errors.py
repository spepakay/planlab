"""The one exception family, and the exit code each member maps to.

Anything a caller did wrong, or anything the tree says no to, is raised as a
PlanlabError and rendered by the CLI as a single line on stderr. A traceback
means a bug in planlab, never a refusal.
"""


class PlanlabError(Exception):
    """Anything the operator should see as one line, not a traceback. Exit 1."""

    code = 1


class FrontmatterError(PlanlabError):
    """A plan's frontmatter does not parse as a YAML mapping."""


class LostRace(PlanlabError):
    """Another process owns this transition. Exit 3: a skip, not a failure."""

    code = 3
