"""Profiles → Templates, T34 S1 (owner 2026-11-22 16:59 / 17:08 / 17:46): the templates of the service profiles'
config files and installations - their own key, a kind, scope "service", no preview, used by service profiles only,
the "Service profiles" card last in the details and in the edit dialog."""

from django.test import TestCase
from django.urls import reverse

from accounts.tests import Base
from inventory.models import ConfigTemplate, CoreService, ServiceProfile, Site


def new(client, **kw):
    data = {
        "new-name": "BIND options",
        "new-kind": "config",
        "new-os": "ubuntu",
        "new-os_version": "26.04",
        "new-description": "/etc/bind/named.conf.options",
        "new-body": 'options { directory "/var/cache/bind"; };',
        "key": "bind-options",
    }
    data.update(kw)
    return client.post(reverse("infra_template_new"), data, follow=True)


class Model(TestCase):
    def test_scope_and_profiles_using(self):
        from inventory.models import HostService

        t = ConfigTemplate.objects.create(role="bind-options", kind="config", name="x", body="x", os="ubuntu")
        inst = ConfigTemplate.objects.create(
            role="bind-install", kind="steps", name="i", body="- name: ns\n", os="ubuntu"
        )
        self.assertEqual(t.scope, "service")
        HostService.objects.filter(key="ns").update(  # T36 B5a: the ns function's installation and configs, by id
            install_template=inst,
            configs=[{"path": "/etc/bind/named.conf.options", "template": t.pk}, "junk", {}],
        )
        p = ServiceProfile.objects.get(key="ns-primary")
        self.assertEqual(p.template_keys()[-2:], ["bind-install", "bind-options"])  # after base-os's
        self.assertEqual(p.template_keys()[0], "base-os-install")
        self.assertEqual([x.key for x in ServiceProfile.using(t)], ["ns-primary", "ns-secondary", "ns-site"])
        rs = ConfigTemplate.objects.create(role="rsyslog-bind", kind="config", name="r", body="x", os="ubuntu")
        self.assertEqual(ServiceProfile.using(rs), [])


class Pages(Base):
    def setUp(self):
        self.client.force_login(self.staff("sa@corp.kz", is_super_admin=True))

    def test_add_a_service_template(self):
        r = new(self.client)
        t = ConfigTemplate.objects.get(role="bind-options")
        self.assertEqual((t.kind, t.scope, t.os), ("config", "service", "ubuntu"))
        self.assertContains(r, "BIND options saved")

    def test_the_key_is_checked(self):
        before = ConfigTemplate.objects.count()
        for key in ("", "X", "has space", "egw", "a" * 41):
            new(self.client, key=key)
        self.assertEqual(ConfigTemplate.objects.count(), before)
        new(self.client)
        r = new(self.client, **{"new-name": "again"})
        self.assertContains(r, "A template with the key bind-options exists")
        self.assertEqual(ConfigTemplate.objects.filter(role="bind-options").count(), 1)

    def test_a_network_role_as_before(self):
        ConfigTemplate.objects.filter(role="l2-a").delete()
        self.client.post(
            reverse("infra_template_new"),
            {"role": "l2-a", "new-name": "L2 a", "new-os": "vyos", "new-body": "set x"},
        )
        t = ConfigTemplate.objects.get(role="l2-a")
        self.assertEqual((t.kind, t.scope), ("network", "tenant"))
        # a network role's kind cannot be posted as a service kind
        ConfigTemplate.objects.filter(role="l2-b").delete()
        new(self.client, role="l2-b", key="", **{"new-kind": "network", "new-name": "L2 b", "new-os": ""})
        self.assertEqual(ConfigTemplate.objects.get(role="l2-b").kind, "network")

    def test_details_card_no_preview_and_delete(self):
        st = Site.objects.create(code="at1")
        CoreService.objects.create(site=st, stype="ns", name="ns")
        t = ConfigTemplate.objects.create(
            role="bind-options", kind="config", name="BIND options", body="x", os="ubuntu"
        )
        u = ConfigTemplate.objects.create(role="bind-unused", kind="script", name="Unused", body="x", os="ubuntu")
        from inventory.models import HostService

        ns = HostService.objects.get(key="ns")  # T36 B5a: a config file of the ns function
        HostService.objects.filter(pk=ns.pk).update(configs=ns.configs + [{"path": "/etc/x", "template": t.pk}])
        html = self.client.get(reverse("infra_templates")).content.decode()
        d = html[html.index(f'data-for="tpl-{t.pk}"') :]
        d = d[: d.index("odetail", 10)] if "odetail" in d[10:] else d
        self.assertNotIn(f"dlg-pv-{t.pk}", html)  # no preview for a service template
        self.assertIn("Service profiles", d)
        self.assertIn("at1 ns", d)  # the instances of the profile's type
        self.assertIn("config file of a service", d)
        dlg = html[html.index(f'id="dlg-tpl-{t.pk}"') :]
        dlg = dlg[: dlg.index("</dialog>")]
        self.assertLess(dlg.index("Save"), dlg.index("Service profiles"))  # last in the edit dialog
        self.assertNotIn("network function or host service configuration", dlg)  # service kinds only
        u_det = html[html.index(f'data-for="tpl-{u.pk}"') :]
        self.assertNotIn("Service profiles", u_det[: u_det.index("</div>", u_det.index("Template"))])
        # used: not deleted; unused: deleted
        self.client.post(reverse("infra_template_delete", args=[t.pk]))
        self.assertTrue(ConfigTemplate.objects.filter(pk=t.pk).exists())
        self.client.post(reverse("infra_template_delete", args=[u.pk]))
        self.assertFalse(ConfigTemplate.objects.filter(pk=u.pk).exists())

    def test_edit_keeps_the_key_and_service_kinds(self):
        t = ConfigTemplate.objects.create(role="bind-install", kind="steps", name="BIND", body="x", os="ubuntu")
        self.client.post(
            reverse("infra_template_save", args=[t.pk]),
            {
                f"t{t.pk}-name": "BIND installation",
                f"t{t.pk}-kind": "network",
                f"t{t.pk}-os": "ubuntu",
                f"t{t.pk}-body": "apt-get install -y bind9",
            },
        )
        t.refresh_from_db()
        self.assertEqual((t.role, t.kind, t.name), ("bind-install", "steps", "BIND"))  # network refused
        self.client.post(
            reverse("infra_template_save", args=[t.pk]),
            {
                f"t{t.pk}-name": "BIND installation",
                f"t{t.pk}-kind": "script",
                f"t{t.pk}-os": "ubuntu",
                f"t{t.pk}-body": "apt-get install -y bind9",
            },
        )
        t.refresh_from_db()
        self.assertEqual((t.role, t.kind, t.name), ("bind-install", "script", "BIND installation"))


