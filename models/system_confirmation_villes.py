from odoo import api, fields, models, _


class Villes(models.Model):
    _name = 'system.confirmation.villes'
    _description = 'Villes'

    name = fields.Char(string='Ville', required=True)
    code = fields.Char(string='Code', required=True)
    keyword_ids = fields.One2many('system.confirmation.keyword', 'ville_id', string='Keywords')
    
    
    
class Keyword(models.Model):
    _name = 'system.confirmation.keyword'
    _description = 'Keyword'

    name = fields.Char(string='Keyword', required=True)
    ville_id = fields.Many2one('system.confirmation.villes', string='Ville', required=True)