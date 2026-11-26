from odoo.exceptions import AccessError, UserError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestOrderCreation(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.origin = cls.env["system.confirmation.origin"].create({"name": "Creator origin"})
        cls.other_origin = cls.env["system.confirmation.origin"].create({"name": "Other origin"})
        cls.city = cls.env["system.confirmation.villes"].create({"name": "Creator city", "code": "901"})
        cls.product = cls.env["product.product"].create({"name": "Creator product", "sale_ok": True})
        cls.creator = cls.env["res.users"].create({
            "name": "Order creator", "login": "test_order_creator",
            "confirmation_origin_id": cls.origin.id,
            "groups_id": [(6, 0, [
                cls.env.ref("base.group_user").id,
                cls.env.ref("system_confirmation.group_order_creator").id,
            ])],
        })
        cls.customer = {"name": "Customer", "phone": "0600007890",
                        "address": "Address", "city": cls.city.id}

    def create_order(self):
        api = self.env["system.confirmation.colis"].with_user(self.creator)
        result = api.create_draft_order({
            "client": self.customer, "amount": 100,
            "items": [{"product_id": self.product.id, "quantity": 2}],
        })
        return self.env["system.confirmation.colis"].browse(result["id"])

    def test_create_and_edit_with_traceability(self):
        order = self.create_order()
        self.assertEqual(order.origin, self.origin)
        self.assertEqual(order.colis_state, "draft")
        self.assertEqual(order.items_line_ids.product_id, self.product)
        self.assertEqual(order.items_line_ids.quantity, 2)
        api = self.env["system.confirmation.colis"].with_user(self.creator)
        api.update_draft_customer(order.id, dict(self.customer, address="New address"))
        self.assertEqual(order.client.adresse, "New address")
        messages = order.message_ids.filtered(lambda message: "Customer information changed" in (message.body or ""))
        self.assertTrue(messages)
        self.assertEqual(messages[0].author_id, self.creator.partner_id)
        self.assertTrue(order.message_ids.filtered(lambda message: "Product added" in (message.body or "")))

    def test_creator_cannot_confirm_or_bypass(self):
        order = self.create_order()
        with self.assertRaises(AccessError):
            order.with_user(self.creator).action_confirm()
        with self.assertRaises(AccessError):
            order.with_user(self.creator).write({"colis_state": "confirmed"})
        with self.assertRaises(AccessError):
            order.client.with_user(self.creator).write({"name": "Bypass"})
        with self.assertRaises(AccessError):
            order.items_line_ids.with_user(self.creator).write({"quantity": 50})
        with self.assertRaises(AccessError):
            order.with_user(self.creator).create_package_api(order.with_user(self.creator))

    def test_viewer_can_use_creation_interface_but_not_modify_colis_directly(self):
        viewer = self.env["res.users"].create({
            "name": "Viewer order creator",
            "login": "test_viewer_order_creator",
            "confirmation_origin_id": self.origin.id,
            "groups_id": [(6, 0, [
                self.env.ref("base.group_user").id,
                self.env.ref("system_confirmation.group_system_confirmation_viewer").id,
            ])],
        })
        visible = self.env["ir.ui.menu"].with_user(viewer)._visible_menu_ids()
        self.assertIn(self.env.ref("system_confirmation.order_creation_process_system_confirmation").id, visible)
        api = self.env["system.confirmation.colis"].with_user(viewer)
        self.assertEqual(api.creation_options()["origin"], self.origin.name)
        result = api.create_draft_order({
            "client": self.customer, "amount": 100,
            "items": [{"product_id": self.product.id, "quantity": 2}],
        })
        order = self.env["system.confirmation.colis"].browse(result["id"])
        self.assertEqual(order.origin, self.origin)
        self.assertEqual(order.colis_state, "draft")
        original = api.creation_order_lines(order.id)
        api.update_draft_products(order.id, [dict(original[0], quantity=3)], original, 125, 100)
        api.update_draft_customer(order.id, dict(self.customer, address="Viewer address"))
        self.assertEqual(order.items_line_ids.quantity, 3)
        self.assertEqual(order.amount, 125)
        messages = order.message_ids.filtered(lambda message: "Amount changed" in (message.body or ""))
        self.assertTrue(messages)
        self.assertEqual(messages[0].author_id, viewer.partner_id)
        self.assertEqual(order.client.adresse, "Viewer address")
        with self.assertRaises(AccessError):
            order.with_user(viewer).write({"amount": 99})
        with self.assertRaises(AccessError):
            order.with_user(viewer).action_confirm()
        foreign = self.env["system.confirmation.colis"].create({"origin": self.other_origin.id})
        with self.assertRaises(AccessError):
            api.update_draft_customer(foreign.id, self.customer)
        viewer.confirmation_origin_id = False
        with self.assertRaises(UserError):
            api.creation_options()

    def test_origin_isolation_and_no_origin(self):
        order = self.create_order()
        other = self.env["system.confirmation.colis"].create({"origin": self.other_origin.id})
        api = self.env["system.confirmation.colis"].with_user(self.creator)
        self.assertIn(order.id, [item["id"] for item in api.creation_orders()["orders"]])
        self.assertNotIn(other.id, [item["id"] for item in api.creation_orders()["orders"]])
        self.assertNotIn(other, api.search([]))
        with self.assertRaises(AccessError):
            api.update_draft_customer(other.id, self.customer)
        self.creator.confirmation_origin_id = False
        self.assertFalse(api.search([]))
        with self.assertRaises(UserError):
            api.creation_orders()

    def test_confirmed_and_shared_customers_are_protected(self):
        order = self.create_order()
        api = self.env["system.confirmation.colis"].with_user(self.creator)
        other = self.env["system.confirmation.colis"].create({
            "origin": self.other_origin.id, "client": order.client.id, "colis_state": "confirmed",
        })
        with self.assertRaises(UserError):
            api.update_draft_customer(order.id, dict(self.customer, address="New address"))
        self.assertEqual(other.client.adresse, "Address")
        api.update_draft_customer(order.id, dict(self.customer, phone="0600007891", address="New address"))
        self.assertEqual(other.client.adresse, "Address")
        order.colis_state = "confirmed"
        with self.assertRaises(UserError):
            api.update_draft_customer(order.id, dict(self.customer, phone="0600007891"))

    def test_invalid_order_is_not_created(self):
        api = self.env["system.confirmation.colis"].with_user(self.creator)
        with self.assertRaises(UserError):
            api.create_draft_order({
                "client": self.customer, "amount": 10,
                "items": [{"product_id": self.product.id, "quantity": -1}],
            })
        self.assertFalse(self.env["system.confirmation.client"].search([
            ("telephone", "=", self.customer["phone"])
        ]))

    def test_edit_draft_products_and_audit(self):
        order = self.create_order()
        api = self.env["system.confirmation.colis"].with_user(self.creator)
        product = self.env["product.product"].create({"name": "Additional product", "sale_ok": True})
        original = api.creation_order_lines(order.id)
        changed = [dict(original[0], quantity=3), {
            "product_id": product.id, "quantity": 1,
        }]
        api.update_draft_products(order.id, changed, original)
        self.assertEqual(order.items_line_ids.filtered(lambda line: line.product_id == self.product).quantity, 3)
        self.assertEqual(order.items_line_ids.filtered(lambda line: line.product_id == product).quantity, 1)
        messages = order.message_ids
        self.assertTrue(messages.filtered(lambda message: "Product line changed" in (message.body or "")))
        self.assertTrue(messages.filtered(lambda message: "Product added" in (message.body or "")))
        self.assertEqual(messages.filtered(
            lambda message: "Product line changed" in (message.body or "")
        )[0].author_id, self.creator.partner_id)
        original = api.creation_order_lines(order.id)
        api.update_draft_products(order.id, [line for line in original if line["product_id"] != product.id], original)
        self.assertEqual(len(order.items_line_ids), 1)
        self.assertTrue(order.message_ids.filtered(lambda message: "Product removed" in (message.body or "")))

    def test_product_edit_rejects_stale_confirmed_and_foreign_orders(self):
        order = self.create_order()
        api = self.env["system.confirmation.colis"].with_user(self.creator)
        original = api.creation_order_lines(order.id)
        order.items_line_ids.write({"quantity": 4})
        with self.assertRaises(UserError):
            api.update_draft_products(order.id, original, original)
        self.assertEqual(order.items_line_ids.quantity, 4)
        other = self.env["system.confirmation.colis"].create({"origin": self.other_origin.id})
        with self.assertRaises(AccessError):
            api.creation_order_lines(other.id)
        with self.assertRaises(AccessError):
            api.update_draft_products(other.id, [], [])
        order.colis_state = "confirmed"
        with self.assertRaises(UserError):
            api.update_draft_products(order.id, [], api.creation_order_lines(order.id))
        self.assertEqual(order.items_line_ids.quantity, 4)

    def test_product_edit_rejects_invalid_or_other_order_lines(self):
        order = self.create_order()
        api = self.env["system.confirmation.colis"].with_user(self.creator)
        original = api.creation_order_lines(order.id)
        with self.assertRaises(UserError):
            api.update_draft_products(order.id, [dict(original[0], quantity=-2)], original)
        other = self.env["system.confirmation.colis"].create({
            "origin": self.origin.id,
            "items_line_ids": [(0, 0, {"product_id": self.product.id, "quantity": 1})],
        })
        with self.assertRaises(AccessError):
            api.update_draft_products(order.id, [{
                "line_id": other.items_line_ids.id, "product_id": self.product.id, "quantity": 1,
            }], original)
        self.assertEqual(order.items_line_ids.quantity, 2)

    def test_product_edit_amount_validation_and_concurrency(self):
        order = self.create_order()
        api = self.env["system.confirmation.colis"].with_user(self.creator)
        original = api.creation_order_lines(order.id)
        changed = [dict(original[0], quantity=3)]
        for invalid in (-1, "nan", "inf", ""):
            with self.assertRaises(UserError):
                api.update_draft_products(order.id, changed, original, invalid, 100)
        self.assertEqual(order.amount, 100)
        self.assertEqual(order.items_line_ids.quantity, 2)
        order.write({"amount": 110})
        with self.assertRaises(UserError):
            api.update_draft_products(order.id, changed, original, 120, 100)
        self.assertEqual(order.amount, 110)
        self.assertEqual(order.items_line_ids.quantity, 2)
        api.update_draft_products(order.id, changed, original, 0, 110)
        self.assertEqual(order.amount, 0)
        self.assertEqual(order.items_line_ids.quantity, 3)
        order.colis_state = "confirmed"
        with self.assertRaises(UserError):
            api.update_draft_products(order.id, changed, api.creation_order_lines(order.id), 120, 0)
        foreign = self.env["system.confirmation.colis"].create({"origin": self.other_origin.id})
        with self.assertRaises(AccessError):
            api.update_draft_products(foreign.id, [], [], 120, 0)

    def test_creator_sees_only_creation_menu_in_module(self):
        visible = self.env["ir.ui.menu"].with_user(self.creator)._visible_menu_ids()
        self.assertIn(self.env.ref("system_confirmation.system_confirmation_root").id, visible)
        self.assertIn(self.env.ref("system_confirmation.order_creation_process_system_confirmation").id, visible)
        for xmlid in (
            "system_confirmation_client_menu", "system_confirmation_colis_menu",
            "system_confirmation_origin_menu", "system_confirmation_reporting_menu",
            "system_confirmation_villes_menu", "system_confirmation_keyword_menu",
        ):
            self.assertNotIn(self.env.ref("system_confirmation." + xmlid).id, visible)

    def test_order_creation_assets_are_registered(self):
        assets = self.env["ir.asset"]
        paths = {item[0].lstrip("/") for item in assets._get_asset_paths(
            "web.assets_backend", assets._get_asset_params()
        )}
        for filename in ("main-view.js", "main-view.xml", "main-view.scss"):
            self.assertIn(
                "system_confirmation/static/src/components/main-view/" + filename, paths
            )

    def test_creation_options_include_city_codes(self):
        cities = self.env["system.confirmation.colis"].with_user(self.creator).creation_options()["cities"]
        city = next(item for item in cities if item["id"] == self.city.id)
        self.assertEqual(city["name"], self.city.name)
        self.assertEqual(city["code"], self.city.code)