class Fixes(Base):
    """commit-376..378 (review of db4e1b0)."""

    def setUp(self):
        self.client.force_login(self.staff("sa@corp.kz", is_super_admin=True))
        self.st = Site.objects.create(code="at1")

    def test_376_no_preview_behind_the_reveal(self):
        t = ConfigTemplate.objects.create(role="bind-options", kind="config", name="B", body="x", os="ubuntu")
        img = ConfigTemplate.objects.filter(role__in=ConfigTemplate.IMAGE_ROLES).first()
        img = img or ConfigTemplate.objects.create(role="vyos-image-prepare", name="prep", body="x")
        for x in (t, img):
            r = self.client.post(reverse("infra_template_reveal", args=[x.pk]), {"site": "at1", "index": "0"})
            self.assertEqual(r.status_code, 200)
            self.assertContains(r, "This template has no preview.")

    def test_377_site_services_take_network_templates_only(self):
        from inventory.core_views import ServiceForm

        svc_t = ConfigTemplate.objects.create(role="bind-options", kind="config", name="B", body="x", os="ubuntu")
        net = ConfigTemplate.objects.filter(kind="network").exclude(role__in=ConfigTemplate.IMAGE_ROLES).first()
        pks = [v for v, _l in ServiceForm(site=self.st).fields["template"].choices]
        self.assertIn(str(net.pk), pks)
        self.assertNotIn(str(svc_t.pk), pks)
        # a template a site service is made from is not deleted (deleting would clear the service's template)
        own = ConfigTemplate.objects.create(role="bind-install", kind="steps", name="I", body="x", os="ubuntu")
        CoreService.objects.create(site=self.st, stype="ns", name="ns", template=own)
        self.client.post(reverse("infra_template_delete", args=[own.pk]))
        self.assertTrue(ConfigTemplate.objects.filter(pk=own.pk).exists())
        html = self.client.get(reverse("infra_templates")).content.decode()
        self.assertIn("site service at1 ns", html)

    def test_378_the_target_follows_the_kind(self):
        new(self.client, **{"new-os": "vyos"})
        self.assertFalse(ConfigTemplate.objects.filter(role="bind-options").exists())
        new(self.client, **{"new-os": ""})
        self.assertEqual(ConfigTemplate.objects.get(role="bind-options").os, "ubuntu")  # the default by kind
        ConfigTemplate.objects.filter(role="l2-a").delete()
        base = {"role": "l2-a", "new-name": "L2 a", "new-body": "set x"}
        self.client.post(reverse("infra_template_new"), dict(base, **{"new-os": "ubuntu"}))
        self.assertFalse(ConfigTemplate.objects.filter(role="l2-a").exists())
        self.client.post(reverse("infra_template_new"), dict(base, **{"new-os": ""}))
        self.assertEqual(ConfigTemplate.objects.get(role="l2-a").os, "vyos")
        t = ConfigTemplate.objects.get(role="bind-options")
        post = {f"t{t.pk}-name": "B2", f"t{t.pk}-kind": "config", f"t{t.pk}-os": "vyos", f"t{t.pk}-body": "x"}
        self.client.post(reverse("infra_template_save", args=[t.pk]), post)
        t.refresh_from_db()
        self.assertEqual((t.name, t.os), ("BIND options", "ubuntu"))  # refused
        html = self.client.get(reverse("infra_templates")).content.decode()
        dlg = html[html.index(f'id="dlg-tpl-{t.pk}"') :]
        dlg = dlg[: dlg.index("</dialog>")]
        self.assertNotIn('value="vyos"', dlg)  # the edit form offers Ubuntu only

    def test_379_an_unrelated_edit_keeps_the_services_template(self):
        from inventory import core_views

        old = ConfigTemplate.objects.create(role="bind-options", kind="config", name="Old", body="x", os="ubuntu")
        other = ConfigTemplate.objects.create(role="bind-install", kind="steps", name="Other", body="x", os="ubuntu")
        net = ConfigTemplate.objects.filter(kind="network").exclude(role__in=ConfigTemplate.IMAGE_ROLES).first()
        from inventory.models import CoreServiceNode

        svc = CoreService.objects.create(site=self.st, stype="core-vm", kind="host", name="misc", template=old)
        CoreServiceNode.objects.create(service=svc, name="m1", hostname="m1", address="10.0.0.9", ips=["10.0.0.9"])
        html = self.client.get(reverse("infra_site", args=["at1"]) + "?tab=core").content.decode()
        self.assertIn("Old - not a network template", html)  # shown, selected as stored

        def post(**kw):
            data = {k: ("" if v is None else v) for k, v in core_views.initial(svc).items()}
            data.update(kw)
            self.client.post(reverse("core_save", args=["at1", svc.pk]), data)
            svc.refresh_from_db()

        post(description="edited")
        self.assertEqual((svc.description, svc.template_id), ("edited", old.pk))  # kept
        post(description="again", template=str(other.pk))
        self.assertEqual((svc.description, svc.template_id), ("edited", old.pk))  # another service template: refused
        post(template=str(net.pk))
        self.assertEqual(svc.template_id, net.pk)  # a network template replaces it
        post(template=str(old.pk))
        self.assertEqual(svc.template_id, net.pk)  # and the service template cannot come back


