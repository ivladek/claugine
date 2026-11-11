"""makemessages that also extracts `_l("…")` (the project's alias of gettext_lazy)."""
from django.core.management.commands import makemessages


class Command(makemessages.Command):
    xgettext_options = makemessages.Command.xgettext_options + ["--keyword=_l"]
