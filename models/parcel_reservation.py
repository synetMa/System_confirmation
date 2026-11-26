from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_compare


_DEFER_CHECK = object()


class ParcelReservationError(UserError):
    """A draft parcel would reserve more stock than is available."""


class ProductReservationLock(models.Model):
    _inherit = "product.product"

    parcel_reservation_version = fields.Integer(default=0, copy=False)

    def _lock_parcel_reservations(self):
        # Updating a shared row, rather than just taking an advisory lock, makes
        # concurrent Odoo repeatable-read transactions retry with a fresh snapshot.
        for product_id in sorted(self.ids):
            self.env.cr.execute(
                "UPDATE product_product SET parcel_reservation_version = "
                "COALESCE(parcel_reservation_version, 0) + 1 WHERE id = %s",
                [product_id],
            )
        self.invalidate_recordset(["parcel_reservation_version"])


class ParcelReservation(models.Model):
    _inherit = "system.confirmation.colis"

    @api.model
    def create_package(self, package_data):
        with self.env.cr.savepoint():
            return super().create_package(package_data)

    @api.model
    def create_draft_order(self, data):
        with self.env.cr.savepoint():
            return super().create_draft_order(data)

    @api.model
    def update_draft_products(self, order_id, items, original, amount=None, original_amount=None):
        with self.env.cr.savepoint():
            order = self._creation_order(order_id, lock=True)
            old_products = order.items_line_ids.product_id
            old_products._lock_parcel_reservations()
            result = super(ParcelReservation, self.with_context(
                defer_parcel_reservation_check=_DEFER_CHECK
            )).update_draft_products(order_id, items, original, amount, original_amount)
            order.invalidate_recordset(["items_line_ids"])
            order._check_draft_reservations(
                old_products | order.items_line_ids.product_id, order.company_id
            )
        return result

    @api.model
    def _check_draft_reservations(self, products, companies):
        products = products.sudo().filtered(lambda p: p.is_storable)
        if not products:
            return
        location = self._get_stock_location()
        if not location:
            raise UserError(_("Configure the stock location before creating parcels with stocked products."))
        lines = self.env["system.confirmation.colis.lines"].sudo().search([
            ("product_id", "in", products.ids),
            ("colis_id.active", "=", True),
            ("colis_id.colis_state", "=", "draft"),
        ])
        demand = {}
        for line in lines:
            key = line.product_id.id
            demand[key] = demand.get(key, 0.0) + line.uom_id._compute_quantity(
                line.quantity, line.product_id.uom_id
            )
        quant = self.env["stock.quant"].sudo().with_company(companies[:1] or self.env.company)
        for product in products:
            required = demand.get(product.id, 0.0)
            if not required:
                continue
            available = quant._get_available_quantity(product, location, strict=True)
            if float_compare(required, available, precision_rounding=product.uom_id.rounding) > 0:
                raise ParcelReservationError(
                    _("Création impossible : le produit %(product)s est déjà réservé ou son stock disponible est insuffisant (disponible : %(available)s, réservé dans les brouillons : %(required)s).")
                    % {"product": product.display_name, "available": available, "required": required}
                )

    @api.model_create_multi
    def create(self, vals_list):
        with self.env.cr.savepoint():
            parcels = super(ParcelReservation, self.with_context(
                defer_parcel_reservation_check=_DEFER_CHECK
            )).create(vals_list)
            parcels = parcels.with_env(self.env)
            lines = parcels.items_line_ids
            parcels._check_draft_reservations(lines.product_id, parcels.company_id)
        return parcels

    def write(self, vals):
        reservation_change = {"items_line_ids", "colis_state", "active", "company_id"}.intersection(vals)
        if not reservation_change:
            return super().write(vals)
        with self.env.cr.savepoint():
            old_products = self.items_line_ids.product_id
            old_companies = self.company_id
            old_products._lock_parcel_reservations()
            result = super(ParcelReservation, self.with_context(
                defer_parcel_reservation_check=_DEFER_CHECK
            )).write(vals)
            products = old_products | self.items_line_ids.product_id
            products._lock_parcel_reservations()
            if not (vals.get("colis_state") and vals["colis_state"] != "draft"
                    or vals.get("active") is False):
                self._check_draft_reservations(products, old_companies | self.company_id)
        return result


class ParcelLineReservation(models.Model):
    _inherit = "system.confirmation.colis.lines"

    @api.model_create_multi
    def create(self, vals_list):
        products = self.env["product.product"].browse(
            [vals["product_id"] for vals in vals_list if vals.get("product_id")]
        )
        with self.env.cr.savepoint():
            products._lock_parcel_reservations()
            lines = super().create(vals_list)
            if self.env.context.get("defer_parcel_reservation_check") is not _DEFER_CHECK:
                lines.colis_id._check_draft_reservations(lines.product_id, lines.colis_id.company_id)
        return lines

    def write(self, vals):
        if not {"product_id", "quantity", "uom_id", "colis_id"}.intersection(vals):
            return super().write(vals)
        with self.env.cr.savepoint():
            products = self.product_id | self.env["product.product"].browse(vals.get("product_id"))
            companies = self.colis_id.company_id
            products._lock_parcel_reservations()
            result = super().write(vals)
            if self.env.context.get("defer_parcel_reservation_check") is not _DEFER_CHECK:
                self.colis_id._check_draft_reservations(
                    products | self.product_id, companies | self.colis_id.company_id
                )
        return result

    def unlink(self):
        self.product_id._lock_parcel_reservations()
        return super().unlink()
