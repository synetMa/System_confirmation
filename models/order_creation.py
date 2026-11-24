import math

from markupsafe import Markup, escape
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError


CREATOR_GROUP = "system_confirmation.group_order_creator"


class OrderCreation(models.Model):
    _inherit = "system.confirmation.colis"

    def _check_order_creator_access(self):
        if not (self.env.user.has_group(CREATOR_GROUP) or
                self.env.user.has_group("system_confirmation.group_system_confirmation_user")):
            raise AccessError(_("You cannot use order creation."))
        origin = self.env.user.confirmation_origin_id
        if not origin:
            raise UserError(_("Ask an administrator to assign an origin to your user."))
        return origin

    @api.model
    def creation_options(self):
        origin = self._check_order_creator_access()
        return {
            "origin": origin.name,
            "cities": self.env["system.confirmation.villes"].sudo().search_read(
                [], ["name", "code"], order="name"
            ),
        }

    @api.model
    def creation_products(self, query=""):
        self._check_order_creator_access()
        query = (query or "").strip()[:100]
        domain = [("sale_ok", "=", True)]
        if query:
            domain += ["|", ("name", "ilike", query), ("default_code", "ilike", query)]
        return self.env["product.product"].sudo().search_read(
            domain, ["name", "default_code"], limit=30, order="name"
        )

    @api.model
    def creation_orders(self, offset=0):
        origin = self._check_order_creator_access()
        try:
            offset = max(0, int(offset))
        except (TypeError, ValueError):
            raise UserError(_("Invalid page."))
        orders = self.sudo().search([("origin", "=", origin.id)], offset=offset, limit=51)
        return {"has_more": len(orders) > 50, "orders": [{
            "id": order.id,
            "name": order.name,
            "state": order.colis_state,
            "state_label": dict(order._fields["colis_state"].selection).get(
                order.colis_state, order.colis_state
            ),
            "amount": order.amount,
            "client_name": order.client.name or "",
            "client_phone": order.client.telephone or "",
            "client_address": order.client.adresse or "",
            "client_city": order.client.ville.id or False,
            "date": fields.Date.to_string(order.date),
        } for order in orders[:50]]}

    @api.model
    def _creation_client_values(self, data):
        name = str(data.get("name") or "").strip()
        phone = str(data.get("phone") or "").strip()
        address = str(data.get("address") or "").strip()
        try:
            city_id = int(data.get("city") or 0)
        except (ValueError, TypeError):
            city_id = 0
        city = self.env["system.confirmation.villes"].sudo().browse(city_id).exists()
        if not name or not phone or not address or not city:
            raise UserError(_("Enter a name, phone number, address and valid city."))
        return {"name": name, "telephone": phone, "adresse": address, "ville": city.id}

    @api.model
    def _creation_find_client(self, values):
        client = self.env["system.confirmation.client"].sudo().with_context(active_test=False).search(
            [("telephone", "=", values["telephone"])], limit=1
        )
        if client and (not client.active or client.name != values["name"] or
                       client.adresse != values["adresse"] or client.ville.id != values["ville"]):
            raise UserError(_("This phone number is already in use with different customer information. Contact a manager."))
        return client or self.env["system.confirmation.client"].sudo().create(values)

    @api.model
    def create_draft_order(self, data):
        origin = self._check_order_creator_access()
        values = self._creation_client_values(data.get("client") or {})
        try:
            amount = float(data.get("amount"))
        except (TypeError, ValueError):
            amount = -1
        if not math.isfinite(amount) or amount < 0:
            raise UserError(_("Enter a valid non-negative amount."))
        items = data.get("items") or []
        if not isinstance(items, list) or not items or len(items) > 100:
            raise UserError(_("Select at least one product."))
        lines = []
        for item in items:
            try:
                product_id = int(item.get("product_id"))
                qty = float(item.get("quantity"))
            except (AttributeError, TypeError, ValueError):
                raise UserError(_("Invalid product or quantity."))
            product = self.env["product.product"].sudo().browse(product_id).exists()
            if not product or not product.active or not product.sale_ok or not math.isfinite(qty) or qty <= 0:
                raise UserError(_("Invalid product or quantity."))
            lines.append((0, 0, {"product_id": product.id, "quantity": qty}))
        client = self._creation_find_client(values)
        order = self.sudo().create({
            "origin": origin.id, "client": client.id, "amount": amount,
            "shipping_cost": 35.0,
            "items_line_ids": lines,
        })
        return {"id": order.id, "name": order.name}

    @api.model
    def _creation_order(self, order_id, lock=False):
        origin = self._check_order_creator_access()
        order = self.sudo().browse(order_id).exists()
        if len(order) != 1 or order.origin.id != origin.id:
            raise AccessError(_("You cannot access this order."))
        if lock:
            order._lock_workflow()
            order.invalidate_recordset(["origin", "items_line_ids"])
            if order.origin.id != origin.id:
                raise AccessError(_("You cannot access this order."))
        return order

    @api.model
    def creation_order_lines(self, order_id):
        order = self._creation_order(order_id)
        return [{
            "line_id": line.id,
            "product_id": line.product_id.id,
            "name": line.product_id.display_name,
            "default_code": line.product_reference or "",
            "quantity": line.quantity,
        } for line in order.items_line_ids]

    @api.model
    def update_draft_products(self, order_id, items, original):
        order = self._creation_order(order_id, lock=True)
        if order.colis_state != "draft":
            raise UserError(_("Products can only be edited on a draft order."))
        if not isinstance(items, list) or len(items) > 100 or not isinstance(original, list):
            raise UserError(_("Invalid product list."))
        current = {
            line.id: (line.product_id.id, line.quantity)
            for line in order.items_line_ids
        }
        try:
            snapshot = {
                int(item["line_id"]): (int(item["product_id"]), float(item["quantity"]))
                for item in original
            }
        except (TypeError, ValueError, KeyError):
            raise UserError(_("Invalid product list."))
        if len(snapshot) != len(original) or snapshot != current:
            raise UserError(_("The products have changed since you opened this order. Reload and try again."))

        updates = []
        additions = []
        kept_ids = set()
        for item in items:
            try:
                line_id = int(item.get("line_id") or 0)
                product_id = int(item.get("product_id"))
                quantity = float(item.get("quantity"))
            except (AttributeError, TypeError, ValueError):
                raise UserError(_("Invalid product or quantity."))
            if not math.isfinite(quantity) or quantity <= 0:
                raise UserError(_("Invalid product or quantity."))
            if line_id:
                if line_id in kept_ids or line_id not in current or current[line_id][0] != product_id:
                    raise AccessError(_("Invalid order product line."))
                kept_ids.add(line_id)
                updates.append((line_id, quantity))
            else:
                product = self.env["product.product"].sudo().browse(product_id).exists()
                if not product or not product.active or not product.sale_ok:
                    raise UserError(_("Invalid product or quantity."))
                additions.append({"colis_id": order.id, "product_id": product.id, "quantity": quantity})

        lines = self.env["system.confirmation.colis.lines"].sudo()
        for line_id, quantity in updates:
            if current[line_id][1] != quantity:
                lines.browse(line_id).write({"quantity": quantity})
        lines.browse(list(set(current) - kept_ids)).unlink()
        if additions:
            lines.create(additions)
        return True

    @api.model
    def update_draft_customer(self, order_id, data):
        order = self._creation_order(order_id, lock=True)
        if order.colis_state != "draft":
            raise UserError(_("Customer information can only be edited on a draft order."))
        values = self._creation_client_values(data)
        client = order.client
        if not client:
            raise UserError(_("This order has no customer."))
        previous = {
            "name": client.name or "", "telephone": client.telephone or "",
            "adresse": client.adresse or "", "ville": client.ville.id or False,
        }
        if previous == values:
            return True
        # A customer is shared across orders. Never rewrite another order's customer.
        other_orders = self.sudo().with_context(active_test=False).search_count(
            [("client", "=", client.id), ("id", "!=", order.id)], limit=1
        )
        if other_orders:
            if values["telephone"] == client.telephone:
                raise UserError(_("This customer is shared by other orders. Contact a manager or use a different phone number."))
            order.write({"client": self._creation_find_client(values).id})
        else:
            existing = self.env["system.confirmation.client"].sudo().with_context(active_test=False).search(
                [("telephone", "=", values["telephone"]), ("id", "!=", client.id)], limit=1
            )
            if existing:
                order.write({"client": self._creation_find_client(values).id})
            else:
                client.sudo().write(values)
        city_model = self.env["system.confirmation.villes"].sudo()
        old_city = city_model.browse(previous["ville"]).name or ""
        new_city = city_model.browse(values["ville"]).name or ""
        parts = []
        for label, old, new in (
            (_("Name"), previous["name"], values["name"]),
            (_("Phone"), previous["telephone"], values["telephone"]),
            (_("Address"), previous["adresse"], values["adresse"]),
            (_("City"), old_city, new_city),
        ):
            if old != new:
                parts.append(Markup("<li>%s: %s → %s</li>") % (escape(label), escape(old), escape(new)))
        order.message_post(body=Markup("<p>%s</p><ul>%s</ul>") % (
            escape(_("Customer information changed")), Markup("").join(parts)
        ), subtype_xmlid="mail.mt_note")
        return True

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su and self.env.user.has_group(CREATOR_GROUP):
            raise AccessError(_("Use order creation to create draft orders."))
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.su and self.env.user.has_group(CREATOR_GROUP):
            raise AccessError(_("Use order creation to edit draft customer information."))
        return super().write(vals)

    def action_confirm(self):
        if not self.env.su and self.env.user.has_group(CREATOR_GROUP):
            raise AccessError(_("Order creators cannot confirm orders."))
        return super().action_confirm()

    def create_package_api(self, colis):
        if not self.env.su and self.env.user.has_group(CREATOR_GROUP):
            raise AccessError(_("Order creators cannot send orders to QuickLivraison."))
        return super().create_package_api(colis)


