import base64
import logging

import requests

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import float_compare, float_is_zero
from odoo.tools.pdf import merge_pdf


_logger = logging.getLogger(__name__)


class TicketDownload(models.TransientModel):
    _name = "system.confirmation.ticket.download"
    _description = "Temporary QuickLivraison PDF Download"

    data = fields.Binary(required=True, attachment=False)


class ColisWorkflow(models.Model):
    _inherit = "system.confirmation.colis"

    tracking_number = fields.Char(index=True)
    delivery_status = fields.Selection(default=False)
    stock_state = fields.Selection(
        [("pending", "Pending"), ("done", "Done"), ("error", "Error")],
        default="pending",
        copy=False,
        readonly=True,
        tracking=True,
    )
    stock_error = fields.Text(copy=False, readonly=True)
    outbound_move_ids = fields.Many2many(
        "stock.move",
        "system_confirmation_colis_out_move_rel",
        "colis_id",
        "move_id",
        string="Outbound Stock Moves",
        copy=False,
        readonly=True,
    )
    ticket_attachment_id = fields.Many2one(
        "ir.attachment", copy=False, readonly=True, ondelete="set null"
    )
    pdf_state = fields.Selection(
        [("pending", "Pending"), ("done", "Available"), ("error", "Error")],
        default="pending",
        copy=False,
        readonly=True,
        tracking=True,
    )
    pdf_error = fields.Text(copy=False, readonly=True)
    external_deleted = fields.Boolean(copy=False, readonly=True, tracking=True)
    external_deleted_at = fields.Datetime(copy=False, readonly=True)
    archive_state = fields.Selection(
        [
            ("none", "Not Archived"),
            ("external_deleted", "External Parcel Deleted"),
            ("stock_error", "Stock Reversal Error"),
            ("archived", "Archived"),
        ],
        default="none",
        copy=False,
        readonly=True,
        tracking=True,
    )
    archive_error = fields.Text(copy=False, readonly=True)
    workflow_lock_at = fields.Datetime(copy=False, readonly=True)
    return_ids = fields.One2many(
        "system.confirmation.return", "colis_id", string="Returns", readonly=True
    )
    return_status = fields.Selection(
        [("none", "No Return"), ("partial", "Partially Returned"), ("full", "Fully Returned")],
        compute="_compute_return_status",
    )

    def write(self, vals):
        protected = {
            "active",
            "stock_state",
            "stock_error",
            "outbound_move_ids",
            "ticket_attachment_id",
            "pdf_state",
            "pdf_error",
            "external_deleted",
            "external_deleted_at",
            "archive_state",
            "archive_error",
            "workflow_lock_at",
        }
        if protected.intersection(vals) and not self.env.su:
            raise AccessError(_("Workflow fields can only be changed through parcel actions."))
        return super().write(vals)

    @api.depends("items_line_ids.quantity", "return_ids.state", "return_ids.line_ids.quantity")
    def _compute_return_status(self):
        for colis in self:
            outstanding = colis._get_outstanding_quantities()
            shipped = bool(colis.items_line_ids.filtered(lambda line: line.product_id.type == "consu"))
            returned = any(
                ret.state == "done" and ret.line_ids for ret in colis.return_ids
            )
            if shipped and all(
                float_is_zero(qty, precision_rounding=line.uom_id.rounding)
                for line, qty in outstanding.items()
            ):
                colis.return_status = "full"
            elif returned:
                colis.return_status = "partial"
            else:
                colis.return_status = "none"

    def _validate_for_confirmation(self):
        self.ensure_one()
        if not self.client:
            raise UserError(_("Please set a client before confirming colis %s.") % self.name)
        if not self.items_line_ids:
            raise UserError(_("Add at least one product line before confirming colis %s.") % self.name)
        if self.out_of_stock:
            raise UserError(_("Colis %s is marked out of stock.") % self.name)
        self._check_stock_availability()

    def _lock_workflow(self):
        """Serialize state-changing operations on one parcel using an ORM write."""
        self.ensure_one()
        self.sudo().write({"workflow_lock_at": fields.Datetime.now()})
        self.invalidate_recordset(
            ["colis_state", "api_state", "tracking_number", "external_deleted", "active"]
        )

    def _check_stock_availability(self):
        """Validate aggregate demand without creating stock moves."""
        self.ensure_one()
        location = self._get_stock_location()
        if not location:
            raise UserError(_("Configure the stock location before confirming parcels."))

        demand = {}
        for line in self.items_line_ids.filtered(lambda item: item.product_id.type == "consu"):
            qty = line.uom_id._compute_quantity(line.quantity, line.product_id.uom_id)
            demand[line.product_id] = demand.get(line.product_id, 0.0) + qty

        quant_model = self.env["stock.quant"].sudo().with_company(self.company_id)
        missing = []
        for product, required in demand.items():
            available = quant_model._get_available_quantity(product, location, strict=True)
            if float_compare(available, required, precision_rounding=product.uom_id.rounding) < 0:
                missing.append("%s (%s / %s)" % (product.display_name, available, required))
        if missing:
            self.out_of_stock = True
            raise UserError(
                _("Insufficient stock for %(colis)s: %(products)s")
                % {"colis": self.name, "products": ", ".join(missing)}
            )
        return True

    def create_package_api(self, colis):
        colis.ensure_one()
        if colis.api_state == "sent" and colis.tracking_number:
            return True
        result = super().create_package_api(colis)
        if result and not colis.tracking_number:
            colis.write(
                {
                    "api_state": "error",
                    "message_api": _("QuickLivraison created no usable parcel code."),
                }
            )
            return False
        return result

    def action_confirm(self):
        confirmed = self.browse()
        failures = []
        for order in self:
            order._lock_workflow()
            if order.colis_state != "draft":
                continue
            try:
                order._validate_for_confirmation()
            except UserError as error:
                failures.append((order.name, str(error)))
                continue

            if not order.create_package_api(order):
                failures.append((order.name, order.message_api or _("QuickLivraison creation failed.")))
                continue

            order.sudo().write(
                {"colis_state": "confirmed", "stock_state": "pending", "stock_error": False}
            )
            try:
                with self.env.cr.savepoint():
                    order._create_stock_moves()
            except Exception as error:
                _logger.exception("Stock processing failed for colis %s", order.name)
                order.sudo().write({"stock_state": "error", "stock_error": str(error)})
                order.message_post(
                    body=_("QuickLivraison parcel exists, but stock processing failed: %s") % error,
                    subtype_xmlid="mail.mt_note",
                )
                failures.append((order.name, order.stock_error))
                confirmed |= order
                continue
            confirmed |= order
            order.message_post(
                body=_("Colis confirmed in QuickLivraison and stock processed."),
                subtype_xmlid="mail.mt_note",
            )

        if confirmed:
            confirmed._download_ticket_pdf()
        if failures:
            self.env.user._bus_send(
                "simple_notification",
                {
                    "type": "warning",
                    "message": "\n".join("%s: %s" % failure for failure in failures),
                    "sticky": True,
                },
            )
        return True

    def _create_stock_moves(self):
        self.ensure_one()
        if self.stock_state == "done":
            return self.outbound_move_ids or True
        existing_moves = self.outbound_move_ids.filtered(lambda move: move.state == "done")
        if existing_moves:
            self.sudo().write({"stock_state": "done", "stock_error": False})
            return existing_moves
        self._check_stock_availability()
        stock_env = self.sudo().with_company(self.company_id)
        source = self._get_stock_location()
        destination = stock_env.company_id.partner_id.property_stock_customer
        destination = destination or stock_env.env.ref("stock.stock_location_customers")
        moves = stock_env.env["stock.move"]
        for line in self.items_line_ids.filtered(lambda item: item.product_id.type == "consu"):
            move = stock_env.env["stock.move"].create(
                {
                    "name": "%s - %s" % (self.name, line.product_id.display_name),
                    "product_id": line.product_id.id,
                    "product_uom": line.uom_id.id,
                    "product_uom_qty": line.quantity,
                    "location_id": source.id,
                    "location_dest_id": destination.id,
                    "origin": self.name,
                }
            )
            stock_env.env["stock.move.line"].create(
                {
                    "move_id": move.id,
                    "product_id": line.product_id.id,
                    "product_uom_id": line.uom_id.id,
                    "quantity": line.quantity,
                    "location_id": source.id,
                    "location_dest_id": destination.id,
                    "picked": True,
                }
            )
            move._action_done()
            moves |= move
        self.sudo().write(
            {
                "outbound_move_ids": [(6, 0, moves.ids)],
                "stock_state": "done",
                "stock_error": False,
                "out_of_stock": False,
            }
        )
        return moves or True

    def action_retry_stock(self):
        if not self.env.su and not self.env.user.has_group(
            "system_confirmation.group_system_confirmation_manager"
        ):
            raise AccessError(_("Only parcel managers can retry stock processing."))
        for colis in self.filtered(lambda rec: rec.stock_state == "error"):
            try:
                with self.env.cr.savepoint():
                    colis._create_stock_moves()
            except Exception as error:
                colis.sudo().write({"stock_error": str(error), "stock_state": "error"})
        return True

    def resend_to_api(self):
        raise UserError(_("Use Confirm to create a QuickLivraison parcel."))

    @api.model
    def _cron_retry_failed_api(self):
        return True

    def _download_ticket_pdf(self):
        parcels = self.filtered(
            lambda rec: rec.tracking_number and rec.api_state == "sent" and not rec.ticket_attachment_id
        )
        if not parcels:
            return True
        # Each attachment must belong to one parcel, even after bulk confirmation.
        if len(parcels) > 1:
            results = [parcel._download_ticket_pdf() for parcel in parcels]
            return all(results)
        api_key = self.env["ir.config_parameter"].sudo().get_param(
            "system_confirmation.quicklivraison_api_key"
        )
        if not api_key:
            parcels.sudo().write(
                {"pdf_state": "error", "pdf_error": _("QuickLivraison API key is not configured.")}
            )
            return False
        payload = {"parcel_codes": [parcels.tracking_number]}
        try:
            response = requests.post(
                "https://clients.quicklivraison.ma/api/parcels/eticket",
                headers={
                    "Authorization": "Bearer %s" % api_key,
                    "Accept": "application/pdf",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=30,
            )
            if not response.ok or not response.content.startswith(b"%PDF-"):
                raise UserError(
                    _("Invalid PDF response (HTTP %s).") % response.status_code
                )
            for parcel in parcels:
                attachment = self.env["ir.attachment"].create(
                    {
                        "name": "ticket-%s.pdf" % parcel.tracking_number,
                        "type": "binary",
                        "datas": base64.b64encode(response.content),
                        "mimetype": "application/pdf",
                        "res_model": parcel._name,
                        "res_id": parcel.id,
                    }
                )
                parcel.sudo().write(
                    {"ticket_attachment_id": attachment.id, "pdf_state": "done", "pdf_error": False}
                )
        except (requests.exceptions.RequestException, UserError) as error:
            _logger.warning("QuickLivraison ticket retrieval failed: %s", error)
            parcels.sudo().write({"pdf_state": "error", "pdf_error": str(error)})
            for parcel in parcels:
                parcel.message_post(
                    body=_("Official ticket download failed: %s") % error,
                    subtype_xmlid="mail.mt_note",
                )
            return False
        return True

    def action_retry_pdf(self):
        self.filtered(lambda rec: not rec.ticket_attachment_id)._download_ticket_pdf()
        return True

    def action_download_pdf(self):
        self.ensure_one()
        if not self.ticket_attachment_id:
            raise UserError(_("No official QuickLivraison ticket is available."))
        return {
            "type": "ir.actions.act_url",
            "url": "/web/content/%s?download=true" % self.ticket_attachment_id.id,
            "target": "self",
        }

    def action_download_bulk_quicklivraison_pdf(self):
        self.check_access("read")
        parcels = self.filtered(lambda parcel: parcel.ticket_attachment_id)
        if not parcels:
            raise UserError(_("None of the selected parcels has a QuickLivraison PDF."))

        documents = []
        for parcel in parcels:
            attachment = parcel.ticket_attachment_id
            if attachment.name.startswith("etickets-"):
                # Older bulk confirmations saved the entire batch on each parcel.
                # Request this parcel's ticket rather than leaking the other pages.
                api_key = self.env["ir.config_parameter"].sudo().get_param(
                    "system_confirmation.quicklivraison_api_key"
                )
                if not api_key or not parcel.tracking_number:
                    raise UserError(_("Cannot retrieve the individual QuickLivraison PDF for %s. Check its tracking number and API key.") % parcel.display_name)
                try:
                    response = requests.post(
                        "https://clients.quicklivraison.ma/api/parcels/eticket",
                        headers={
                            "Authorization": "Bearer %s" % api_key,
                            "Accept": "application/pdf",
                            "Content-Type": "application/json",
                        },
                        json={"parcel_codes": [parcel.tracking_number]},
                        timeout=30,
                    )
                    if not response.ok or not response.content.startswith(b"%PDF-"):
                        raise UserError(_("Invalid PDF response (HTTP %s).") % response.status_code)
                    documents.append(response.content)
                except (requests.exceptions.RequestException, UserError) as error:
                    raise UserError(_("Could not retrieve the QuickLivraison PDF for %(parcel)s: %(error)s") % {
                        "parcel": parcel.display_name, "error": str(error),
                    }) from error
            else:
                try:
                    documents.append(base64.b64decode(
                        attachment.with_context(bin_size=False).datas or b"", validate=True
                    ))
                except (ValueError, TypeError) as error:
                    raise UserError(_("The QuickLivraison PDF for %s is invalid.") % parcel.display_name) from error

        try:
            pdf = merge_pdf(documents)
        except Exception as error:
            _logger.warning("Could not merge QuickLivraison PDFs: %s", error)
            raise UserError(_("One of the selected QuickLivraison PDFs is invalid or cannot be merged.")) from error
        download = self.env["system.confirmation.ticket.download"].create({
            "data": base64.b64encode(pdf),
        })
        return {
            "type": "ir.actions.act_url",
            "url": "/web/content/system.confirmation.ticket.download/%s/data/quicklivraison-tickets.pdf?download=true" % download.id,
            "target": "self",
        }

    def _prepare_labels_for_print(self):
        """The QWeb report is a read-only preparation preview."""
        return self, []

    def action_register_return(self):
        self.ensure_one()
        if self.colis_state == "draft" or self.stock_state != "done":
            raise UserError(_("Only stock-processed parcels can be returned."))
        return {
            "name": _("Register Return"),
            "type": "ir.actions.act_window",
            "res_model": "system.confirmation.return.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_colis_id": self.id},
        }

    def _get_outstanding_quantities(self):
        self.ensure_one()
        returned = {}
        done_lines = self.return_ids.filtered(lambda ret: ret.state == "done").mapped("line_ids")
        for return_line in done_lines:
            returned[return_line.colis_line_id] = returned.get(return_line.colis_line_id, 0.0) + return_line.quantity
        return {
            line: max(line.quantity - returned.get(line, 0.0), 0.0)
            for line in self.items_line_ids.filtered(lambda item: item.product_id.type == "consu")
        }

    def _create_automatic_return(self, source, tracking_package=None):
        self.ensure_one()
        shipped_lines = self.items_line_ids.filtered(lambda line: line.product_id.type == "consu")
        if self.stock_state != "done" or (shipped_lines and not self.outbound_move_ids):
            self.message_post(
                body=_("No stock return was created because outbound stock was not completed."),
                subtype_xmlid="mail.mt_note",
            )
            return True
        operation = self.return_ids.filtered(lambda ret: ret.source == source and ret.state != "done")[:1]
        if not operation:
            values = {
                "colis_id": self.id,
                "source": source,
                "tracking_package_id": tracking_package.id if tracking_package else False,
                "line_ids": [
                    (0, 0, {"colis_line_id": line.id, "quantity": quantity})
                    for line, quantity in self._get_outstanding_quantities().items()
                    if not float_is_zero(quantity, precision_rounding=line.uom_id.rounding)
                ],
            }
            if not values["line_ids"]:
                return True
            operation = self.env["system.confirmation.return"].create(values)
        return operation.sudo().with_context(system_auto_return=True).action_validate()

    def _apply_quicklivraison_status(self, result):
        previous = {record.id: record.colis_state for record in self}
        changed = super()._apply_quicklivraison_status(result)
        for colis in self.filtered(
            lambda rec: previous.get(rec.id) != "Retourne" and rec.colis_state == "Retourne"
        ):
            colis._create_automatic_return("tracking")
        return changed

    def _delete_quicklivraison_parcel(self):
        self.ensure_one()
        if self.external_deleted:
            return True
        if not self.tracking_number:
            raise UserError(_("Confirmed parcel has no QuickLivraison parcel code."))
        api_key = self.env["ir.config_parameter"].sudo().get_param(
            "system_confirmation.quicklivraison_api_key"
        )
        if not api_key:
            raise UserError(_("QuickLivraison API key is not configured."))
        try:
            response = requests.delete(
                "https://clients.quicklivraison.ma/api/parcels/%s" % self.tracking_number,
                headers={"Authorization": "Bearer %s" % api_key, "Accept": "application/json"},
                timeout=30,
            )
        except requests.exceptions.RequestException as error:
            raise UserError(_("QuickLivraison deletion failed: %s") % error) from error
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code != 404 and (
            not response.ok or payload.get("success") is False
        ):
            raise UserError(
                _("QuickLivraison deletion failed (HTTP %(status)s): %(message)s")
                % {"status": response.status_code, "message": payload.get("message") or response.text}
            )
        self.sudo().write(
            {
                "external_deleted": True,
                "external_deleted_at": fields.Datetime.now(),
                "archive_state": "external_deleted",
                "archive_error": False,
            }
        )
        return True

    def action_archive(self):
        if not self.env.su and not self.env.user.has_group(
            "system_confirmation.group_system_confirmation_manager"
        ):
            raise AccessError(_("Only parcel managers can archive parcels."))
        for colis in self.filtered("active"):
            colis._lock_workflow()
            if not colis.active:
                continue
            externally_created = bool(colis.tracking_number or colis.api_state == "sent")
            if not externally_created:
                colis.sudo().write(
                    {"active": False, "archive_state": "archived", "archive_error": False}
                )
                continue
            try:
                if not colis.external_deleted:
                    colis._delete_quicklivraison_parcel()
            except UserError as error:
                colis.sudo().write({"archive_error": str(error)})
                continue
            if not colis._create_automatic_return("archive"):
                colis.sudo().write(
                    {
                        "archive_state": "stock_error",
                        "archive_error": _("External parcel was deleted, but stock reversal failed."),
                    }
                )
                continue
            colis.sudo().write(
                {"active": False, "archive_state": "archived", "archive_error": False}
            )
        return True

    def action_retry_archive(self):
        return self.action_archive()

    def action_unarchive(self):
        if not self.env.su and not self.env.user.has_group(
            "system_confirmation.group_system_confirmation_manager"
        ):
            raise AccessError(_("Only parcel managers can unarchive parcels."))
        if self.filtered("external_deleted"):
            raise UserError(_("A parcel deleted from QuickLivraison cannot be unarchived."))
        self.sudo().write({"active": True, "archive_state": "none", "archive_error": False})
        return True

    def unlink(self):
        raise UserError(_("Parcels cannot be permanently deleted. Archive them instead."))


