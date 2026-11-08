from odoo import fields, models

class ResCompany(models.Model):
    _inherit = "res.company"

    system_confirmation_quicklivraison_logo = fields.Binary(
        string="Logo de Quicklivraison",
        attachment=True,
    )