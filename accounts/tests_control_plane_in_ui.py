"""No template may name the control plane, because the public build rewrites it.

`deploy/publish-portal.sh` scrubs real identifiers out of the app code on its way to the public repository, and
`cp.example.com` is one of them: it becomes `cp.example.com` everywhere except the two delivery scripts a client
runs. That is right for a comment and wrong for anything on screen — a hardcoded control-plane name in a template
reaches a Standard customer as `cp.example.com`, an address that does not exist and is not theirs.

Found on 2026-11-10 on the production portal, which installs the public snapshot: Settings → Portal read "Which
releases the licence control plane (cp.example.com) may install", two lines under a Licence row that correctly
showed `https://cp.example.com` from the configured setting.

The rule: a template shows the control plane by reading it (`licence.url`, i.e. `settings.CHECKIN_URL`), never by
naming it.
"""

import re
import unittest
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"

#: Hosts the scrub rewrites and that therefore must not be spelled out in anything rendered to a user.
SCRUBBED = ("cp.example.com", "portal.example.com", "example.com", "example.com", "portal.internal")


class ControlPlaneIsReadNotNamed(unittest.TestCase):
    def test_no_template_names_a_host_the_publish_rewrites(self):
        offenders = []
        for path in sorted(TEMPLATES.rglob("*.html")):
            text = path.read_text(encoding="utf-8")
            for host in SCRUBBED:
                for m in re.finditer(re.escape(host), text):
                    line = text.count("\n", 0, m.start()) + 1
                    offenders.append(f"{path.relative_to(TEMPLATES.parent)}:{line}: {host}")
        self.assertEqual(
            offenders,
            [],
            "a template names a host that deploy/publish-portal.sh rewrites, so the public build shows an "
            "example domain to a real customer. Read it from the settings instead (licence.url for the control "
            "plane), or use an example host the scrub leaves alone, such as portal.example.com:\n  "
            + "\n  ".join(offenders),
        )

    def test_the_update_stream_help_reads_the_configured_control_plane(self):
        """The specific string the bug was found in, so a rewrite cannot quietly drop the variable again."""
        text = (TEMPLATES / "staff" / "_portal_nodes.html").read_text(encoding="utf-8")
        self.assertIn("licence control plane ({{ u }})", text)
        self.assertIn("blocktrans with u=licence.url", text)
