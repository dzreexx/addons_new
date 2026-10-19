# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import fields, models


class StockSnoozeWizard(models.TransientModel):
    _name = 'fky.stock.snooze.wizard'
    _description = 'Snooze Stock Alert Rule'

    rule_id = fields.Many2one(
        'fky.stock.rule',
        string='Alert Rule',
        required=True,
        readonly=True,
    )

    product_id = fields.Many2one(
        'product.product',
        related='rule_id.product_id',
        string='Product',
        readonly=True,
    )

    snooze_days = fields.Integer(
        string='Snooze (days)',
        default=1,
        required=True,
        help='Number of days to hide this product from the stock alert dashboard.',
    )

    def action_snooze(self):
        self.ensure_one()
        if self.snooze_days > 0:
            self.rule_id.sudo().write({
                'snooze_until': fields.Date.context_today(self) + timedelta(days=self.snooze_days),
            })
        return {'type': 'ir.actions.act_window_close'}