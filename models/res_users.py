from odoo import api, fields, models


class ResUsers(models.Model):
    _inherit = "res.users"

    def write(self, values):
        result = super().write(values)
        if "confirmation_origin_id" in values:
            # Rule domains embed the user's computed origin IDs and are cached.
            self.env.registry.clear_cache()
        return result

    confirmation_origin_id = fields.Many2one(
        "system.confirmation.origin",
        string="Allowed Origin",
        help="If set, this user only sees the colis and clients coming from "
        "this origin. Leave empty to see everything.",
    )
    allowed_origin_ids = fields.Many2many(
        "system.confirmation.origin",
        compute="_compute_allowed_records",
        string="Visible Origins",
    )
    allowed_client_ids = fields.Many2many(
        "system.confirmation.client",
        compute="_compute_allowed_records",
        string="Visible Clients",
    )

    @api.depends("confirmation_origin_id", "groups_id")
    def _compute_allowed_records(self):
        # sudo() is required: these computes are evaluated from inside record
        # rules, and a sudo search bypasses the rules (no recursion).
        Origin = self.env["system.confirmation.origin"].sudo()
        Client = self.env["system.confirmation.client"].sudo()
        for user in self:
            origin = user.confirmation_origin_id
            if origin:
                user.allowed_origin_ids = origin
                user.allowed_client_ids = Client.search(
                    [
                        "|",
                        ("colis_ids", "=", False),
                        ("colis_ids.origin", "=", origin.id),
                    ]
                )
            elif user.has_group("system_confirmation.group_order_creator"):
                user.allowed_origin_ids = Origin.browse()
                user.allowed_client_ids = Client.browse()
            else:
                user.allowed_origin_ids = Origin.search([])
                user.allowed_client_ids = Client.search([])
