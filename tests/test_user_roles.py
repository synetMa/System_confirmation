from lxml import etree

from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestUserRoles(TransactionCase):
    def test_role_selector_and_switching(self):
        category = self.env.ref("system_confirmation.module_category_system_confirmation")
        groups = self.env["res.groups"]
        role_groups = [
            self.env.ref("system_confirmation.group_system_confirmation_viewer"),
            self.env.ref("system_confirmation.group_system_confirmation_user"),
            self.env.ref("system_confirmation.group_system_confirmation_return_validator"),
            self.env.ref("system_confirmation.group_system_confirmation_manager"),
        ]
        applications = [item for item in groups.get_groups_by_application() if item[0] == category]
        self.assertEqual(len(applications), 1)
        self.assertEqual(applications[0][1], "selection")
        self.assertEqual(set(applications[0][2].ids), {group.id for group in role_groups})

        field_name = "sel_groups_" + "_".join(str(group.id) for group in sorted(role_groups, key=lambda group: group.id))
        view = self.env.ref("base.user_groups_view").with_context(lang="en_US")
        arch = etree.fromstring(view.arch)
        self.assertTrue(arch.xpath("//group[@string='System Confirmation']/field[@name=$name]", name=field_name))

        user = self.env["res.users"].create({
            "name": "Role switching test",
            "login": "confirmation_role_switching_test",
            "groups_id": [(6, 0, [self.env.ref("base.group_user").id])],
        })
        for selected in reversed(role_groups + [False]):
            user.write({field_name: selected.id if selected else False})
            self.assertEqual(user.read([field_name])[0][field_name], selected.id if selected else False)
            expected = role_groups[:role_groups.index(selected) + 1] if selected else []
            self.assertEqual(user.groups_id & sum(role_groups, groups.browse()), sum(expected, groups.browse()))
            self.assertIn(self.env.ref("base.group_user"), user.groups_id)

    def test_viewer_can_read_but_cannot_change_or_confirm_colis(self):
        origin = self.env["system.confirmation.origin"].create({"name": "Viewer origin"})
        other_origin = self.env["system.confirmation.origin"].create({"name": "Other origin"})
        user = self.env["res.users"].create({
            "name": "Colis viewer",
            "login": "confirmation_viewer_test",
            "confirmation_origin_id": origin.id,
            "groups_id": [(6, 0, [
                self.env.ref("base.group_user").id,
                self.env.ref("system_confirmation.group_system_confirmation_viewer").id,
            ])],
        })
        colis = self.env["system.confirmation.colis"].create({"origin": origin.id})
        other = self.env["system.confirmation.colis"].create({"origin": other_origin.id})
        product = self.env["product.product"].create({"name": "Viewer test product"})
        line = self.env["system.confirmation.colis.lines"].create({
            "colis_id": colis.id,
            "product_id": product.id,
            "quantity": 1,
        })
        viewed = colis.with_user(user)

        self.assertEqual(viewed.read(["name"])[0]["name"], colis.name)
        self.assertEqual(line.with_user(user).read(["quantity"])[0]["quantity"], 1)
        self.assertFalse(other.with_user(user).search([("id", "=", other.id)]))
        for operation in (
            lambda: viewed.write({"amount": 99}),
            lambda: line.with_user(user).write({"quantity": 2}),
            lambda: line.with_user(user).check_access("unlink"),
            lambda: self.env["system.confirmation.colis"].with_user(user).create({"origin": origin.id}),
            lambda: viewed.check_access("unlink"),
            viewed.action_confirm,
            viewed.action_retry_pdf,
            viewed.action_deliver,
            lambda: viewed.create_package_api(viewed),
        ):
            with self.assertRaises(AccessError):
                operation()
        self.assertEqual(colis.colis_state, "draft")
        self.assertFalse(colis.workflow_lock_at)

        form = self.env["system.confirmation.colis"].with_user(user).get_view(
            self.env.ref("system_confirmation.system_confirmation_colis_form").id, "form"
        )
        arch = etree.fromstring(form["arch"])
        self.assertFalse(arch.xpath("//button[@name='action_confirm' or @name='action_retry_pdf' or @name='action_deliver' or @name='action_register_return']"))

    def test_tracking_and_confirmation_roles_are_independent(self):
        internal = self.env.ref("base.group_user")
        confirmation_manager = self.env.ref("system_confirmation.group_system_confirmation_manager")
        tracking_manager = self.env.ref("suivi_colis.group_track_package_manager")
        export = self.env.ref("base.group_allow_export")
        user = self.env["res.users"].create({
            "name": "Independent roles test",
            "login": "confirmation_independent_roles_test",
            "role": "Confirmateur",
            "groups_id": [(6, 0, [internal.id])],
        })
        tracking = self.env["track.package"].create({"barcode": "role-test-barcode"})
        origin = self.env["system.confirmation.origin"].create({"name": "Role test origin"})
        confirmation = self.env["system.confirmation.colis"].create({"origin": origin.id})

        user.write({"groups_id": [(4, confirmation_manager.id)]})
        self.assertEqual(user.role, "Confirmateur")
        self.assertIn(export, user.groups_id)
        self.assertNotIn(tracking_manager, user.groups_id)
        self.assertEqual(
            confirmation.with_user(user).export_data(["name"])["datas"],
            [[confirmation.name]],
        )
        with self.assertRaises(AccessError):
            tracking.with_user(user).export_data(["barcode"])
        with self.assertRaises(AccessError):
            tracking.with_user(user).write({"state": "Livree"})
        user._sync_role_security_groups()  # The same synchronization runs on startup.
        self.assertIn(export, user.groups_id)
        self.assertNotIn(tracking_manager, user.groups_id)

        user.write({"role": "Manager"})
        self.assertIn(confirmation_manager, user.groups_id)
        self.assertIn(tracking_manager, user.groups_id)
        self.assertEqual(tracking.with_user(user).export_data(["barcode"])["datas"], [["role-test-barcode"]])

        user.write({"groups_id": [(3, confirmation_manager.id)]})
        self.assertIn(tracking_manager, user.groups_id)
        self.assertIn(export, user.groups_id)
        user.write({"role": "Confirmateur"})
        self.assertNotIn(tracking_manager, user.groups_id)
        self.assertNotIn(export, user.groups_id)
