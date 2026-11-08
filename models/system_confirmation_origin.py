from odoo import fields, models

class OriginConfirmation(models.Model):
    _name = "system.confirmation.origin"
    _description = "Origin Confirmation"

    name = fields.Char("Nom", required=True,  index=True)
    
    _sql_constraints = [
        ("name_uniq", "unique(name)", "Origin name must be unique.")
    ]