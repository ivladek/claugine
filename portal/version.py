"""owner 2026-11-03: the human release tag shown in the top bar next to the installed commit.

Bump VERSION (and DATE) in the commit that gets tagged for a release (the git tag and VERSION match), so the
portal shows e.g. "v00.11.13 2026-11-04 f6da05c69a02" — the release, its date, and the exact installed build.
Avoid putting a bare "vNN.NN.NN (hash)" in this docstring: the top bar reads VERSION, not the comment.
"""

VERSION = "v01.00.00"
DATE = "2026-11-11"
