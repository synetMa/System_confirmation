import base64
import io
from unittest.mock import patch

import requests
from lxml import etree

from odoo.exceptions import AccessError, UserError
from odoo.tests.common import TransactionCase, tagged
from odoo.tools.pdf import PdfFileReader, PdfFileWriter
from odoo.tools.safe_eval import safe_eval


@tagged("post_install", "-at_install")
class TestSystemConfirmation(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.origin = cls.env["system.confirmation.origin"].create(
            {"name": "Test Origin"}
        )
        cls.ville = cls.env["system.confirmation.villes"].create(
            {"name": "Casablanca", "code": "1"}
        )
        cls.client = cls.env["system.confirmation.client"].create(
            {
                "name": "Test Client",
                "telephone": "0611111111",
                "ville": cls.ville.id,
                "adresse": "1 Test Street",
            }
        )
        cls.product = cls.env["product.product"].create(
            {
                "name": "Test Product",
                "type": "consu",
                "is_storable": True,
                "list_price": 100.0,
                "standard_price": 60.0,
            }
        )
        cls.stock_location = cls.env.ref("stock.stock_location_stock")
        cls.env["ir.config_parameter"].sudo().set_param(
            "system_confirmation.location_id", str(cls.stock_location.id)
        )
        # Make sure the tests never hit the real API
        cls.env["ir.config_parameter"].sudo().set_param(
            "system_confirmation.quicklivraison_api_key", ""
        )
        cls.env["stock.quant"].create(
            {
                "product_id": cls.product.id,
                "location_id": cls.stock_location.id,
                "quantity": 100.0,
            }
        )

    def _create_colis(self, with_lines=True):
        colis = self.env["system.confirmation.colis"].create(
            {
                "client": self.client.id,
                "origin": self.origin.id,
            }
        )
        if with_lines:
            self.env["system.confirmation.colis.lines"].create(
                {
                    "colis_id": colis.id,
                    "product_id": self.product.id,
                    "uom_id": self.product.uom_id.id,
                    "quantity": 2.0,
                    "online_price": 100.0,
                    "cost_price": 60.0,
                }
            )
        return colis

    def test_name_from_sequence(self):
        colis = self._create_colis()
        self.assertTrue(colis.name)
        self.assertTrue(colis.name.startswith("COLIS"))

    def test_amounts_based_on_online_price(self):
        colis = self._create_colis()
        line = colis.items_line_ids
        self.assertEqual(line.subtotal, 200.0)
        self.assertEqual(colis.amount_total, 200.0)
        self.assertEqual(colis.total_cost, 120.0)
        self.assertEqual(colis.gross_profit, 80.0)

    def test_reporting_defaults_to_active_colis(self):
        active = self._create_colis()
        archived = self._create_colis()
        lines = active.items_line_ids | archived.items_line_ids
        archived.action_archive()

        action = self.env.ref("system_confirmation.action_system_confirmation_reporting")
        self.assertTrue(safe_eval(action.context)["search_default_filter_active_colis"])
        search_view = self.env.ref("system_confirmation.view_system_confirmation_colis_lines_search")
        arch = etree.fromstring(search_view.arch_db)
        active_filter = arch.xpath("//filter[@name='filter_active_colis']")[0]
        archived_filter = arch.xpath("//filter[@name='filter_archived_colis']")[0]
        self.assertEqual(arch.xpath("//filter[@name='filter_colis_date']")[0].get("date"), "date")
        active_domain = safe_eval(active_filter.get("domain"))
        archived_domain = safe_eval(archived_filter.get("domain"))
        line_model = self.env["system.confirmation.colis.lines"]
        selected_lines = [("id", "in", lines.ids)]

        self.assertEqual(line_model.search(selected_lines + active_domain), active.items_line_ids)
        self.assertEqual(line_model.search(selected_lines + archived_domain), lines - active.items_line_ids)
        self.assertEqual(line_model.search_count(selected_lines), 2)
        totals = line_model.read_group(selected_lines + active_domain, ["subtotal:sum"], [])
        self.assertEqual(totals[0]["subtotal"], 200.0)

        archived.action_unarchive()
        self.assertEqual(line_model.search_count(selected_lines + active_domain), 2)

    def test_reporting_costs_are_counted_once_per_colis(self):
        first = self._create_colis()
        second = self._create_colis()
        first.write({"shipping_cost": 30.0, "amount": 600.0})
        second.write({"shipping_cost": 50.0, "amount": 1200.0})
        other_product = self.env["product.product"].create({"name": "Other product", "type": "consu"})
        line_model = self.env["system.confirmation.colis.lines"]
        extra_lines = line_model.create([
            {"colis_id": first.id, "product_id": self.product.id, "quantity": 1},
            {"colis_id": first.id, "product_id": other_product.id, "quantity": 1},
        ])
        domain = [("colis_id", "in", (first | second).ids)]

        def assert_cost_totals(expected_shipping, expected_packaging):
            totals = line_model.read_group(
                domain, ["shipping_cost:sum", "cost_of_packaging:sum"], ["product_id"], lazy=False
            )
            self.assertAlmostEqual(sum(row["shipping_cost"] for row in totals), expected_shipping)
            self.assertAlmostEqual(sum(row["cost_of_packaging"] for row in totals), expected_packaging)
            return totals

        grouped = assert_cost_totals(80.0, 60.0)
        self.assertEqual(len(grouped), 2)
        for parcel in (first | second):
            self.assertAlmostEqual(sum(parcel.items_line_ids.mapped("shipping_cost")), parcel.shipping_cost)
            self.assertAlmostEqual(sum(parcel.items_line_ids.mapped("cost_of_packaging")), parcel.cost_of_packaging)

        extra_lines[-1].unlink()
        assert_cost_totals(80.0, 60.0)
        first.shipping_cost = 40.0
        assert_cost_totals(90.0, 60.0)

    def test_reporting_cost_rounding_has_no_negative_shares(self):
        parcel = self._create_colis()
        rounding = parcel.currency_id.rounding
        parcel.shipping_cost = 2 * rounding
        self.env["system.confirmation.colis.lines"].create([
            {"colis_id": parcel.id, "product_id": self.product.id, "quantity": 1}
            for unused in range(3)
        ])
        shares = parcel.items_line_ids.mapped("shipping_cost")
        self.assertTrue(all(share >= 0 for share in shares))
        self.assertAlmostEqual(sum(shares), parcel.shipping_cost)

    def test_confirm_requires_lines(self):
        colis = self._create_colis(with_lines=False)
        colis.action_confirm()
        self.assertEqual(colis.colis_state, "draft")

    def test_confirm_requires_client(self):
        colis = self._create_colis()
        colis.client = False
        colis.action_confirm()
        self.assertEqual(colis.colis_state, "draft")

    def test_confirm_without_api_key_sets_error(self):
        colis = self._create_colis()
        colis.action_confirm()
        self.assertEqual(colis.colis_state, "draft")
        self.assertEqual(colis.api_state, "error")
        self.assertIn("API key", colis.message_api)
        self.assertFalse(colis.outbound_move_ids)

    def test_create_package_fills_line_defaults(self):
        colis = self.env["system.confirmation.colis"].create_package(
            {
                "client": self.client.id,
                "origin": self.origin.id,
                "items_line_ids": [
                    {"product_id": self.product.id, "quantity": 3.0}
                ],
            }
        )
        self.assertEqual(len(colis.items_line_ids), 1)
        line = colis.items_line_ids
        self.assertEqual(line.uom_id, self.product.uom_id)
        self.assertEqual(line.online_price, self.product.lst_price)
        self.assertEqual(line.quantity, 3.0)

    def test_create_package_accepts_default_code(self):
        self.product.default_code = "TEST-SKU"
        colis = self.env["system.confirmation.colis"].create_package(
            {
                "client": self.client.id,
                "origin": self.origin.id,
                "items_line_ids": [
                    {"default_code": "TEST-SKU", "quantity": 1.0}
                ],
            }
        )
        self.assertEqual(colis.items_line_ids.product_id, self.product)

    def test_confirm_creates_stock_move(self):
        colis = self._create_colis()
        move_before = self.env["stock.move"].search_count(
            [("origin", "=", colis.name)]
        )
        def fake_api(self, colis_rec):
            colis_rec.write({"api_state": "sent", "tracking_number": "TRK-STOCK"})
            return True

        with patch.object(type(colis), "create_package_api", fake_api):
            colis.action_confirm()
        move = self.env["stock.move"].search(
            [("origin", "=", colis.name)], limit=1
        )
        self.assertTrue(move, "Confirm should create a stock move")
        self.assertEqual(move.state, "done")
        self.assertEqual(move.location_id, self.stock_location)
        self.assertEqual(move.product_uom_qty, 2.0)
        self.assertEqual(
            self.env["stock.move"].search_count([("origin", "=", colis.name)]),
            move_before + 1,
        )

    def test_confirm_by_module_manager_without_stock_rights(self):
        """A user with only this module's Manager group can confirm a colis.

        The stock moves are internal logistics work, so they must not require
        the stock groups (regression test: AccessError on stock.location).
        """
        manager_group = self.env.ref(
            "system_confirmation.group_system_confirmation_manager"
        )
        user = self.env["res.users"].create(
            {
                "name": "SC Manager Only",
                "login": "sc_manager_only",
                "email": "sc_manager_only@example.com",
                "groups_id": [
                    (6, 0, [self.env.ref("base.group_user").id, manager_group.id])
                ],
            }
        )
        colis = self._create_colis()
        def fake_api(self, colis_rec):
            colis_rec.write({"api_state": "sent", "tracking_number": "TRK-MANAGER"})
            return True

        with patch.object(type(colis), "create_package_api", fake_api):
            colis.with_user(user).action_confirm()
        self.assertEqual(colis.colis_state, "confirmed")
        move = self.env["stock.move"].search(
            [("origin", "=", colis.name)], limit=1
        )
        self.assertTrue(
            move, "Manager without stock rights should still create a stock move"
        )
        self.assertEqual(move.state, "done")

    def test_print_is_read_only_preview(self):
        colis = self._create_colis()
        with patch.object(type(colis), "create_package_api") as mock_api:
            ready, failures = colis._prepare_labels_for_print()
        mock_api.assert_not_called()
        self.assertEqual(ready, colis)
        self.assertFalse(failures)
        self.assertEqual(colis.colis_state, "draft")
        self.assertFalse(colis.tracking_number)
        self.assertFalse(colis.outbound_move_ids)

    def test_print_does_not_require_api(self):
        colis = self._create_colis()
        ready, failures = colis._prepare_labels_for_print()
        self.assertEqual(ready, colis)
        self.assertFalse(failures)
        self.assertEqual(colis.api_state, "draft")

    def test_print_skips_cancelled_colis(self):
        colis = self._create_colis()
        colis.action_cancel()
        ready, failures = colis._prepare_labels_for_print()
        self.assertEqual(ready, colis)
        self.assertFalse(failures)

    def test_print_skips_colis_without_client(self):
        colis = self._create_colis()
        colis.client = False
        ready, failures = colis._prepare_labels_for_print()
        self.assertEqual(ready, colis)
        self.assertFalse(failures)

    def test_print_does_not_resend_sent_colis(self):
        colis = self._create_colis()
        colis.write(
            {
                "colis_state": "confirmed",
                "api_state": "sent",
                "tracking_number": "TRK-123",
            }
        )
        with patch.object(
            type(colis), "create_package_api"
        ) as mock_api:
            ready, failures = colis._prepare_labels_for_print()
        mock_api.assert_not_called()
        self.assertEqual(ready, colis)
        self.assertFalse(failures)
        self.assertFalse(colis.qrcode)
        self.assertFalse(colis.barcode)

    def test_report_values_supports_draft_preview(self):
        colis = self._create_colis()
        report = self.env["report.system_confirmation.report_colis_slip"]
        values = report._get_report_values([colis.id])
        self.assertEqual(values["docs"], colis)

    def test_cancel_sets_annulee(self):
        colis = self._create_colis()
        colis.action_cancel()
        self.assertEqual(colis.delivery_status, "Annulee")
        self.assertEqual(colis.label_color, "#616161")

    def test_confirm_without_api_key_keeps_delivery_status_empty(self):
        colis = self._create_colis()
        colis.action_confirm()
        self.assertFalse(colis.delivery_status)

    def test_confirm_with_mocked_api_keeps_delivery_status_empty(self):
        colis = self._create_colis()

        def fake_api(self, colis_rec):
            colis_rec.api_state = "sent"
            colis_rec.tracking_number = "TRK-555"
            colis_rec._generate_tracking_codes()
            return True

        with patch.object(type(colis), "create_package_api", fake_api):
            colis.action_confirm()
        self.assertFalse(colis.delivery_status)

    def test_track_package_sync_updates_delivery_status(self):
        colis = self._create_colis()
        colis.write(
            {
                "colis_state": "confirmed",
                "api_state": "sent",
                "tracking_number": "TRK-SYNC-1",
            }
        )
        package = self.env["track.package"].create(
            {"barcode": "TRK-SYNC-1", "state": "Encours"}
        )
        self.assertEqual(colis.delivery_status, "Encours")
        package.write({"state": "Livree"})
        self.assertEqual(colis.delivery_status, "Livree")
        self.assertEqual(colis.label_color, "#2e7d32")
        package.write({"state": "Payee"})
        self.assertEqual(colis.delivery_status, "Payee")
        self.assertEqual(colis.label_color, "#2e7d32")

    def test_track_package_created_paid_syncs_delivery_status(self):
        colis = self._create_colis()
        colis.write({"tracking_number": "TRK-SYNC-PAID"})
        self.env["track.package"].create(
            {"barcode": "TRK-SYNC-PAID", "state": "Payee"}
        )
        self.assertEqual(colis.delivery_status, "Payee")
        self.assertEqual(colis.label_color, "#2e7d32")

    def test_parcel_status_colors_cover_all_states(self):
        list_view = etree.fromstring(
            self.env.ref("system_confirmation.system_confirmation_colis_list").arch_db
        )
        kanban_view = etree.fromstring(
            self.env.ref("system_confirmation.system_confirmation_colis_kanban").arch_db
        )
        order_badges = list_view.xpath("//field[@name='colis_state'][@widget='badge']") + kanban_view.xpath(
            "//field[@name='colis_state'][@widget='badge']"
        )
        delivery_badges = list_view.xpath("//field[@name='delivery_status'][@widget='badge']") + kanban_view.xpath(
            "//field[@name='delivery_status'][@widget='badge']"
        )
        self.assertEqual(len(order_badges), 2)
        self.assertEqual(len(delivery_badges), 2)
        decorations = ("success", "info", "warning", "danger", "muted")

        def colors(node, values):
            return [
                color for color in decorations
                if node.get("decoration-" + color)
                and safe_eval(node.get("decoration-" + color), values)
            ]

        order_states = self.env["system.confirmation.colis"]._fields["colis_state"]._description_selection(self.env)
        for state, _label in order_states:
            values = {"colis_state": state, "delivery_status": False, "out_of_stock": False}
            badge_colors = [colors(badge, values) for badge in order_badges]
            self.assertEqual(len(badge_colors[0]), 1, state)
            self.assertEqual(badge_colors[0], badge_colors[1], state)
            self.assertEqual(colors(list_view, values), badge_colors[0], state)

        for state, expected in {
            "Encours": "warning", "Livree": "success", "Payee": "success",
            "Retournee": "danger", "Perdu": "danger", "Annulee": "muted",
        }.items():
            values = {"colis_state": "confirmed", "delivery_status": state, "out_of_stock": False}
            for badge in delivery_badges:
                self.assertEqual(colors(badge, values), [expected], state)
            self.assertEqual(colors(list_view, values), [expected], state)

    def test_track_package_import_syncs_colis(self):
        import base64
        from io import BytesIO

        from openpyxl import Workbook

        colis = self._create_colis()
        colis.write(
            {
                "colis_state": "confirmed",
                "api_state": "sent",
                "tracking_number": "TRK-XLS-1",
            }
        )
        self.env["track.package"].create(
            {"barcode": "TRK-XLS-1", "state": "Encours"}
        )
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["barcode"])
        sheet.append(["TRK-XLS-1"])
        buffer = BytesIO()
        workbook.save(buffer)
        wizard = self.env["track.package.import.excel"].create(
            {
                "file": base64.b64encode(buffer.getvalue()),
                "state": "Livree",
            }
        )
        wizard.action_import()
        self.assertEqual(colis.delivery_status, "Livree")

    def test_quicklivraison_status_map_covers_known_statuses(self):
        from odoo.addons.system_confirmation.models.system_confirmation_colis import (
            QUICKLIVRAISON_STATE_MAP,
        )

        expected = {
            "NEW_PARCEL": "confirmed",
            "WAITING_PICKUP": "WAITING_PICKUP",
            "PICKED_UP": "PICKED_UP",
            "PICKED_UP_RAMASSEUR": "PICKED_UP_RAMASSEUR",
            "RECEIVED": "Recu",
            "SENT": "Expedie",
            "EN_VOYAGE": "EN_VOYAGE",
            "DISTRIBUTION": "Distribution",
            "IN_PROGRESS": "Encours",
            "DELIVERED": "delivered",
            "POSTPONED": "POSTPONED",
            "PROGRAMMER": "PROGRAMMER",
            "LATER": "LATER",
            "NOANSWER": "NOANSWER",
            "DEUX": "DEUX",
            "TROIS": "TROIS",
            "QUATRE": "QUATRE",
            "UNREACHABLE": "UNREACHABLE",
            "INJOI": "INJOI",
            "ERR": "ERR",
            "OUT_OF_AREA": "OUT_OF_AREA",
            "REFUSE": "REFUSE",
            "CANCELED": "CANCELED",
            "ANN": "ANN",
            "CANCELED_BY_VENDEUR": "CANCELED_BY_VENDEUR",
            "CLIENT_INTERESE": "CLIENT_INTERESE",
            "WAIT_RELANCE": "WAIT_RELANCE",
            "RELENCE_NEW_CLIENT": "RELENCE_NEW_CLIENT",
            "RETURNED": "Retourne",
            "RETURNED_CLIENT": "RETURNED_CLIENT",
            "DOUBLE": "DOUBLE",
        }
        self.assertEqual(QUICKLIVRAISON_STATE_MAP, expected)
        selection = dict(self.env["system.confirmation.colis"]._fields["colis_state"].selection)
        self.assertTrue(set(expected.values()).issubset(selection))

    def _synced_colis(self, tracking="TRK-SYNC-API"):
        colis = self._create_colis()
        colis.write(
            {
                "colis_state": "confirmed",
                "api_state": "sent",
                "tracking_number": tracking,
            }
        )
        return colis

    def test_apply_status_updates_state_and_message(self):
        colis = self._synced_colis()
        result = {
            "status": "DISTRIBUTION",
            "situation": "INVOICED",
            "date_derniere_modification": "2025-01-03 16:00:00",
        }
        colis._apply_quicklivraison_status(result)
        self.assertEqual(colis.colis_state, "Distribution")
        self.assertIn("TRK-SYNC-API", colis.message_api)
        self.assertIn("En distribution", colis.message_api)
        self.assertIn("2025-01-03", colis.message_api)
        notes = colis.message_ids.filtered(
            lambda m: m.body and "En distribution" in m.body
        )
        self.assertTrue(notes, "State change should be posted to the chatter")

    def test_apply_status_same_state_no_message(self):
        colis = self._synced_colis()
        colis.colis_state = "Encours"
        message_before = colis.message_api
        colis._apply_quicklivraison_status({"status": "IN_PROGRESS"})
        self.assertEqual(colis.colis_state, "Encours")
        self.assertEqual(colis.message_api, message_before)

    def test_new_statuses_are_recorded_and_can_progress_to_return(self):
        colis = self._synced_colis()
        for status in ("WAITING_PICKUP", "REFUSE", "RETURNED_CLIENT"):
            with self.subTest(status=status):
                self.assertTrue(colis._apply_quicklivraison_status({"status": status}))
                self.assertEqual(colis.colis_state, status)
                self.assertIn(dict(colis._fields["colis_state"].selection)[status], colis.message_api)

        self.assertTrue(colis._apply_quicklivraison_status({"status": "RETURNED"}))
        self.assertEqual(colis.colis_state, "Retourne")

    def test_apply_unknown_status_keeps_state(self):
        colis = self._synced_colis()
        colis._apply_quicklivraison_status({"status": "SOMETHING_NEW"})
        self.assertEqual(colis.colis_state, "confirmed")
        self.assertIn("SOMETHING_NEW", colis.message_api)
        self.assertIn("inconnu", colis.message_api)

    def test_apply_date_livraison_fallback_marks_delivered(self):
        colis = self._synced_colis()
        colis._apply_quicklivraison_status(
            {"status": None, "date_livraison": "2025-01-03 15:45:00"}
        )
        self.assertEqual(colis.colis_state, "delivered")

    def test_apply_status_never_touches_delivery_status(self):
        colis = self._synced_colis()
        colis._apply_quicklivraison_status({"status": "DELIVERED"})
        self.assertEqual(colis.colis_state, "delivered")
        self.assertFalse(colis.delivery_status)

    def test_cron_sync_skips_final_states(self):
        delivered = self._synced_colis("TRK-FINAL-1")
        delivered.colis_state = "delivered"
        cancelled = self._synced_colis("TRK-FINAL-2")
        cancelled.colis_state = "cancelled"
        draft = self._synced_colis("TRK-FINAL-3")
        draft.colis_state = "draft"
        active = self._synced_colis("TRK-FINAL-4")

        called = []

        def fake_get(url, *args, **kwargs):
            called.append(url)
            raise requests.exceptions.RequestException("stop here")

        with patch(
            "odoo.addons.system_confirmation.models.system_confirmation_colis.requests.get",
            side_effect=fake_get,
        ):
            self.env["system.confirmation.colis"]._cron_sync_status_from_api()

        self.assertEqual(len(called), 1, "Only the active colis should be polled")
        self.assertIn("TRK-FINAL-4", called[0])

    def test_cron_sync_applies_status_via_api_call(self):
        colis = self._synced_colis("TRK-CRON-1")
        response = type("R", (), {})()
        response.ok = True
        response.status_code = 200
        response.text = "{}"

        def fake_json():
            return {"status": "SENT", "date_derniere_modification": "2025-01-02 08:30:00"}

        response.json = fake_json

        with patch(
            "odoo.addons.system_confirmation.models.system_confirmation_colis.requests.get",
            return_value=response,
        ):
            self.env["system.confirmation.colis"]._cron_sync_status_from_api()
        self.assertEqual(colis.colis_state, "Expedie")
        self.assertIn("Expédié", colis.message_api)

    def test_cron_continues_polling_return_request(self):
        colis = self._synced_colis("TRK-RETURN-REQUEST")
        colis.colis_state = "RETURNED_CLIENT"
        response = type("R", (), {})()
        response.ok = True
        response.status_code = 200
        response.text = "{}"
        response.json = lambda: {"status": "RETURNED"}

        with patch(
            "odoo.addons.system_confirmation.models.system_confirmation_colis.requests.get",
            return_value=response,
        ) as request:
            self.env["system.confirmation.colis"]._cron_sync_status_from_api()
        self.assertEqual(request.call_count, 1)
        self.assertIn("TRK-RETURN-REQUEST", request.call_args.args[0])
        self.assertEqual(colis.colis_state, "Retourne")

    def test_action_deliver_button_syncs(self):
        colis = self._synced_colis("TRK-BTN-1")
        response = type("R", (), {})()
        response.ok = True
        response.status_code = 200
        response.text = "{}"

        def fake_json():
            return {"status": "RETURNED"}

        response.json = fake_json

        with patch(
            "odoo.addons.system_confirmation.models.system_confirmation_colis.requests.get",
            return_value=response,
        ):
            colis.action_deliver()
        self.assertEqual(colis.colis_state, "Retourne")
        self.assertFalse(colis.delivery_status)

    def test_official_ticket_is_saved_and_retry_is_idempotent(self):
        colis = self._synced_colis("TRK-PDF-1")
        self.env["ir.config_parameter"].sudo().set_param(
            "system_confirmation.quicklivraison_api_key", "secret"
        )
        response = type("R", (), {})()
        response.ok = True
        response.status_code = 200
        response.content = b"%PDF-1.4\nmock"
        with patch(
            "odoo.addons.system_confirmation.models.parcel_workflow.requests.post",
            return_value=response,
        ) as request:
            colis._download_ticket_pdf()
            colis.action_retry_pdf()
        self.assertTrue(colis.ticket_attachment_id)
        self.assertEqual(colis.pdf_state, "done")
        self.assertEqual(request.call_count, 1)
        request.assert_called_once_with(
            "https://clients.quicklivraison.ma/api/parcels/eticket",
            headers={
                "Authorization": "Bearer secret",
                "Accept": "application/pdf",
                "Content-Type": "application/json",
            },
            json={"parcel_codes": ["TRK-PDF-1"]},
            timeout=30,
        )

    def test_invalid_ticket_keeps_confirmed_parcel(self):
        colis = self._synced_colis("TRK-PDF-2")
        self.env["ir.config_parameter"].sudo().set_param(
            "system_confirmation.quicklivraison_api_key", "secret"
        )
        response = type("R", (), {})()
        response.ok = True
        response.status_code = 200
        response.content = b"not a pdf"
        with patch(
            "odoo.addons.system_confirmation.models.parcel_workflow.requests.post",
            return_value=response,
        ):
            colis._download_ticket_pdf()
        self.assertEqual(colis.colis_state, "confirmed")
        self.assertEqual(colis.pdf_state, "error")
        self.assertFalse(colis.ticket_attachment_id)

    def _ticket_pdf(self, widths):
        writer = PdfFileWriter()
        for width in widths:
            writer.addBlankPage(width, 100)
        output = io.BytesIO()
        writer.write(output)
        return output.getvalue()

    def _attach_ticket(self, colis, pdf, prefix="ticket"):
        attachment = self.env["ir.attachment"].create({
            "name": "%s-%s.pdf" % (prefix, colis.tracking_number),
            "type": "binary",
            "datas": base64.b64encode(pdf),
            "mimetype": "application/pdf",
            "res_model": colis._name,
            "res_id": colis.id,
        })
        colis.sudo().write({"ticket_attachment_id": attachment.id, "pdf_state": "done"})

    def _downloaded_ticket_widths(self, action):
        download_id = int(action["url"].split("/")[4])
        data = self.env["system.confirmation.ticket.download"].browse(download_id).data
        reader = PdfFileReader(io.BytesIO(base64.b64decode(data)))
        return [float(reader.getPage(i).mediaBox.getWidth()) for i in range(reader.getNumPages())]

    def test_bulk_ticket_download_merges_only_available_selected_pdfs(self):
        first = self._synced_colis("PDF-FIRST")
        second = self._synced_colis("PDF-SECOND")
        missing = self._synced_colis("PDF-MISSING")
        self._attach_ticket(first, self._ticket_pdf([101, 102]))
        self._attach_ticket(second, self._ticket_pdf([201]))

        action = (first | missing | second).action_download_bulk_quicklivraison_pdf()
        self.assertEqual(self._downloaded_ticket_widths(action), [101, 102, 201])
        with self.assertRaises(UserError):
            missing.action_download_bulk_quicklivraison_pdf()

    def test_bulk_ticket_download_refetches_legacy_batch_for_selected_parcel(self):
        first = self._synced_colis("PDF-LEGACY-1")
        second = self._synced_colis("PDF-LEGACY-2")
        legacy_pdf = self._ticket_pdf([101, 201])
        self._attach_ticket(first, legacy_pdf, prefix="etickets")
        self._attach_ticket(second, legacy_pdf, prefix="etickets")
        self.env["ir.config_parameter"].sudo().set_param(
            "system_confirmation.quicklivraison_api_key", "secret"
        )
        response = type("R", (), {
            "ok": True, "status_code": 200, "content": self._ticket_pdf([101]),
        })()
        with patch(
            "odoo.addons.system_confirmation.models.parcel_workflow.requests.post",
            return_value=response,
        ) as request:
            action = first.action_download_bulk_quicklivraison_pdf()
        self.assertEqual(self._downloaded_ticket_widths(action), [101])
        request.assert_called_once_with(
            "https://clients.quicklivraison.ma/api/parcels/eticket",
            headers={
                "Authorization": "Bearer secret",
                "Accept": "application/pdf",
                "Content-Type": "application/json",
            },
            json={"parcel_codes": ["PDF-LEGACY-1"]},
            timeout=30,
        )

    def test_bulk_ticket_download_rejects_legacy_refetch_failure(self):
        colis = self._synced_colis("PDF-LEGACY-FAIL")
        self._attach_ticket(colis, self._ticket_pdf([101, 201]), prefix="etickets")
        with self.assertRaises(UserError):
            colis.action_download_bulk_quicklivraison_pdf()
        self.env["ir.config_parameter"].sudo().set_param(
            "system_confirmation.quicklivraison_api_key", "secret"
        )
        with patch(
            "odoo.addons.system_confirmation.models.parcel_workflow.requests.post",
            side_effect=requests.exceptions.Timeout("timeout"),
        ):
            with self.assertRaises(UserError):
                colis.action_download_bulk_quicklivraison_pdf()

    def test_bulk_ticket_download_rejects_invalid_pdf(self):
        colis = self._synced_colis("PDF-INVALID")
        self._attach_ticket(colis, b"not a pdf")
        with self.assertRaises(UserError):
            colis.action_download_bulk_quicklivraison_pdf()

    def test_bulk_confirmation_stores_individual_ticket_pdfs(self):
        first = self._synced_colis("PDF-INDIVIDUAL-1")
        second = self._synced_colis("PDF-INDIVIDUAL-2")
        self.env["ir.config_parameter"].sudo().set_param(
            "system_confirmation.quicklivraison_api_key", "secret"
        )
        responses = [type("R", (), {
            "ok": True, "status_code": 200, "content": self._ticket_pdf([width]),
        })() for width in (101, 201)]
        with patch(
            "odoo.addons.system_confirmation.models.parcel_workflow.requests.post",
            side_effect=responses,
        ) as request:
            (first | second)._download_ticket_pdf()
        self.assertEqual(request.call_count, 2)
        self.assertEqual(self._downloaded_ticket_widths(
            first.action_download_bulk_quicklivraison_pdf()), [101])
        self.assertEqual(self._downloaded_ticket_widths(
            second.action_download_bulk_quicklivraison_pdf()), [201])

    def test_partial_return_validation_is_idempotent(self):
        colis = self._create_colis()
        colis.write({"colis_state": "confirmed"})
        colis._create_stock_moves()
        operation = self.env["system.confirmation.return"].create(
            {
                "colis_id": colis.id,
                "line_ids": [
                    (0, 0, {"colis_line_id": colis.items_line_ids.id, "quantity": 1.0})
                ],
            }
        )
        operation.with_context(automatic_return=True).action_validate()
        move_count = len(operation.stock_move_ids)
        operation.with_context(automatic_return=True).action_validate()
        self.assertEqual(operation.state, "done")
        self.assertEqual(len(operation.stock_move_ids), move_count)
        self.assertEqual(colis.return_status, "partial")

    def test_tracking_return_only_reintegrates_once(self):
        colis = self._create_colis()
        colis.write(
            {
                "colis_state": "confirmed",
                "api_state": "sent",
                "tracking_number": "TRK-RETURN-ONCE",
            }
        )
        colis._create_stock_moves()
        package = self.env["track.package"].create(
            {"barcode": "TRK-RETURN-ONCE", "state": "Encours"}
        )
        package.write({"state": "Retournee"})
        returns = colis.return_ids.filtered(lambda ret: ret.source == "tracking")
        move_count = len(returns.stock_move_ids)
        package.write({"state": "Retournee"})
        self.assertEqual(len(colis.return_ids.filtered(lambda ret: ret.source == "tracking")), 1)
        self.assertEqual(len(returns.stock_move_ids), move_count)
        self.assertEqual(colis.return_status, "full")

    def test_archive_retries_stock_without_repeating_delete(self):
        colis = self._create_colis()
        colis.write(
            {
                "colis_state": "confirmed",
                "api_state": "sent",
                "tracking_number": "TRK-ARCHIVE-1",
            }
        )
        colis._create_stock_moves()
        self.env["ir.config_parameter"].sudo().set_param(
            "system_confirmation.quicklivraison_api_key", "secret"
        )
        response = type("R", (), {})()
        response.ok = True
        response.status_code = 200
        response.text = "{}"
        response.json = lambda: {"success": True}
        with patch(
            "odoo.addons.system_confirmation.models.parcel_workflow.requests.delete",
            return_value=response,
        ) as delete:
            colis.action_archive()
            colis.action_archive()
        self.assertTrue(colis.external_deleted)
        self.assertFalse(colis.active)
        self.assertEqual(colis.archive_state, "archived")
        self.assertEqual(delete.call_count, 1)
        self.assertEqual(colis.return_status, "full")

    def test_unconfirmed_archive_has_no_external_or_stock_operation(self):
        colis = self._create_colis()
        with patch(
            "odoo.addons.system_confirmation.models.parcel_workflow.requests.delete"
        ) as delete:
            colis.action_archive()
        delete.assert_not_called()
        self.assertFalse(colis.active)
        self.assertFalse(colis.return_ids)

    def test_tracking_return_does_not_add_stock_when_outbound_failed(self):
        colis = self._create_colis()
        colis.write(
            {
                "colis_state": "confirmed",
                "api_state": "sent",
                "tracking_number": "TRK-NO-STOCK",
                "stock_state": "error",
            }
        )
        self.env["track.package"].create(
            {"barcode": "TRK-NO-STOCK", "state": "Retournee"}
        )
        self.assertFalse(colis.return_ids)

    def test_confirmed_products_cannot_be_rewritten(self):
        colis = self._create_colis()
        colis.colis_state = "confirmed"
        with self.assertRaises(UserError):
            colis.items_line_ids.write({"quantity": 99.0})

    def test_user_cannot_rewrite_stock_workflow_state(self):
        user = self.env["res.users"].create(
            {
                "name": "Parcel User",
                "login": "parcel_workflow_user",
                "groups_id": [
                    (
                        6,
                        0,
                        [
                            self.env.ref("base.group_user").id,
                            self.env.ref(
                                "system_confirmation.group_system_confirmation_user"
                            ).id,
                        ],
                    )
                ],
            }
        )
        colis = self._create_colis()
        with self.assertRaises(AccessError):
            colis.with_user(user).write({"stock_state": "done"})
