import base64
from io import BytesIO
from odoo import fields, models, api, _
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_compare
import requests
import logging
from barcode import Code128
from barcode.writer import SVGWriter, ImageWriter
from markupsafe import Markup, escape
import qrcode

_logger = logging.getLogger(__name__)

# QuickLivraison getParcelDetails "status" -> system.confirmation.colis "colis_state"
QUICKLIVRAISON_STATE_MAP = {
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

# colis_state values that must not be polled anymore
QUICKLIVRAISON_FINAL_STATES = ("delivered", "Retourne", "cancelled", "draft")


class ColisConfirmation(models.Model):
    _name = "system.confirmation.colis"
    _description = "Colis Confirmation"
    _inherit = ["mail.thread", "mail.activity.mixin"]

    _order = "date desc, id desc"
    active = fields.Boolean("Active", default=True)
    _check_company_auto = True
    name = fields.Char(
        required=True,
        index=True,
        tracking=True,
        default=lambda self: self.env["ir.sequence"].next_by_code(
            "system.confirmation.colis"
        ),
    )
    amount = fields.Monetary("Amount", default=0.0)
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
        index=True,
    )
    currency_id = fields.Many2one(
        "res.currency",
        related="company_id.currency_id",
        store=True,
        readonly=True,
    )
    client = fields.Many2one("system.confirmation.client", "Client")

    message_api = fields.Char("Message de api (QuickLivraison)")
    cost_of_packaging = fields.Float(
        "Frais de l'emballage",
        compute="_calculer_frais_emballage",
        store=True,
        readonly=True,
    )
    origin = fields.Many2one(
        "system.confirmation.origin", required=True, ondelete="restrict"
    )

    items_line_ids = fields.One2many(
        "system.confirmation.colis.lines", "colis_id", string="Products", copy=True
    )
    barcode = fields.Binary("Barcode", readonly=True, copy=False, attachment=True)
    qrcode = fields.Binary("QR Code", readonly=True, copy=False, attachment=True)
    date = fields.Date(
        required=True,
        default=fields.Date.context_today,
        index=True,
        tracking=True,
    )

    amount_total = fields.Monetary(compute="_compute_amounts", store=True)
    total_cost = fields.Monetary(
        compute="_compute_amounts",
        store=True,
    )

    gross_profit = fields.Monetary(
        compute="_compute_amounts",
        store=True,
    )

    margin_percent = fields.Float(
        compute="_compute_amounts",
        store=True,
    )
    tracking_number = fields.Char("Tracking Number", readonly=True, copy=False)
    colis_state = fields.Selection(
        [
            ("draft", "Brouillon"),
            ("confirmed", "Confirmé"),
            ("Pending", "En attente"),
            ("WAITING_PICKUP", "Attente de ramassage"),
            ("PICKED_UP", "Ramassé"),
            ("PICKED_UP_RAMASSEUR", "Ramassé par le ramasseur"),
            ("Recu", "Reçu"),
            ("Expedie", "Expédié"),
            ("EN_VOYAGE", "En voyage"),
            ("Distribution", "En distribution"),
            ("Encours", "Encours"),
            ("delivered", "Livré"),
            ("POSTPONED", "Reporté"),
            ("PROGRAMMER", "Programmé"),
            ("LATER", "Plus tard"),
            ("NOANSWER", "Pas de réponse + SMS"),
            ("DEUX", "Deuxième appel sans réponse"),
            ("TROIS", "Troisième appel sans réponse"),
            ("QUATRE", "Quatrième appel sans réponse"),
            ("UNREACHABLE", "Injoignable"),
            ("INJOI", "Injoignable (suivi)"),
            ("ERR", "Numéro erroné"),
            ("OUT_OF_AREA", "Hors-zone"),
            ("REFUSE", "Refusé"),
            ("CANCELED", "Annulé par QuickLivraison"),
            ("ANN", "Annulé (équipe QuickLivraison)"),
            ("CANCELED_BY_VENDEUR", "Annulé par vendeur"),
            ("CLIENT_INTERESE", "Client intéressé"),
            ("WAIT_RELANCE", "Attente de relancer"),
            ("RELENCE_NEW_CLIENT", "Relancé nouveau client"),
            ("Retourne", "Retourné"),
            ("RETURNED_CLIENT", "Demande de retour"),
            ("DOUBLE", "Double"),
            ("cancelled", "Annulé"),
        ],
        string="Statut de commande",
        required=True,
        default="draft",
        copy=False,
        index=True,
        tracking=True,
    )
    client_adresse = fields.Char(
        string="Adresse",
        compute="_compute_client_snapshot",
        store=True,
        readonly=False,
    )
    client_qrcode = fields.Binary(
        string="Client QR Code",
        related="client.qrcode",
        attachment=True,
        store=True,
        readonly=True,
    )
    client_ville = fields.Many2one(
        "system.confirmation.villes",
        string="Ville",
        compute="_compute_client_snapshot",
        store=True,
        readonly=False,
    )

    @api.depends("client")
    def _compute_client_snapshot(self):
        for colis in self:
            colis.client_adresse = colis.client.adresse
            colis.client_ville = colis.client.ville

    shipping_cost = fields.Monetary("Shipping Cost", default=0.0)
    api_state = fields.Selection(
        [
            ("draft", "Brouillon"),
            ("sent", "Envoyé"),
            ("error", "Erreur"),
        ],
        string="Statut API",
        default="draft",
        copy=False,
    )
    out_of_stock = fields.Boolean("rupture de stock", default=False)
    delivery_status = fields.Selection(
        [
            ("Brouillon", "Brouillon"),
            ("Encours", "Encours"),
            ("Livree", "Livrée"),
            ("Payee", "Payée"),
            ("Retournee", "Retournée"),
            ("Annulee", "Annulée"),
            ("Perdu", "Perdu"),
        ],
        string="Statut de livraison",
        readonly=True,
        default="Brouillon",
        copy=False,
        help="Statut de livraison géré manuellement ou synchronisé depuis suivi_colis.",
    )
    label_color = fields.Char(
        compute="_compute_label_color",
        string="Label Color",
    )

    @api.depends("delivery_status", "colis_state")
    def _compute_label_color(self):
        color_map = {
            "Livree": "#2e7d32",
            "Payee": "#2e7d32",
            "delivered": "#2e7d32",
            "Encours": "#d99a00",
            "confirmed": "#1565c0",
            "PICKED_UP": "#1565c0",
            "PICKED_UP_RAMASSEUR": "#1565c0",
            "Recu": "#1565c0",
            "Expedie": "#1565c0",
            "EN_VOYAGE": "#1565c0",
            "Distribution": "#1565c0",
            "CLIENT_INTERESE": "#1565c0",
            "RELENCE_NEW_CLIENT": "#1565c0",
            "Pending": "#d99a00",
            "WAITING_PICKUP": "#d99a00",
            "POSTPONED": "#d99a00",
            "PROGRAMMER": "#d99a00",
            "LATER": "#d99a00",
            "NOANSWER": "#d99a00",
            "DEUX": "#d99a00",
            "TROIS": "#d99a00",
            "QUATRE": "#d99a00",
            "WAIT_RELANCE": "#d99a00",
            "RETURNED_CLIENT": "#d99a00",
            "Retournee": "#c62828",
            "Retourne": "#c62828",
            "UNREACHABLE": "#c62828",
            "INJOI": "#c62828",
            "ERR": "#c62828",
            "OUT_OF_AREA": "#c62828",
            "REFUSE": "#c62828",
            "DOUBLE": "#c62828",
            "draft": "#616161",
            "Annulee": "#616161",
            "cancelled": "#616161",
            "CANCELED": "#616161",
            "ANN": "#616161",
            "CANCELED_BY_VENDEUR": "#616161",
            "Perdu": "#c62828",
            "error": "#c62828",
        }
        for colis in self:
            key = (
                colis.delivery_status
                if colis.delivery_status not in (False, "Brouillon")
                else colis.colis_state
            )
            colis.label_color = color_map.get(key, "#616161")

    @api.depends("items_line_ids.subtotal", "items_line_ids.cost_total")
    def _compute_amounts(self):
        for order in self:
            amount_total = sum(order.items_line_ids.mapped("subtotal"))
            total_cost = sum(order.items_line_ids.mapped("cost_total"))
            gross_profit = amount_total - total_cost
            order.amount_total = amount_total
            order.total_cost = total_cost
            order.gross_profit = gross_profit
            order.margin_percent = (
                gross_profit / amount_total * 100.0 if amount_total else 0.0
            )

    def action_confirm(self):
        for order in self:
            if order.colis_state != "draft":
                continue

            if not order.client:
                raise UserError(
                    _("Please set a client before confirming colis %s.") % order.name
                )

            if not order.items_line_ids:
                raise UserError(
                    _("Add at least one product line before confirming colis %s.")
                    % order.name
                )

            # Validate stock and create moves only if all items are available.
            moves_created = order._create_stock_moves()

            if not moves_created:
                # Stop this colis here.
                # Do not confirm it or send it to QuickLivraison.
                continue

            order.colis_state = "confirmed"
            order.create_package_api(order)

            order.message_post(
                body=_("Colis confirmed and sent to QuickLivraison."),
                subtype_xmlid="mail.mt_note",
            )

        return True

    def _get_stock_location(self):
        location_id = (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("system_confirmation.location_id")
        )
        if not location_id:
            return self.env["stock.location"].sudo()
        try:
            return (
                self.env["stock.location"]
                .sudo()
                .browse(int(location_id))
                .exists()
            )
        except (TypeError, ValueError):
            return self.env["stock.location"].sudo()

    def _create_stock_moves(self):
        """Move stock only if every item has sufficient available quantity."""
        self.ensure_one()
        self.out_of_stock = False
        # Stock models (location/quant/move) are managed by this module's
        # logistics flow: run them as superuser so users without the stock
        # groups can still confirm. with_company resolves company-dependent
        # properties (e.g. property_stock_customer) in the colis's company.
        stock_env = self.sudo().with_company(self.company_id)
        src_location = self._get_stock_location()
        if not src_location:
            _logger.warning(
                "Colis %s confirmed without stock moves: "
                "system_confirmation.location_id is not set.",
                self.name,
            )
            return

        dst_location = stock_env.company_id.partner_id.property_stock_customer
        if not dst_location:
            dst_location = stock_env.env.ref("stock.stock_location_customers")

        Quant = stock_env.env["stock.quant"]

        # STEP 1: Validate ALL items before creating any stock move.
        missing_items = []

        for line in self.items_line_ids:
            if line.product_id.type != "consu":
                continue

            quants = Quant.search(
                [
                    ("location_id", "=", src_location.id),
                    ("product_id", "=", line.product_id.id),
                ]
            )

            quantity = sum(quants.mapped("quantity"))

            available_qty = self.env["uom.uom"]._compute_quantity(
                quantity,
                line.uom_id,
                rounding_method="HALF-UP",
            )

            if (
                float_compare(
                    available_qty,
                    line.quantity,
                    precision_rounding=line.uom_id.rounding,
                )
                < 0
            ):
                missing_items.append(
                    "<li><b>%s</b> — Demandé: %s, Available: %s</li>"
                    % (
                        escape(line.product_id.display_name),
                        line.quantity,
                        available_qty,
                    )
                )

        # If stock is insufficient, log the issue and stop.
        if missing_items:
            body = Markup(
                "<p><b>Transfert de stock annulé : stock insuffisant.</b></p>"
                "<p>Les produits suivants n'ont pas pu être transférés :</p>"
                "<ul>%s</ul>"
                "<p>Aucun mouvement de stock n'a été créé.</p>"
            ) % Markup("".join(missing_items))

            self.out_of_stock = True
            self.message_post(
                body=body,
                message_type="comment",
                subtype_xmlid="mail.mt_note",
            )
            return False
        # STEP 2: All items passed validation. Create the stock moves.
        MoveLine = stock_env.env["stock.move.line"]

        for line in self.items_line_ids:
            if line.product_id.type != "consu":
                continue

            move = stock_env.env["stock.move"].create(
                {
                    "name": "%s - %s"
                    % (
                        self.name,
                        line.product_id.display_name,
                    ),
                    "product_id": line.product_id.id,
                    "product_uom": line.uom_id.id,
                    "product_uom_qty": line.quantity,
                    "location_id": src_location.id,
                    "location_dest_id": dst_location.id,
                    "origin": self.name,
                }
            )

            MoveLine.create(
                {
                    "move_id": move.id,
                    "product_id": line.product_id.id,
                    "product_uom_id": line.uom_id.id,
                    "quantity": line.quantity,
                    "location_id": src_location.id,
                    "location_dest_id": dst_location.id,
                    "picked": True,
                }
            )

            move._action_done()
        return True

    def _apply_quicklivraison_status(self, result):
        """Map a getParcelDetails payload onto colis_state.

        Writes the change summary to message_api and the chatter only when
        the state actually changes. Unknown statuses keep the current state.
        Never touches delivery_status.
        """
        self.ensure_one()
        status = (result.get("status") or "").strip()
        date_livraison = result.get("date_livraison")
        new_state = QUICKLIVRAISON_STATE_MAP.get(status)
        if not new_state and date_livraison:
            new_state = "delivered"

        if not new_state:
            if status:
                message = _("Statut inconnu: %(status)s (état conservé).") % {
                    "status": status
                }
            else:
                message = _("Aucun statut reçu de QuickLivraison (état conservé).")
            # only log to chatter when the message changes, the cron runs often
            if self.message_api != message:
                self.message_api = message
                self.message_post(body=message, subtype_xmlid="mail.mt_note")
            return False

        if new_state == self.colis_state:
            return False

        old_label = dict(self._fields["colis_state"].selection).get(
            self.colis_state, self.colis_state
        )
        new_label = dict(self._fields["colis_state"].selection).get(
            new_state, new_state
        )
        date_value = result.get("date_derniere_modification") or date_livraison or ""
        message = "%s : %s → %s (%s)" % (
            self.tracking_number or self.name,
            old_label,
            new_label,
            date_value,
        )
        self.colis_state = new_state
        self.message_api = message
        self.message_post(body=message, subtype_xmlid="mail.mt_note")
        return True

    def _sync_from_quicklivraison(self):
        """Read the colis status from QuickLivraison and apply it."""
        api_url = "https://clients.quicklivraison.ma/api/getParcelDetails/"
        for order in self:
            if not order.tracking_number:
                order.message_api = _("No tracking number available.")
                continue
            try:
                headers = {
                    "Accept": "application/json",
                }
                response = requests.get(
                    api_url + str(order.tracking_number), headers=headers, timeout=15
                )
                _logger.info(
                    "QuickLivraison response: %s - %s",
                    response.status_code,
                    response.text,
                )
                try:
                    result = response.json()
                except ValueError:
                    result = {}
                if response.ok:
                    order._apply_quicklivraison_status(result)
                else:
                    order.message_api = _(
                        "Status: %(status)s, Situation: %(situation)s"
                    ) % {
                        "status": result.get("status"),
                        "situation": result.get("situation"),
                    }
            except requests.exceptions.RequestException as e:
                _logger.error("QuickLivraison API error: %s", e)
                order.message_api = _("Error calling the API: %s") % e
        return True

    def action_deliver(self):
        for order in self:
            if order.colis_state in QUICKLIVRAISON_FINAL_STATES:
                continue
            order._sync_from_quicklivraison()
        return True

    def action_cancel(self):
        for order in self:
            if order.colis_state not in ["draft", "confirmed"]:
                continue
            order.colis_state = "cancelled"
            order.delivery_status = "Annulee"
        return True

    def resend_to_api(self):
        for order in self:
            if order.api_state != "error":
                continue
            order.create_package_api(order)
        return True

    def create_package(self, package_data):
        client_id = package_data.get("client")
        items_line_ids = package_data.get("items_line_ids", [])
        origin_id = package_data.get("origin")
        date = package_data.get("date") or fields.Date.context_today(self)
        amount = package_data.get("amount", 0.0)
        shipping_cost = package_data.get("shipping_cost", 0.0)
        if not client_id or not origin_id:
            raise UserError(_("Client and origin are required to create a package."))

        values = {
            "client": client_id,
            "origin": origin_id,
            "date": date,
            "amount": amount,
            "shipping_cost": shipping_cost,
            "delivery_status": "Brouillon",
        }
        if package_data.get("client_adresse") is not None:
            values["client_adresse"] = package_data["client_adresse"]
        if package_data.get("client_ville"):
            values["client_ville"] = package_data["client_ville"]

        # Create the colis record
        colis = self.create(values)

        # Create the colis lines
        Line = self.env["system.confirmation.colis.lines"]
        Product = self.env["product.product"]
        for item in items_line_ids:
            product = Product.browse(item.get("product_id")).exists()
            if not product and item.get("default_code"):
                product = Product.search(
                    [("default_code", "=", item.get("default_code"))], limit=1
                )
            if not product:
                _logger.error(
                    "A valid product_id or default_code is required for each line item."
                )

            Line.create(
                {
                    "colis_id": colis.id,
                    "product_id": product.id,
                    "quantity": item.get("quantity", 1.0),
                    "uom_id": product.uom_id.id,
                    "cost_price": item.get("cost_price", product.standard_price),
                    "list_price": item.get("list_price", product.list_price),
                    "online_price": item.get(
                        "online_price", item.get("list_price", product.list_price)
                    ),
                    "product_reference": product.default_code,
                }
            )
        # if colis:
        #     colis.action_confirm()
        return colis

    def _generate_tracking_codes(self):
        self.ensure_one()
        value = self.tracking_number or self.name
        if not value:
            return

        # Generate QR code
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=4,
        )
        qr.add_data(value)
        qr.make(fit=True)

        qr_buffer = BytesIO()
        qr.make_image(
            fill_color="black",
            back_color="white",
        ).save(qr_buffer, format="PNG")

        self.qrcode = base64.b64encode(qr_buffer.getvalue())

        # Generate Code128 barcode
        barcode = Code128(value, writer=ImageWriter())

        image = barcode.render(
            {
                "module_width": 0.5,
                "module_height": 20,
                "quiet_zone": 1,
                "write_text": False,
                "dpi": 300,
            }
        )

        barcode_buffer = BytesIO()
        image.save(barcode_buffer, format="PNG")

        self.barcode = base64.b64encode(barcode_buffer.getvalue())

    def _prepare_labels_for_print(self):
        """Confirm/send colis as needed and return those with a printable label.

        Returns:
            tuple: (ready recordset, list of (name, reason) failures)
        """
        ready = self.browse()
        failures = []
        for colis in self:
            if colis.colis_state == "cancelled":
                failures.append((colis.name, _("Cancelled colis cannot be printed.")))
                continue
            if colis.colis_state == "draft":
                try:
                    colis.action_confirm()
                except UserError as e:
                    failures.append((colis.name, str(e)))
                    continue
            if colis.out_of_stock:
                failures.append(
                    (
                        colis.name,
                        _("Cannot print label: one or more products are out of stock."),
                    )
                )
                continue
            if colis.api_state == "error":
                try:
                    colis.create_package_api(colis)
                except UserError as e:
                    failures.append((colis.name, str(e)))
                    continue
            if colis.api_state != "sent" or not colis.tracking_number:
                failures.append(
                    (
                        colis.name,
                        colis.message_api
                        or _("Not sent to QuickLivraison, no tracking number."),
                    )
                )
                continue
            if not colis.qrcode or not colis.barcode:
                colis._generate_tracking_codes()
            ready |= colis
        return ready, failures

    def create_package_api(self, colis):
        icp = self.env["ir.config_parameter"].sudo()
        api_url = "https://clients.quicklivraison.ma/api/deliveries/store"
        api_key = icp.get_param("system_confirmation.quicklivraison_api_key")

        if colis.out_of_stock:
            return False
        if not api_key:
            colis.api_state = "error"
            colis.message_api = _(
                "QuickLivraison API key is not configured. "
                "Set the system_confirmation.quicklivraison_api_key system parameter."
            )
            return False
        if not colis.client:
            colis.api_state = "error"
            colis.message_api = _("Colis has no client set.")
            return False

        try:
            district_id = int(
                icp.get_param("system_confirmation.quicklivraison_district_id", "43")
            )
        except (TypeError, ValueError):
            district_id = 43

        try:
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            data = {
                "api_key": api_key,
                "district_id": colis.client.ville.code,
                "name": colis.client.name + " " + colis.origin.name,
                "amount": colis.amount,
                "phone": colis.client.telephone,
                "address": colis.client.adresse,
                "prd_name": ", ".join(
                    f"{line.product_id.default_code}-{line.product_id.name} ?{line.quantity}?"
                    for line in colis.items_line_ids
                ),
                "open" : True,
                "qte_prd": 1,
                "fragile": True,
                "weight_less_6kg": False,
                "specific_weight": 8,
            }
            response = requests.post(api_url, headers=headers, json=data, timeout=15)
            _logger.info(
                "QuickLivraison response: %s - %s", response.status_code, response.text
            )
            try:
                result = response.json()
            except ValueError:
                result = {}
            if response.ok and result.get("success"):
                tracking_number = result.get("tracking_number")
                if not tracking_number:
                    colis.api_state = "error"
                    colis.message_api = _(
                        "QuickLivraison created no usable parcel code."
                    )
                else:
                    colis.write(
                        {
                            "api_state": "sent",
                            "message_api": _("Data sent successfully to the API."),
                            "tracking_number": tracking_number,
                        }
                    )
                    try:
                        colis._generate_tracking_codes()
                    except Exception:
                        # Local preview assets must not invalidate a remote creation.
                        _logger.exception(
                            "Could not generate local tracking images for %s", colis.name
                        )
            else:
                colis.api_state = "error"
                colis.message_api = _(
                    "Failed to send data to the API. Status code: %(status)s, Response: %(response)s"
                ) % {"status": response.status_code, "response": response.text}
        except requests.exceptions.RequestException as e:
            _logger.error("QuickLivraison API error: %s", e)
            colis.api_state = "error"
            colis.message_api = _("Error calling the API: %s") % e

        colis.message_post(
            body=colis.message_api,
            subtype_xmlid="mail.mt_note",
        )
        return colis.api_state == "sent"

    @api.model
    def _cron_retry_failed_api(self):
        colis_to_retry = self.search(
            [("api_state", "=", "error"), ("colis_state", "=", "confirmed")],
            limit=50,
        )
        for colis in colis_to_retry:
            colis.create_package_api(colis)
        return True

    @api.model
    def _cron_sync_status_from_api(self):
        """Poll QuickLivraison for sent colis and apply status changes."""
        colis_to_sync = self.search(
            [
                ("api_state", "=", "sent"),
                ("tracking_number", "!=", False),
                ("colis_state", "not in", list(QUICKLIVRAISON_FINAL_STATES)),
            ],
            limit=50,
        )
        colis_to_sync._sync_from_quicklivraison()
        return True

    def action_print_label(self):
        self.ensure_one()
        return self.env.ref(
            "system_confirmation.action_report_colis_slip"
        ).report_action(self)

    @api.depends("amount")
    def _calculer_frais_emballage(self):
        # =IF(G31<500, 15, IF(G31<=1000, 25, IF(G31<=2000, 35, 100)))
        for record in self:
            if record.amount < 500:
                record.cost_of_packaging = 15
            elif record.amount <= 1000:
                record.cost_of_packaging = 25
            elif record.amount <= 2000:
                record.cost_of_packaging = 35
            else:
                record.cost_of_packaging = 100


