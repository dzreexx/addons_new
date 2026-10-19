# -*- coding: utf-8 -*-
"""Remembered manual MoQ per product.

Written automatically on Save Report (MoQ > 0) and removed when emptied,
mirroring the coverage-rule pattern. The next Generate uses the remembered
value instead of the seller minimum — clear it (0) to follow the master
supplier data again.
"""
from odoo import fields, models


class StockMoqRule(models.Model):
    _name = 'fky.stock.moq.rule'
    _description = 'Remembered MoQ per Product'
    _order = 'product_id'

    product_id = fields.Many2one(
        'product.product', string='Product', required=True, ondelete='cascade')
    company_id = fields.Many2one(
        'res.company', string='Company', required=True,
        default=lambda self: self.env.company.id, ondelete='cascade')
    moq_qty = fields.Float(string='MoQ', required=True)
    active = fields.Boolean(string='Active', default=True)

    _sql_constraints = [
        ('product_company_uniq',
         'unique(product_id, company_id)',
         'A remembered MoQ already exists for this product!'),
    ]