class Commit383(Base):
    """commit-383: a service template's body is checked as the deployment uses it."""

    def setUp(self):
        self.client.force_login(self.staff("sa@corp.kz", is_super_admin=True))

    def save(self, t, **kw):
        data = {f"t{t.pk}-{k}": getattr(t, k) for k in ("name", "os", "os_version", "description", "body")}
        data[f"t{t.pk}-kind"] = t.kind
        data.update({f"t{t.pk}-{k}": v for k, v in kw.items()})
        return self.client.post(reverse("infra_template_save", args=[t.pk]), data, follow=True)

    def test_the_seeded_ns_installation_saves_unchanged(self):
        t = ConfigTemplate.objects.get(role="ns-install")
        self.assertIn("|sh", t.body)
        before = t.updated_at
        self.save(t)
        self.assertGreater(ConfigTemplate.objects.get(pk=t.pk).updated_at, before)

    def test_bad_steps_and_bodies_are_refused(self):
        t = ConfigTemplate.objects.get(role="ns-install")
        for body, msg in (
            ("- name: a\n  colours: [red]\n", "unknown colours"),
            ("not: [a list", "not valid YAML"),
            ("- name: a\n  commands: ['{{ x|nofilter }}']\n", "nofilter"),
        ):
            r = self.save(t, body=body)
            self.assertContains(r, msg)
            self.assertNotEqual(ConfigTemplate.objects.get(pk=t.pk).body, body)
        c = ConfigTemplate.objects.get(role="bind-named-conf")
        r = self.save(c, body="{% include 'x.html' %}")
        self.assertContains(r, "is not allowed in a recipe")
        self.assertEqual(self.save(c, body="x {{ node.hostname|sh }}\n").status_code, 200)
        self.assertEqual(ConfigTemplate.objects.get(pk=c.pk).body.strip(), "x {{ node.hostname|sh }}")