class ColisLinesConfirmationSystem(models.Model):
    _name = "system.confirmation.colis.lines"
    _description = "Colis Line"

    colis_id = fields.Many2one(
        "system.confirmation.colis", required=True, index=True, ondelete="cascade"
    )
    origin = fields.Many2one(related="colis_id.origin", store=True)
    client = fields.Many2one(related="colis_id.client", store=True)
    date = fields.Date(related="colis_id.date", store=True)
    colis_state = fields.Selection(related="colis_id.colis_state", store=True)
    # Shares of the colis-level costs: summing the lines of a colis always
    # gives back the colis value (see _compute_colis_shared_costs).
    shipping_cost = fields.Monetary(
        "Shipping Cost",
        compute="_compute_colis_shared_costs",
        store=True,
    )
    cost_of_packaging = fields.Float(
        "Frais de l'emballage",
        compute="_compute_colis_shared_costs",
        store=True,
    )

    product_id = fields.Many2one(
        "product.product", required=True, index=True, ondelete="restrict"
    )

    quantity = fields.Float(
        required=True, default=1.0, string="Quantite", digits=(16, 3)
    )
    list_price = fields.Monetary(required=True, default=0.0)
    cost_price = fields.Float(required=True, default=0.0, digits="Product Price")
    product_reference = fields.Char("default_code")
    online_price = fields.Float(required=True, default=0.0, digits="Product Price")
    subtotal = fields.Monetary(compute="_compute_amounts", store=True)
    cost_total = fields.Monetary(
        compute="_compute_amounts",
        store=True,
        groups="system_confirmation.group_system_confirmation_user",
    )

    profit = fields.Monetary(
        compute="_compute_amounts",
        store=True,
        groups="system_confirmation.group_system_confirmation_user",
    )

    margin_percent = fields.Float(
        compute="_compute_amounts",
        store=True,
        groups="system_confirmation.group_system_confirmation_user",
    )
    currency_id = fields.Many2one(
        "res.currency", related="colis_id.currency_id", store=True
    )
    uom_id = fields.Many2one(
        "uom.uom",
        string="Unit of Measure",
        required=True,
        ondelete="restrict",
    )

    @api.model_create_multi
    def create(self, vals_list):
        Product = self.env["product.product"]
        for vals in vals_list:
            product = Product.browse(vals.get("product_id")).exists()
            if product:
                vals.setdefault("uom_id", product.uom_id.id)
                vals.setdefault("product_reference", product.default_code)
                vals.setdefault("cost_price", product.standard_price)
                vals.setdefault("list_price", product.lst_price)
                vals.setdefault(
                    "online_price",
                    vals.get("list_price", product.lst_price),
                )
        return super().create(vals_list)

    @api.depends("quantity", "cost_price", "online_price")
    def _compute_amounts(self):
        for line in self:
            subtotal = line.currency_id.round(line.quantity * line.online_price)
            cost_total = line.currency_id.round(line.quantity * line.cost_price)
            profit = subtotal - cost_total
            line.subtotal = subtotal
            line.cost_total = cost_total
            line.profit = profit
            line.margin_percent = profit / subtotal * 100.0 if subtotal else 0.0

    @api.depends(
        "colis_id.shipping_cost",
        "colis_id.cost_of_packaging",
        "colis_id.items_line_ids",
    )
    def _compute_colis_shared_costs(self):
        """Split the colis-level costs evenly over the colis's lines.

        Summing the lines of a colis always gives back the colis value, so
        pivot/graph totals stay correct whatever the grouping: all the lines
        of a colis share the same origin, date, client and state, so a colis
        can never be split across two groups of those. Cumulative rounding
        distributes the remainder without giving a line a negative share.
        """
        for line in self:
            colis = line.colis_id
            siblings = colis.items_line_ids
            count = len(siblings)
            position = next(
                (index for index, sibling in enumerate(siblings) if sibling == line),
                None,
            )
            currency = colis.currency_id or self.env.company.currency_id
            shares = []
            for total in (colis.shipping_cost, colis.cost_of_packaging):
                total = total or 0.0
                if position is None:
                    share = 0.0
                else:
                    before = currency.round(total * position / count)
                    after = currency.round(total * (position + 1) / count)
                    share = currency.round(after - before)
                shares.append(share)
            line.shipping_cost = shares[0]
            line.cost_of_packaging = shares[1]

    @api.constrains("quantity", "uom_id")
    def _check_quantity(self):
        for line in self:
            if (
                float_compare(
                    line.quantity,
                    0.0,
                    precision_rounding=line.uom_id.rounding,
                )
                <= 0
            ):
                raise ValidationError(_("Colis line quantities must be positive."))

    @api.onchange("product_id")
    def _onchange_product_id(self):
        for line in self:
            if not line.product_id:
                continue
            product = line.product_id
            line.uom_id = product.uom_id
            line.cost_price = product.standard_price
            line.list_price = product.list_price
            line.online_price = product.list_price
            line.product_reference = product.default_code

    _sql_constraints = [
        ("quantity_positive", "check(quantity > 0)", "Quantity must be positive."),
    ]


