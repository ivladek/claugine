"""Core services of a site from the command line (the same as Infrastructure → site → Core services).

    manage.py core_services read <site>   read the site's platforms (their own VMs) and every host and VM of its
                                          services over ssh — read only; recorded as a Task
    manage.py core_services seed <site>   add the services of the standard layout the site has (after a read)
    manage.py core_services list <site>
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Core services of a site: read, seed (standard layout), list."

    def add_arguments(self, parser):
        parser.add_argument("what", choices=["read", "seed", "list"])
        parser.add_argument("site")

    def handle(self, *args, **opts):
        from accounts import ha, tasks
        from inventory import core_services as cs
        from inventory.models import CoreService, Site
        st = Site.objects.filter(code=opts["site"]).first()
        if st is None:
            raise CommandError(f"no site {opts['site']}")
        if opts["what"] == "read":
            if not ha.is_active():
                self.stdout.write(f"this portal node is {ha.role()}, not active: nothing to do")
                return
            settings.DEPLOY_ASYNC = False  # run here and wait
            t = tasks.start("core services read", f"Read core services of {st.code}: all (command line)",
                            lambda task: cs.read(task, st.code), target=st)
            for s in t.steps:
                self.stdout.write(f"{s['status']:7} {s['name']}: {s['detail']}")
            self.stdout.write(f"{t.state}: {t.result or t.error}")
        elif opts["what"] == "seed":
            made = cs.seed(st.code)
            self.stdout.write("made: " + (", ".join(made) or "nothing"))
        else:
            for s in CoreService.objects.filter(site=st).prefetch_related("nodes"):
                self.stdout.write(f"{s.name:20} {s.kind:4} {s.state:8} " + " ".join(f"{n.address}:{n.ssh_port}" for n in s.nodes.all()))
