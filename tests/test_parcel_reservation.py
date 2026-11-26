import json

from odoo.tests.common import HttpCase, TransactionCase, tagged
from odoo.exceptions import UserError

from ..models.parcel_reservation import ParcelReservationError


@tagged("post_install", "-at_install")
class TestParcelReservation(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.location = cls.env.ref("stock.stock_location_stock")
        cls.env["ir.config_parameter"].sudo().set_param(
            "system_confirmation.location_id", str(cls.location.id)
        )
        cls.origin = cls.env["system.confirmation.origin"].create({"name": "Reservation origin"})
        cls.city = cls.env["system.confirmation.villes"].create({"name": "Reservation city", "code": "908"})
        cls.client = cls.env["system.confirmation.client"].create({
            "name": "Reservation client", "telephone": "0600991122",
            "adresse": "Test", "ville": cls.city.id,
        })
        cls.product = cls.env["product.product"].create({
            "name": "Reserved product", "type": "consu", "is_storable": True,
            "default_code": "RESERVATION-TEST",
        })
        cls.env["stock.quant"]._update_available_quantity(cls.product, cls.location, 5)
        cls.creator = cls.env["res.users"].create({
            "name": "Reservation creator", "login": "reservation_creator_test",
            "confirmation_origin_id": cls.origin.id,
            "groups_id": [(6, 0, [
                cls.env.ref("base.group_user").id,
                cls.env.ref("system_confirmation.group_order_creator").id,
            ])],
        })

    def _package(self, qty):
        return self.env["system.confirmation.colis"].create_package({
            "client": self.client.id, "origin": self.origin.id,
            "items_line_ids": [{"default_code": self.product.default_code, "quantity": qty}],
        })

    def _order(self, qty, phone="0600991133"):
        return self.env["system.confirmation.colis"].with_user(self.creator).create_draft_order({
            "client": {"name": "Order customer", "phone": phone, "address": "Test", "city": self.city.id},
            "amount": 10, "items": [{"product_id": self.product.id, "quantity": qty}],
        })

    def test_cross_channel_reservations_and_release(self):
        package = self._package(3)
        order = self.env["system.confirmation.colis"].browse(self._order(2)["id"])
        with self.assertRaisesRegex(ParcelReservationError, "déjà réservé"):
            self._order(1, "0600991144")
        self.assertFalse(self.env["system.confirmation.client"].search([
            ("telephone", "=", "0600991144"),
        ]))
        count = self.env["system.confirmation.colis"].search_count([])
        with self.assertRaises(ParcelReservationError):
            self._package(1)
        self.assertEqual(self.env["system.confirmation.colis"].search_count([]), count)
        order.action_cancel()
        self.assertTrue(self._package(2))
        self.assertEqual(package.items_line_ids.quantity, 3)

    def test_regular_form_lines_and_edits_are_checked(self):
        first = self.env["system.confirmation.colis"].create({
            "origin": self.origin.id,
            "items_line_ids": [(0, 0, {"product_id": self.product.id, "quantity": 4})],
        })
        with self.assertRaises(ParcelReservationError):
            self.env["system.confirmation.colis"].create({
                "origin": self.origin.id,
                "items_line_ids": [(0, 0, {"product_id": self.product.id, "quantity": 2})],
            })
        with self.assertRaises(ParcelReservationError):
            first.items_line_ids.write({"quantity": 6})
        with self.assertRaises(ParcelReservationError):
            first.items_line_ids.with_context(defer_parcel_reservation_check=True).write({"quantity": 6})
        self.assertEqual(first.items_line_ids.quantity, 4)
        first.items_line_ids.write({"quantity": 3})
        self.assertTrue(self._package(2))

    def test_duplicate_lines_and_odoo_stock_reservations(self):
        self.env["stock.quant"]._update_reserved_quantity(self.product, self.location, 2)
        with self.assertRaises(ParcelReservationError):
            self.env["system.confirmation.colis"].create({
                "origin": self.origin.id,
                "items_line_ids": [
                    (0, 0, {"product_id": self.product.id, "quantity": 2}),
                    (0, 0, {"product_id": self.product.id, "quantity": 2}),
                ],
            })
        self.assertTrue(self._package(3))

    def test_archiving_releases_and_unarchiving_rechecks(self):
        first = self._package(5)
        first.action_archive()
        self._package(5)
        with self.assertRaises(ParcelReservationError):
            first.action_unarchive()
        self.assertFalse(first.active)

    def test_confirmation_respects_other_drafts_after_stock_changes(self):
        first = self._package(2)
        self._package(3)
        self.env["stock.quant"]._update_available_quantity(self.product, self.location, -1)
        with self.assertRaisesRegex(UserError, "Insufficient stock"):
            first._check_stock_availability()


@tagged("post_install", "-at_install")
class TestParcelReservationHttp(HttpCase):
    def test_api_returns_conflict_and_rolls_back_client(self):
        location = self.env.ref("stock.stock_location_stock")
        params = self.env["ir.config_parameter"].sudo()
        params.set_param("system_confirmation.location_id", str(location.id))
        params.set_param("system_confirmation.api_token", "reservation-test-token")
        city = self.env["system.confirmation.villes"].create({"name": "Reservation HTTP city", "code": "909"})
        origin = self.env["system.confirmation.origin"].create({"name": "Reservation HTTP origin"})
        product = self.env["product.product"].create({
            "name": "Reservation HTTP product", "type": "consu", "is_storable": True,
        })
        self.env["stock.quant"]._update_available_quantity(product, location, 1)
        self.env["system.confirmation.colis"].create({
            "origin": origin.id,
            "items_line_ids": [(0, 0, {"product_id": product.id, "quantity": 1})],
        })
        response = self.url_open("/create_package", data={
            "token": "reservation-test-token", "name": "HTTP client",
            "telephone": "0600991155", "adresse": "Test address",
            "ville": str(city.code), "origin": str(origin.id),
            "items_line_ids": json.dumps([{"product_id": product.id, "quantity": 1}]),
        })
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "product_reserved")
        self.assertIn("déjà réservé", response.json()["message"])
        self.assertFalse(self.env["system.confirmation.client"].search([
            ("telephone", "=", "0600991155"),
        ]))