class SystemConfirmationReport(models.AbstractModel):
    _name = "report.system_confirmation.report_colis_slip"
    _description = "Colis Report"

    @api.model
    def _get_report_values(self, docids, data=None):
        logo = self.env.company.sudo().system_confirmation_quicklivraison_logo
        # _logger.info(f"_docids: {docids}")
        colis = self.env["system.confirmation.colis"].browse(docids)
        docs, failures = colis._prepare_labels_for_print()
        # _logger.info(f"docs: {docs} and failures: {failures}")

        # client info
        # client = self.env["system.confirmation.client"].browse(2)

        # _logger.info("Current:", repr(client.name))

        # try:
        # recovered = client.name.encode("latin-1").decode("utf-8")
        #     # _logger.info("Recovered:", recovered)
        # except (UnicodeEncodeError, UnicodeDecodeError) as e:
        #     _logger.info("Cannot recover with this conversion:", e)

        if failures:
            message = (
                _("These colis were not printed:")
                + "\n"
                + "\n".join("- %s: %s" % (name, reason) for name, reason in failures)
            )
            # _logger.warning("Label printing failures: %s", message)
            self.env.user._bus_send(
                "simple_notification",
                {"type": "warning", "message": message, "sticky": True},
            )
        # _logger.info("Report docids: %s", docids)

        # for record in docs:
        #     _logger.info(
        #         "Colis %s | client=%r | address=%r | city=%r",
        #         record.name,
        #         record.client.name,
        #         record.client_adresse,
        #         record.client_ville.name if record.client_ville else None,
        #     )
        if not docs:
            raise UserError(
                _("None of the selected colis could be prepared for printing.")
                + "\n\n"
                + "\n".join("- %s: %s" % (name, reason) for name, reason in failures)
            )
        return {
            "doc_ids": docs.ids,
            "doc_model": "system.confirmation.colis",
            "docs": docs,
            "logo": logo,
        }
