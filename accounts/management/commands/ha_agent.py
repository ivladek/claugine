"""The HA agent of the portal on two sites (accounts/ha_agent.py); runs as root.

    manage.py ha_agent serve            the agent service (systemd claugine-ha), on this node's address, port HA_AGENT_PORT (portal.env)
    manage.py ha_agent status           this node as the agent reports it (JSON)
    manage.py ha_agent op <operation>   run an operation here and wait: switchover, to-new, new-to-active,
                                        new-to-standby, maintenance-on, maintenance-off
    manage.py ha_agent replicate <addr> clone the database from addr and replicate from it (a new standby)
    manage.py ha_agent role <role>      set this node's role by hand (e.g. maintenance on the active node, and back)
"""
import json

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "The HA agent of the portal (root)."

    def add_arguments(self, parser):
        parser.add_argument("what", choices=["serve", "status", "op", "replicate", "role"])
        parser.add_argument("arg", nargs="?", default="")

    def handle(self, *args, **opts):
        from django.conf import settings
        from accounts import ha_agent, thisnode
        what, arg = opts["what"], opts["arg"]
        if what == "serve":
            n = thisnode.read()
            addr = n.get("ADDR") or "127.0.0.1"
            if not settings.HA_TOKEN:
                raise CommandError("HA_TOKEN is not set in portal.env")
            self.stdout.write(f"claugine-ha on {addr}:{settings.HA_AGENT_PORT}")
            ha_agent.enforce_role()
            ha_agent.serve(addr, settings.HA_AGENT_PORT)
        elif what == "status":
            self.stdout.write(json.dumps(ha_agent.status(), indent=1, default=str))
        elif what == "op":
            data = ha_agent.run_op(arg, by="command line")
            self.stdout.write(json.dumps(data, indent=1, default=str))
            if data.get("state") != "done":
                raise CommandError(data.get("error") or "failed")
        elif what == "role":
            self.stdout.write(ha_agent.set_role(arg))
        elif what == "replicate":
            if not arg:
                raise CommandError("give the address of the node to replicate from")
            self.stdout.write(ha_agent._pair_required().load_from(arg, replicate=True))
            thisnode.write({"ROLE": "standby"})
            self.stdout.write("role standby")