class ColisReturn(models.Model):
    _name = "system.confirmation.return"
    _description = "Parcel Product Return"
    _inherit = ["mail.thread"]
    _order = "registered_at desc, id desc"

    name = fields.Char(default=lambda self: _("New"), readonly=True, copy=False)
    colis_id = fields.Many2one(
        "system.confirmation.colis", required=True, index=True, ondelete="restrict", tracking=True
    )
    source = fields.Selection(
        [("manual", "Manual"), ("tracking", "Tracking Return"), ("archive", "Archive")],
        required=True,
        default="manual",
        readonly=True,
    )
    state = fields.Selection(
        [("pending", "Pending Validation"), ("done", "Validated"), ("error", "Stock Error")],
        default="pending",
        required=True,
        copy=False,
        tracking=True,
    )
    line_ids = fields.One2many(
        "system.confirmation.return.line", "return_id", string="Returned Products", copy=True
    )
    registered_by = fields.Many2one("res.users", default=lambda self: self.env.user, readonly=True)
    registered_at = fields.Datetime(default=fields.Datetime.now, readonly=True)
    validated_by = fields.Many2one("res.users", readonly=True, copy=False)
    validated_at = fields.Datetime(readonly=True, copy=False)
    stock_move_ids = fields.Many2many(
        "stock.move",
        "system_confirmation_return_move_rel",
        "return_id",
        "move_id",
        copy=False,
        readonly=True,
    )
    tracking_package_id = fields.Many2one("track.package", readonly=True, ondelete="set null")
    error_message = fields.Text(readonly=True, copy=False)

    @api.model_create_multi
    def create(self, vals_list):
        sequence = self.env["ir.sequence"]
        for values in vals_list:
            if values.get("name", _("New")) == _("New"):
                values["name"] = sequence.next_by_code("system.confirmation.return") or _("New")
        return super().create(vals_list)

    def _validate_quantities(self):
        self.ensure_one()
        if not self.line_ids:
            raise ValidationError(_("Add at least one returned product."))
        outstanding = self.colis_id._get_outstanding_quantities()
        for line in self.line_ids:
            available = outstanding.get(line.colis_line_id, 0.0)
            if float_compare(line.quantity, 0.0, precision_rounding=line.uom_id.rounding) <= 0:
                raise ValidationError(_("Return quantities must be positive."))
            if float_compare(line.quantity, available, precision_rounding=line.uom_id.rounding) > 0:
                raise ValidationError(
                    _("Returned quantity for %(product)s exceeds the outstanding shipped quantity (%(qty)s).")
                    % {"product": line.product_id.display_name, "qty": available}
                )

    def action_validate(self):
        for operation in self:
            if operation.state == "done":
                continue
            if not self.env.su and not self.env.user.has_group(
                "system_confirmation.group_system_confirmation_return_validator"
            ):
                raise AccessError(_("You are not allowed to validate product returns."))
            try:
                with self.env.cr.savepoint():
                    operation.colis_id._lock_workflow()
                    operation._validate_quantities()
                    moves = operation._create_return_moves()
            except Exception as error:
                _logger.exception("Return validation failed for %s", operation.name)
                operation.write({"state": "error", "error_message": str(error)})
                if not self.env.context.get("system_auto_return"):
                    self.env.user._bus_send(
                        "simple_notification",
                        {
                            "type": "danger",
                            "message": _("Return stock processing failed: %s") % error,
                            "sticky": True,
                        },
                    )
                return False
            operation.write(
                {
                    "state": "done",
                    "stock_move_ids": [(6, 0, moves.ids)],
                    "validated_by": self.env.user.id,
                    "validated_at": fields.Datetime.now(),
                    "error_message": False,
                }
            )
        return True

    def write(self, vals):
        protected = {"state", "validated_by", "validated_at", "stock_move_ids"}
        if protected.intersection(vals) and not self.env.su and not self.env.user.has_group(
            "system_confirmation.group_system_confirmation_return_validator"
        ):
            raise AccessError(_("You are not allowed to validate product returns."))
        return super().write(vals)

    def _create_return_moves(self):
        self.ensure_one()
        colis = self.colis_id
        stock_env = self.sudo().with_company(colis.company_id)
        default_destination = colis._get_stock_location()
        if not default_destination:
            raise UserError(_("The original stock location is not configured."))
        default_source = colis.company_id.partner_id.property_stock_customer
        moves = stock_env.env["stock.move"]
        for line in self.line_ids:
            outbound = colis.outbound_move_ids.filtered(
                lambda move: move.product_id == line.product_id and move.state == "done"
            )[:1]
            source = outbound.location_dest_id or default_source
            destination = outbound.location_id or default_destination
            move = stock_env.env["stock.move"].create(
                {
                    "name": "%s - %s" % (self.name, line.product_id.display_name),
                    "product_id": line.product_id.id,
                    "product_uom": line.uom_id.id,
                    "product_uom_qty": line.quantity,
                    "location_id": source.id,
                    "location_dest_id": destination.id,
                    "origin": "%s / %s" % (colis.name, self.name),
                }
            )
            stock_env.env["stock.move.line"].create(
                {
                    "move_id": move.id,
                    "product_id": line.product_id.id,
                    "product_uom_id": line.uom_id.id,
                    "quantity": line.quantity,
                    "location_id": source.id,
                    "location_dest_id": destination.id,
                    "picked": True,
                }
            )
            move._action_done()
            moves |= move
        return moves


