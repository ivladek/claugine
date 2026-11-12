"""Every address of the portal, in one file.

Grouped by area and summarised in `worklog/internals.md` section 7. Two kinds of entry are not pages: `/active`,
`/standby`, `/maintenance` and `/new` are what HAProxy polls to find each node's role (`accounts/ha.py`), and the
former `/tenants/…` addresses are redirected to `/projects/…` by `_old_tenant_url`.
"""

from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.http import HttpResponsePermanentRedirect
from django.urls import include, path, re_path

from accounts import forms as acc_forms
from accounts import ha, passkeys
from accounts import views as acc
from content import views as content
from inventory import core_views as core
from inventory import edit_views as inved
from inventory import hw_views as hw
from inventory import network_views as invnet
from inventory import profiles as prof
from inventory import registry_views as reg
from inventory import secret_views as secv
from inventory import template_views as tplv
from inventory import views as inv
from tenants import deletion_views as delv
from tenants import igw_views as igwv
from tenants import network_views as netv
from tenants import provision as provv
from tenants import sshkey_views as keyv
from tenants import views as ten


def _old_tenant_url(request, rest=""):
    """Links to /tenants/… (e-mails already sent, bookmarks) go on to /projects/… (owner 2026-10-31)."""
    new = request.path.replace("/tenants/", "/projects/", 1)
    q = request.META.get("QUERY_STRING", "")
    return HttpResponsePermanentRedirect(new + (f"?{q}" if q else ""))


