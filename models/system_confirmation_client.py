from odoo import fields, models, api


class ClientSystemConfirmation(models.Model):
    _name = "system.confirmation.client"
    _description = "System de confirmation Customer"

    _order = "name, id"

    name = fields.Char(required=True,index=True, tracking=True)
    telephone = fields.Char(index=True)
    adresse = fields.Char()
    ville = fields.Many2one("system.confirmation.villes", string="Ville")

    colis_ids = fields.One2many(
        "system.confirmation.colis", "client", string="Colis"
    )
    active = fields.Boolean("Active", default=True)
    colis_count = fields.Integer(compute="_compute_colis_count")
    qrcode = fields.Binary("QR Code", attachment=True, readonly=True)
    
    _sql_constraints = [
        ("unique_telephone", "unique(telephone)", "The phone number must be unique.")
    ]

    def _compute_colis_count(self):
        for client in self:
            client.colis_count = len(client.colis_ids)

    def action_view_colis(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Colis",
            "res_model": "system.confirmation.colis",
            "view_mode": "list,form,kanban",
            "domain": [("client", "=", self.id)],
            "context": {"default_client": self.id},
        }