class OrderCreatorClientGuard(models.Model):
    _inherit = "system.confirmation.client"

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su and self.env.user.has_group(CREATOR_GROUP):
            raise AccessError(_("Use order creation to manage customer information."))
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.su and self.env.user.has_group(CREATOR_GROUP):
            raise AccessError(_("Use order creation to manage customer information."))
        return super().write(vals)


class OrderCreatorLineGuard(models.Model):
    _inherit = "system.confirmation.colis.lines"

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su and self.env.user.has_group(CREATOR_GROUP):
            raise AccessError(_("Use order creation to create product lines."))
        lines = super().create(vals_list)
        for line in lines:
            line.colis_id.message_post(
                body=_("Product added: %(product)s, quantity %(quantity)s, price %(price)s.") % {
                    "product": line.product_id.display_name,
                    "quantity": line.quantity,
                    "price": line.online_price,
                }, subtype_xmlid="mail.mt_note"
            )
        return lines

    def write(self, vals):
        if not self.env.su and self.env.user.has_group(CREATOR_GROUP):
            raise AccessError(_("Order creators cannot modify product lines."))
        tracked = {"product_id", "quantity", "uom_id", "online_price", "list_price", "cost_price"}
        before = {
            line.id: (line.product_id.display_name, line.quantity, line.uom_id.display_name,
                      line.online_price, line.list_price, line.cost_price)
            for line in self
        } if tracked.intersection(vals) else {}
        result = super().write(vals)
        for line in self:
            if line.id in before:
                after = (line.product_id.display_name, line.quantity, line.uom_id.display_name,
                         line.online_price, line.list_price, line.cost_price)
                if before[line.id] != after:
                    line.colis_id.message_post(
                        body=_("Product line changed: %(old)s → %(new)s") % {
                            "old": str(before[line.id]), "new": str(after),
                        }, subtype_xmlid="mail.mt_note"
                    )
        return result

    def unlink(self):
        if not self.env.su and self.env.user.has_group(CREATOR_GROUP):
            raise AccessError(_("Order creators cannot delete product lines."))
        for line in self:
            line.colis_id.message_post(
                body=_("Product removed: %(product)s, quantity %(quantity)s.") % {
                    "product": line.product_id.display_name, "quantity": line.quantity,
                }, subtype_xmlid="mail.mt_note"
            )
        return super().unlink()
