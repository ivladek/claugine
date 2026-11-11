"""The options of deploy/install-lb.sh for this portal VM, one per line, from Settings → Portal and Platform and node.env
(accounts/haproxy.py). Used by deploy/server/claugine-lb apply | certificate.

    manage.py haproxy_args                   HAProxy
    manage.py haproxy_args --letsencrypt     and the Let's Encrypt account (claugine-lb certificate)
"""
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "The options of deploy/install-lb.sh for this portal VM (Settings → Portal and Platform + node.env)."

    def add_arguments(self, parser):
        parser.add_argument("--letsencrypt", action="store_true")

    def handle(self, *args, **opts):
        from accounts import haproxy, thisnode
        try:
            out = haproxy.args_for(haproxy.get(), thisnode.read(), letsencrypt=opts["letsencrypt"])
        except haproxy.Incomplete as exc:
            raise CommandError(str(exc))
        self.stdout.write("\n".join(out))
