"""The common UI rule (owner 2026-11-16, mockup 39): actions on the top row, the inputs they need in a dialog - never
in the body of a details pane or a card."""

import re
import types

from django.template.loader import render_to_string
from django.test import SimpleTestCase


class NetworkRequestRow(SimpleTestCase):
    def row(self, kind, **ctx):
        r = types.SimpleNamespace(
            pk=7,
            kind=kind,
            action="add",
            target="",
            prefix_len=29,
            l2_name="",
            tenant_network="",
            note="",
            pool="",
            requested_by="adm@example.com",
            created_at=None,
            state="pending",
            get_kind_display=lambda: kind,
            get_action_display=lambda: "add",
            get_state_display=lambda: "waiting",
        )
        ctx = {"r": r, "tenant": types.SimpleNamespace(pk=3), "net_can_decide": True, **ctx}
        return render_to_string("tenants/_net_request_row.html", ctx)

    def test_the_range_is_chosen_in_a_dialog(self):
        h = self.row("inet", net_inet_pools=["203.0.113.0/24", "198.51.100.0/24"])
        cell = h[h.index('class="row-actions"') :]
        outside = re.sub(r"<dialog.*?</dialog>", "", cell, flags=re.S)
        self.assertNotIn("<select", outside)  # not next to Approve any more
        self.assertIn('data-open="dlg-nr-7"', h)
        self.assertIn('name="inet_pool"', h)  # in the dialog

    def test_without_a_range_to_choose_approve_stays_a_button(self):
        h = self.row("l2")
        self.assertNotIn("<dialog", h)
        self.assertIn("Approve</button>", h)
