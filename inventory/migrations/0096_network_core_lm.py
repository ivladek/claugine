"""Owner 2026-11-19 07:39 "a yes, type core-lm": the network type of the VMs' live migration between hosts."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0095_host_deployment'),
    ]

    operations = [
        migrations.AlterField(
            model_name='network',
            name='kind',
            field=models.CharField(choices=[('core-services', 'core-services: public addresses of the core services (cloud-services)'), ('core-cloud', "core-cloud: the core servers' VMs (cloud-core)"), ('core-vyos', 'core-vyos: VyOS management (cloud-vyos)'), ('core-s2s', 'core-s2s: L2 VLAN to network (cloud-s2s)'), ('core-kvm', 'core-kvm: KVM hosts (one-core)'), ('core-lm', 'core-lm: live migration of VMs between the hosts (one-lm)'), ('core', 'core'), ('ix', 'ix'), ('evpn', 'evpn'), ('nfs', 'nfs'), ('iscsi', 'iscsi'), ('public', 'public: a public overlay of a portal platform (portal-publicNN, one per delegated subnet)')], max_length=16, verbose_name='type'),
        ),
    ]
