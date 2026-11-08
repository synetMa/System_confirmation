from odoo import api, fields, models


class ResUsers(models.Model):
    _inherit = "res.users"

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

    @api.depends("confirmation_origin_id")
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
            else:
                user.allowed_origin_ids = Origin.search([])
                user.allowed_client_ids = Client.search([])