class ColisReturnLine(models.Model):
    _name = "system.confirmation.return.line"
    _description = "Parcel Product Return Line"

    return_id = fields.Many2one(
        "system.confirmation.return", required=True, index=True, ondelete="cascade"
    )
    colis_line_id = fields.Many2one(
        "system.confirmation.colis.lines", required=True, ondelete="restrict"
    )
    product_id = fields.Many2one(related="colis_line_id.product_id", store=True, readonly=True)
    uom_id = fields.Many2one(related="colis_line_id.uom_id", store=True, readonly=True)
    shipped_quantity = fields.Float(related="colis_line_id.quantity", readonly=True)
    quantity = fields.Float(required=True, digits=(16, 3))

    @api.constrains("colis_line_id", "return_id")
    def _check_same_colis(self):
        for line in self:
            if line.colis_line_id.colis_id != line.return_id.colis_id:
                raise ValidationError(_("The returned line must belong to the original parcel."))


class ColisReturnWizard(models.TransientModel):
    _name = "system.confirmation.return.wizard"
    _description = "Register Parcel Return"

    colis_id = fields.Many2one("system.confirmation.colis", required=True, readonly=True)
    return_type = fields.Selection(
        [("full", "Full Return"), ("partial", "Partial Return")], default="full", required=True
    )
    line_ids = fields.One2many("system.confirmation.return.wizard.line", "wizard_id")

    @api.model
    def default_get(self, field_names):
        values = super().default_get(field_names)
        colis = self.env["system.confirmation.colis"].browse(
            values.get("colis_id") or self.env.context.get("default_colis_id")
        )
        if colis:
            values["line_ids"] = [
                (0, 0, {"colis_line_id": line.id, "quantity": quantity})
                for line, quantity in colis._get_outstanding_quantities().items()
                if not float_is_zero(quantity, precision_rounding=line.uom_id.rounding)
            ]
        return values

    @api.onchange("return_type")
    def _onchange_return_type(self):
        if not self.colis_id:
            return
        outstanding = self.colis_id._get_outstanding_quantities()
        for line in self.line_ids:
            line.quantity = outstanding.get(line.colis_line_id, 0.0) if self.return_type == "full" else 0.0

    def action_register(self):
        self.ensure_one()
        lines = self.line_ids.filtered(lambda line: line.quantity > 0)
        if not lines:
            raise UserError(_("Enter at least one returned quantity."))
        operation = self.env["system.confirmation.return"].create(
            {
                "colis_id": self.colis_id.id,
                "source": "manual",
                "line_ids": [
                    (0, 0, {"colis_line_id": line.colis_line_id.id, "quantity": line.quantity})
                    for line in lines
                ],
            }
        )
        operation._validate_quantities()
        return {
            "type": "ir.actions.act_window",
            "res_model": operation._name,
            "res_id": operation.id,
            "view_mode": "form",
            "target": "current",
        }


class ColisReturnWizardLine(models.TransientModel):
    _name = "system.confirmation.return.wizard.line"
    _description = "Register Parcel Return Line"

    wizard_id = fields.Many2one("system.confirmation.return.wizard", required=True, ondelete="cascade")
    colis_line_id = fields.Many2one("system.confirmation.colis.lines", required=True, readonly=True)
    product_id = fields.Many2one(related="colis_line_id.product_id", readonly=True)
    shipped_quantity = fields.Float(related="colis_line_id.quantity", readonly=True)
    uom_id = fields.Many2one(related="colis_line_id.uom_id", readonly=True)
    quantity = fields.Float(digits=(16, 3))


class ColisLineWorkflow(models.Model):
    _inherit = "system.confirmation.colis.lines"

    def unlink(self):
        if any(line.colis_id.colis_state != "draft" for line in self):
            raise UserError(_("Products cannot be removed from a confirmed parcel."))
        return super().unlink()

    def write(self, vals):
        shipment_fields = {"colis_id", "product_id", "uom_id", "quantity"}
        if shipment_fields.intersection(vals) and any(
            line.colis_id.colis_state != "draft" for line in self
        ):
            raise UserError(_("Products cannot be changed on a confirmed parcel."))
        return super().write(vals)