urlpatterns = [
    re_path(r"^(?:my/)?tenants/(?P<rest>.*)$", _old_tenant_url),  # the former addresses
    # this node's role, for HAProxy (accounts/ha.py): 200 when the node has it
    path("active", ha.status_view, {"name": "active"}, name="ha_active"),
    path("standby", ha.status_view, {"name": "standby"}, name="ha_standby"),
    path("maintenance", ha.status_view, {"name": "maintenance"}, name="ha_maintenance"),
    path("new", ha.status_view, {"name": "new"}, name="ha_new"),
    path("", content.home_view, name="home"),
    path("news/<int:pk>/", content.article_view, name="article"),
    path("guide/", content.guide_view, name="guide"),
    path("guide/<slug:page>/", content.guide_view, name="guide_page"),
    path("login/", acc.login_view, name="login"),
    path("login/code/", acc.login_otp_view, name="login_otp"),
    path("login/passkey/options/", passkeys.auth_options_view, name="passkey_auth_options"),
    path("login/passkey/verify/", passkeys.auth_verify_view, name="passkey_auth_verify"),
    path("account/passkeys/options/", passkeys.register_options_view, name="passkey_register_options"),
    path("account/passkeys/verify/", passkeys.register_verify_view, name="passkey_register_verify"),
    path("account/passkeys/<int:pid>/remove/", passkeys.remove_view, name="passkey_remove"),
    path("logout/", acc.logout_view, name="logout"),
    path("register/", acc.register_view, name="register"),
    path("register/domain/", acc.domain_check_view, name="domain_check"),
    path("verify/<str:token>/", acc.verify_email_view, name="verify_email"),
    path("invite/<str:token>/", acc.accept_invitation_view, name="accept_invitation"),
    path(
        "password-reset/",
        auth_views.PasswordResetView.as_view(
            template_name="accounts/password_reset.html",
            email_template_name="emails/password_reset.txt",
            subject_template_name="emails/password_reset_subject.txt",
        ),
        name="password_reset",
    ),
    path(
        "password-reset/sent/",
        auth_views.PasswordResetDoneView.as_view(template_name="accounts/password_reset_done.html"),
        name="password_reset_done",
    ),
    path(
        "password-reset/<uidb64>/<token>/",
        auth_views.PasswordResetConfirmView.as_view(
            template_name="accounts/password_reset_confirm.html", form_class=acc_forms.PolicySetPasswordForm
        ),
        name="password_reset_confirm",
    ),
    path(
        "password-reset/done/",
        auth_views.PasswordResetCompleteView.as_view(template_name="accounts/password_reset_complete.html"),
        name="password_reset_complete",
    ),
    path("dashboard/", acc.dashboard_view, name="dashboard"),
    path("act-as/", acc.act_as_view, name="act_as"),  # u12: super admin act-as-user
    path("account/", acc.profile_view, name="profile"),
    path("account/2fa/setup/", acc.totp_setup_view, name="totp_setup"),
    path("account/2fa/disable/", acc.totp_disable_view, name="totp_disable"),
    path("account/photo/<int:pk>/", acc.photo_view, name="user_photo"),
    # super admin
    path("staff/invitations/", acc.staff_invitations_view, name="staff_invitations"),
    path("staff/invitations/<int:pk>/revoke/", acc.revoke_invitation_view, name="revoke_invitation"),
    path("staff/users/", acc.staff_users_view, name="staff_users"),
    path("staff/users/<int:pk>/roles/", acc.staff_roles_view, name="staff_roles"),
    path("staff/users/<int:pk>/toggle/", acc.toggle_user_active_view, name="toggle_user_active"),
    path("staff/users/<int:pk>/invite/", acc.invite_person_view, name="invite_person"),
    path("people/add/", acc.staff_person_add_view, name="person_add"),
    path("people/invite-new/", acc.staff_invite_new_view, name="staff_invite_new"),  # u03
    path("people/add-customer/", acc.customer_add_view, name="customer_add"),  # u03
    path("people/invite/", acc.customer_invite_view, name="customer_invite"),
    path("people/<int:pk>/roles/", acc.person_roles_view, name="person_roles"),
    path("people/<int:pk>/edit/", acc.person_edit_view, name="person_edit"),
    path("brand/logo.png", acc.brand_logo_view, name="brand_logo"),
    path("settings/mail-templates/<str:name>/", acc.mail_template_view, name="mail_template"),
    path("staff/super-admin-removal/<int:pk>/<str:action>/", acc.super_admin_removal_view, name="super_admin_removal"),
    path("people/<int:pk>/archive/", acc.archive_person_view, name="person_archive"),
    path("people/<int:pk>/delete/", acc.delete_person_view, name="person_delete"),
    path("people/<int:pk>/role/", acc.person_role_set_view, name="person_role_set"),
    path("staff/sales-reps/", acc.sales_reps_view, name="sales_reps"),
    path("staff/sales-reps/new/", acc.sales_rep_edit_view, name="sales_rep_new"),
    path("staff/sales-reps/<int:pk>/", acc.sales_rep_edit_view, name="sales_rep_edit"),
    path("staff/sales-reps/<int:pk>/invite/", acc.sales_rep_invite_view, name="sales_rep_invite"),
    path("staff/sales-reps/<int:pk>/delete/", acc.sales_rep_delete_view, name="sales_rep_delete"),
    path("staff/settings/", acc.portal_settings_view, name="portal_settings"),
    path("staff/settings/values/", acc.portal_settings_values_view, name="portal_settings_values"),
    path("staff/audit/", acc.audit_log_view, name="audit_log"),
    path("staff/tasks/", acc.tasks_view, name="tasks"),
    path("docs/", acc.docs_view, name="docs"),
    path("docs/data/", acc.docs_view, {"page": "data"}, name="docs_data"),
    path("docs/network/", acc.docs_view, {"page": "network"}, name="docs_network"),
    # tenants and organizations
    path("projects/new/", ten.staff_new_tenant_view, name="tenant_new"),
    path("projects/", ten.tenant_list_view, name="tenant_list"),
    path("projects/<int:pk>/", ten.tenant_detail_view, name="tenant_detail"),
    path("projects/<int:pk>/tracking/", ten.tenant_tracking_view, name="tenant_tracking"),
    path("projects/<int:pk>/approve/rm/", ten.rm_approve_view, name="tenant_rm_approve"),
    path("projects/<int:pk>/approve/support/", ten.support_approve_view, name="tenant_support_approve"),
    path("projects/<int:pk>/approve/sales/", ten.sales_approve_view, name="tenant_sales_approve"),
    path("projects/<int:pk>/reject/", ten.reject_view, name="tenant_reject"),
    path("projects/<int:pk>/edit/", ten.tenant_edit_view, name="tenant_edit"),
    path("projects/<int:pk>/edit-all/", ten.tenant_edit_all_view, name="tenant_edit_all"),  # correction 15
    path("approvals/", ten.approvals_view, name="approvals"),
    path("projects/<int:pk>/deploy/", ten.deploy_view, name="tenant_deploy"),
    path("projects/<int:pk>/provision/", provv.provision_view, name="tenant_provision"),
    path("projects/<int:pk>/resend/", provv.resend_view, name="tenant_resend"),  # correction 15
    path("projects/<int:pk>/pools/order/", ten.pool_order_view, name="tenant_pool_order"),  # correction 15
    path("projects/<int:pk>/pools/remove/", ten.pool_remove_view, name="tenant_pool_remove"),
    path("projects/<int:pk>/gateways/check/", ten.gateway_check_view, name="tenant_gw_check"),
    path("projects/<int:pk>/gateways/<str:role>/redeploy/", ten.gateway_redeploy_view, name="tenant_gw_redeploy"),
    path("projects/<int:pk>/deployment.json", ten.deployment_status_view, name="tenant_deployment_status"),
    path("projects/<int:pk>/access/", ten.reveal_password_view, name="tenant_reveal_password"),
    path("projects/<int:pk>/access/copy/", ten.secret_copy_view, name="tenant_secret_copy"),
    path("projects/<int:pk>/password/", ten.tenant_password_view, name="tenant_password"),
    path("projects/<int:pk>/password/keep/", igwv.password_keep_view, name="tenant_password_keep"),
    path("projects/<int:pk>/igw/configs/new/", igwv.igw_config_view, name="tenant_igw_config_new"),
    path("projects/<int:pk>/igw/configs/<int:cid>/", igwv.igw_config_view, name="tenant_igw_config"),
    path("projects/<int:pk>/igw/login/", igwv.igw_login_view, name="tenant_igw_login"),
    path("projects/<int:pk>/igw/", igwv.igw_view, name="tenant_igw"),
    path("projects/<int:pk>/igw/download/", igwv.igw_download_view, name="tenant_igw_download"),
    path("projects/<int:pk>/resources/request/", ten.resource_request_view, name="tenant_resource_request"),
    path("projects/<int:pk>/backups/request/", ten.backups_request_view, name="tenant_backups_request"),
    path("projects/<int:pk>/config-check/", keyv.config_check_view, name="tenant_config_check"),
    path("projects/<int:pk>/ssh-keys/add/", keyv.ssh_keys_add_view, name="tenant_ssh_keys_add"),
    path("projects/<int:pk>/ssh-keys/<int:kid>/<str:action>/", keyv.ssh_key_action_view, name="tenant_ssh_key_action"),
    path("projects/<int:pk>/deletion/request/", delv.deletion_request_view, name="tenant_deletion_request"),
    path(
        "projects/<int:pk>/deletion/<int:did>/<str:decision>/", delv.deletion_decide_view, name="tenant_deletion_decide"
    ),
    path("projects/<int:pk>/deletion/now/", delv.delete_now_view, name="tenant_delete_now"),
    path("projects/<int:pk>/networks/request/", netv.network_request_view, name="tenant_network_request"),
    path("projects/<int:pk>/l2/add/", netv.l2_add_view, name="tenant_l2_add"),
    path("projects/<int:pk>/l2/internal/", netv.l2_internal_add_view, name="tenant_l2_internal"),
    path("projects/<int:pk>/l2/<int:cid>/vlan/", netv.l2_vlan_view, name="tenant_l2_vlan"),
    path("projects/<int:pk>/l2/<int:cid>/network/", netv.l2_network_view, name="tenant_l2_network"),
    path("projects/<int:pk>/networks/<int:rid>/retry/", netv.network_retry_view, name="tenant_network_retry"),
    path(
        "projects/<int:pk>/networks/<int:rid>/<str:decision>/", netv.network_decide_view, name="tenant_network_decide"
    ),
    path(
        "projects/<int:pk>/resources/changes/<int:cid>/<str:decision>/",
        ten.resource_decide_view,
        name="tenant_resource_decide",
    ),
    path("projects/<int:pk>/staff-member/", ten.tenant_staff_member_view, name="tenant_staff_member"),
    path("projects/<int:pk>/cancel/", ten.cancel_view, name="tenant_cancel"),
    path("projects/<int:pk>/delete/", ten.delete_request_view, name="tenant_delete"),
    path("projects/<int:pk>/stage/", ten.stage_view, name="tenant_stage"),
    path("projects/<int:pk>/status/", ten.status_view, name="tenant_status"),
    path("projects/<int:pk>/review/", ten.review_view, name="tenant_review"),
    path("projects/<int:pk>/move/", ten.tenant_move_view, name="tenant_move"),
    path("projects/<int:pk>/move/<int:mid>/<str:action>/", ten.tenant_move_decide_view, name="tenant_move_decide"),
    path("projects/<int:pk>/resources/", ten.tenant_resources_view, name="tenant_resources"),
    path("projects/<int:pk>/storage-split/", ten.storage_split_view, name="tenant_storage_split"),
    path("projects/<int:pk>/invite/", ten.tenant_invite_view, name="tenant_invite"),
    path("organizations/", ten.org_list_view, name="org_list"),
    path("organizations/new/", ten.org_edit_view, name="org_new"),
    path("organizations/invite/", ten.org_invite_view, name="org_invite"),
    path("organizations/<int:pk>/", ten.org_detail_view, name="org_detail"),
    path("organizations/<int:pk>/registration/<str:action>/", ten.org_registration_view, name="org_registration"),
    path("organizations/<int:pk>/edit/", ten.org_edit_view, name="org_edit"),
    path("organizations/<int:pk>/users/add/", ten.org_user_view, name="org_user_add"),  # correction 15
    path("organizations/<int:pk>/users/<int:uid>/", ten.org_user_view, name="org_user_edit"),
    path("organizations/<int:pk>/resources/", ten.org_resources_view, name="org_resources"),
    path("organizations/<int:pk>/delete/", ten.org_delete_view, name="org_delete"),
    path("organizations/<int:pk>/unarchive/", ten.org_delete_view, {"action": "unarchive"}, name="org_unarchive"),
    path("organizations/<int:pk>/merge/", ten.org_merge_view, name="org_merge"),
    path("my/", ten.my_org_view, name="my_org"),
    path("my/projects/new/", ten.new_tenant_view, name="new_tenant"),
    path("my/users/", ten.org_users_view, name="org_users"),
    path("my/logs/", ten.org_logs_view, name="org_logs"),
    path("my/tasks/", ten.org_tasks_view, name="org_tasks"),
    path("my/keys/", keyv.org_keys_view, name="org_keys"),
    path("my/people/<int:pk>/", ten.person_view, name="person"),
    path("my/people/<int:pk>/remove/", ten.remove_person_view, name="remove_person"),
    path("my/join-requests/<int:pk>/<str:decision>/", ten.decide_join_view, name="decide_join"),
    # infrastructure: resources, platforms, address registry
    path("infrastructure/sites/", inv.inventory_view, name="inventory"),
    path("infrastructure/profiles/", inv.profiles_view, name="infra_profiles"),
    path("infrastructure/sites/<str:code>/ipmi/", hw.site_ipmi_view, name="infra_site_ipmi"),
    path("infrastructure/sites/<str:code>/core/new/", core.save_view, name="core_new"),
    path("infrastructure/sites/<str:code>/core/read/", core.read_view, name="core_read_all"),
    path("infrastructure/sites/<str:code>/core/seed/", core.seed_view, name="core_seed"),
    path("infrastructure/sites/<str:code>/core/refresh/", core.refresh_view, name="core_refresh"),
    path("infrastructure/sites/<str:code>/core/<int:pk>/", core.save_view, name="core_save"),
    path("infrastructure/sites/<str:code>/core/<int:pk>/delete/", core.delete_view, name="core_delete"),
    path("infrastructure/sites/<str:code>/core/<int:pk>/read/", core.read_view, name="core_read"),
    path("infrastructure/sites/<str:code>/core/<int:pk>/vm/<int:node>/", core.plan_view, name="core_plan"),
    path("infrastructure/sites/<str:code>/core/<int:pk>/vm/<int:node>/config/", core.config_view, name="core_config"),
    path("infrastructure/sites/<str:code>/core/<int:pk>/check/", core.check_view, name="core_check"),
    path(
        "infrastructure/sites/<str:code>/core/<int:pk>/vm/<int:node>/redeploy/",
        core.redeploy_view,
        name="core_redeploy",
    ),
    path(
        "infrastructure/sites/<str:code>/core/<int:pk>/vm/<int:node>/rollback/",
        core.rollback_view,
        name="core_rollback",
    ),
    path("infrastructure/sites/<str:code>/core/<int:pk>/vm/<int:node>/finish/", core.finish_view, name="core_finish"),
    path("infrastructure/sites/<str:code>/core/<int:pk>/vm/<int:node>/clean/", core.clean_view, name="core_clean"),
    path("infrastructure/sites/<str:code>/", inv.site_view, name="infra_site"),
    path("infrastructure/templates/", tplv.templates_view, name="infra_templates"),
    path("infrastructure/templates/new/", tplv.template_new_view, name="infra_template_new"),
    path("infrastructure/templates/<int:pk>/", tplv.template_save_view, name="infra_template_save"),
    path("infrastructure/templates/<int:pk>/delete/", tplv.template_delete_view, name="infra_template_delete"),
    path("infrastructure/templates/<int:pk>/reveal/", tplv.template_reveal_view, name="infra_template_reveal"),
    path("projects/<int:pk>/config/<str:role>/", tplv.tenant_config_view, name="tenant_config"),
    path("infrastructure/passwords/", secv.secrets_view, name="infra_passwords"),
    path("infrastructure/keys/", secv.old_url_view, {"tab": "key"}, name="infra_keys"),  # old links
    path("infrastructure/credentials/", secv.old_url_view, name="infra_credentials"),  # old links
    path("infrastructure/secrets/new/<str:kind>/", secv.secret_save_view, name="infra_secret_add"),
    path("infrastructure/secrets/<int:pk>/", secv.secret_save_view, name="infra_secret_save"),
    path("infrastructure/secrets/<int:pk>/reveal/", secv.secret_reveal_view, name="infra_secret_reveal"),
    path("infrastructure/secrets/<int:pk>/values/", secv.secret_values_view, name="infra_secret_values"),
    path("infrastructure/secrets/<int:pk>/replace/", secv.secret_replace_view, name="infra_secret_replace"),
    path("infrastructure/secrets/<int:pk>/check/", secv.secret_check_view, name="infra_secret_check"),
    path("infrastructure/secrets/<int:pk>/archive/", secv.secret_archive_view, name="infra_secret_archive"),
    path("infrastructure/platforms/", inv.platforms_view, name="infra_platforms"),  # old links
    path("infrastructure/addresses/", reg.registry_view, name="registry"),  # old links
    path("infrastructure/addresses/<int:pk>/release/", reg.prefix_release_view, name="prefix_release"),
    path("infrastructure/sites/<str:code>/ip-subnets/add/", reg.ipsubnet_add_view, name="ipsubnet_add"),
    path("infrastructure/ip-subnets/<int:pk>/edit/", reg.ipsubnet_edit_view, name="ipsubnet_edit"),
    path("infrastructure/ip-subnets/<int:pk>/delete/", reg.ipsubnet_delete_view, name="ipsubnet_delete"),
    path("infrastructure/edit/sites/", inved.site_save_view, name="inv_site_save"),
    path("infrastructure/edit/sites/<str:code>/delete/", inved.site_delete_view, name="inv_site_delete"),
    path("infrastructure/edit/hosts/", inved.host_add_view, name="inv_host_add"),
    path("infrastructure/edit/hosts/<str:site>/<str:name>/", inved.host_edit_view, name="inv_host_edit"),
    path("infrastructure/sites/<str:site>/networks/new/", invnet.network_save_view, name="inv_net_new"),
    path("infrastructure/sites/<str:site>/networks/<int:pk>/", invnet.network_save_view, name="inv_net_save"),
    path(
        "infrastructure/sites/<str:site>/networks/<int:pk>/delete/", invnet.network_delete_view, name="inv_net_delete"
    ),
    path("infrastructure/sites/<str:site>/networks/<int:pk>/bgp/", invnet.bgp_save_view, name="inv_net_bgp"),
    path(
        "infrastructure/sites/<str:site>/networks/<int:pk>/bgp/values/",
        invnet.bgp_values_view,
        name="inv_net_bgp_values",
    ),
    path("infrastructure/sites/<str:site>/networks/<int:pk>/ranges/new/", invnet.range_save_view, name="inv_range_new"),
    path(
        "infrastructure/sites/<str:site>/networks/<int:pk>/ranges/<int:rid>/",
        invnet.range_save_view,
        name="inv_range_save",
    ),
    path(
        "infrastructure/sites/<str:site>/networks/<int:pk>/ranges/<int:rid>/delete/",
        invnet.range_delete_view,
        name="inv_range_delete",
    ),
    path("infrastructure/edit/hosts/<str:site>/<str:name>/remove/", inved.host_remove_view, name="inv_host_remove"),
    path("infrastructure/edit/hosts/<str:site>/<str:name>/detect/", inved.host_detect_view, name="inv_host_detect"),
    path("infrastructure/profiles/new/", prof.profile_save_view, name="inv_profile_save"),
    path("infrastructure/profiles/<str:code>/", prof.profile_save_view, name="inv_profile_edit"),
    path("infrastructure/profiles/<str:code>/delete/", prof.profile_delete_view, name="inv_profile_delete"),
    path("infrastructure/profiles/<str:code>/verify/", prof.profile_verify_view, name="inv_profile_verify"),
    path("infrastructure/profiles/<str:code>/check/", prof.profile_check_view, name="inv_profile_check"),
    path(
        "infrastructure/profiles/<str:code>/site-settings/",
        hw.profile_site_settings_view,
        name="infra_profile_site_settings",
    ),
    path("infrastructure/sites/<str:site>/storage/new/", inved.storage_save_view, name="inv_storage_new"),
    path("infrastructure/sites/<str:site>/storage/<int:pk>/", inved.storage_save_view, name="inv_storage_save"),
    path(
        "infrastructure/sites/<str:site>/storage/<int:pk>/delete/", inved.storage_delete_view, name="inv_storage_delete"
    ),
    path(
        "infrastructure/sites/<str:site>/storage/<int:pk>/volumes/new/", inved.volume_save_view, name="inv_volume_new"
    ),
    path(
        "infrastructure/sites/<str:site>/storage/<int:pk>/volumes/<int:vid>/",
        inved.volume_save_view,
        name="inv_volume_save",
    ),
    path(
        "infrastructure/sites/<str:site>/storage/<int:pk>/volumes/<int:vid>/delete/",
        inved.volume_delete_view,
        name="inv_volume_delete",
    ),
    path(
        "infrastructure/platforms/<str:site>/<str:name>/datastores/new/", inved.datastore_save_view, name="inv_ds_new"
    ),
    path(
        "infrastructure/platforms/<str:site>/<str:name>/datastores/<int:pk>/",
        inved.datastore_save_view,
        name="inv_ds_save",
    ),
    path(
        "infrastructure/platforms/<str:site>/<str:name>/datastores/<int:pk>/delete/",
        inved.datastore_delete_view,
        name="inv_ds_delete",
    ),
    path("infrastructure/platforms/<str:site>/<str:name>/networks/new/", inved.vnet_save_view, name="inv_vn_new"),
    path("infrastructure/platforms/<str:site>/<str:name>/networks/<int:pk>/", inved.vnet_save_view, name="inv_vn_save"),
    path(
        "infrastructure/platforms/<str:site>/<str:name>/networks/<int:pk>/delete/",
        inved.vnet_delete_view,
        name="inv_vn_delete",
    ),
    path("infrastructure/edit/platforms/", inved.platform_save_view, name="inv_platform_save"),
    path(
        "infrastructure/edit/platforms/<str:site>/<str:name>/delete/",
        inved.platform_delete_view,
        name="inv_platform_delete",
    ),
    path(
        "infrastructure/edit/platforms/<str:site>/<str:name>/gateway/",
        inved.platform_gateway_view,
        name="inv_platform_gateway",
    ),
    path("infrastructure/edit/clusters/", inved.cluster_add_view, name="inv_cluster_add"),
    path(
        "infrastructure/edit/clusters/<str:site>/<str:platform>/<str:name>/",
        inved.cluster_save_view,
        name="inv_cluster_save",
    ),
    path(
        "infrastructure/edit/clusters/<str:site>/<str:platform>/<str:name>/remove/",
        inved.cluster_remove_view,
        name="inv_cluster_remove",
    ),
    path(
        "infrastructure/edit/clusters/<str:site>/<str:platform>/<str:name>/hosts/",
        inved.cluster_hosts_view,
        name="inv_cluster_hosts",
    ),
    path(
        "infrastructure/edit/clusters/<str:site>/<str:platform>/<str:name>/hosts/<str:host>/remove/",
        inved.cluster_host_remove_view,
        name="inv_cluster_host_remove",
    ),
    path("capacity/", inv.capacity_view, name="capacity"),  # old links
    path("infrastructure/endpoints/<str:key>/test/", inv.control_test_view, name="platform_control_test"),
    path("infrastructure/endpoints/<str:key>/mode/<str:mode>/", inv.control_mode_view, name="platform_control_mode"),
    path(
        "infrastructure/endpoints/<str:key>/consistency/repair/",
        inv.control_consistency_repair_view,
        name="platform_consistency_repair",
    ),
    path("infrastructure/endpoints/<str:key>/collect/", inv.control_collect_view, name="platform_control_collect"),
    path("infrastructure/endpoints/<str:key>/images/", inv.service_image_save_view, name="service_image_add"),
    path("infrastructure/endpoints/<str:key>/images/<int:pk>/", inv.service_image_save_view, name="service_image_save"),
    path(
        "infrastructure/endpoints/<str:key>/images/<int:pk>/delete/",
        inv.service_image_delete_view,
        name="service_image_delete",
    ),
    # content
    path("manage/content/", content.manage_articles_view, name="manage_articles"),
    path("manage/content/new/", content.edit_article_view, name="article_new"),
    path("manage/content/<int:pk>/", content.edit_article_view, name="article_edit"),
    path("manage/services/new/", content.edit_component_view, name="component_new"),
    path("manage/services/<int:pk>/", content.edit_component_view, name="component_edit"),
    path("manage/<str:what>/<int:pk>/delete/", content.delete_content_view, name="content_delete"),
    path("i18n/", include("django.conf.urls.i18n")),
    path("django-admin/", admin.site.urls),
]
