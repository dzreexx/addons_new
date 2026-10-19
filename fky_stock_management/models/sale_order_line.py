# -*- coding: utf-8 -*-
from odoo import api, fields, models

_STATE_RANK = {'urgent': 3, 'low': 2, 'ok': 1}


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    fky_stock_state = fields.Selection([
        ('ok', 'OK'),
        ('low', 'Low Stock'),
        ('urgent', 'Urgent'),
    ], string='Stock Alert', compute='_compute_fky_stock_state',
        help='Alert state from the Stock & Purchase Dashboard for this product '
             'at the order warehouse location (fallback: worst state across the '
             'product\'s locations). Snoozed dashboard rules are ignored.')

    @api.depends('product_id', 'order_id.warehouse_id')
    def _compute_fky_stock_state(self):
        """Read the nightly dashboard state (fky.stock.rule.state) for the line.

        Match rules at the SO warehouse stock location first; if the product
        has no rule there, fall back to the worst state across its rules.
        Snoozed rules are excluded so they do not shout at sales.
        """
        for line in self:
            line.fky_stock_state = False
        lines = self.filtered(lambda l: l.product_id)
        if not lines:
            return
        today = fields.Date.context_today(self)
        rules = self.env['fky.stock.rule'].search([
            ('product_id', 'in', lines.product_id.ids),
            '|',
            ('snooze_until', '=', False),
            ('snooze_until', '<', today),
        ])
        rules_by_product = {}
        for rule in rules:
            rules_by_product.setdefault(rule.product_id.id, []).append(rule)
        for line in lines:
            product_rules = rules_by_product.get(line.product_id.id)
            if not product_rules:
                continue
            warehouse_location = line.order_id.warehouse_id.lot_stock_id
            matched = [r for r in product_rules
                       if self._fky_location_in_warehouse(r.location_id, warehouse_location)]
            candidates = matched or product_rules
            line.fky_stock_state = max(
                candidates, key=lambda r: _STATE_RANK.get(r.state, 0)).state

    @api.model
    def _fky_location_in_warehouse(self, location, warehouse_location):
        """True when a dashboard rule location sits at (or inside) the warehouse."""
        if not location or not warehouse_location:
            return False
        if location == warehouse_location:
            return True
        if warehouse_location.parent_path and location.parent_path:
            return location.parent_path.startswith(warehouse_location.parent_path)
        return False
