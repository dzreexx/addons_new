# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class StockBomLine(models.TransientModel):
    _name = 'fky.stock.bom.line'
    _description = 'Stock Alert BOM Component Line'

    wizard_id = fields.Many2one(
        'fky.stock.bom.wizard',
        string='Wizard',
        ondelete='cascade',
    )

    component_id = fields.Many2one(
        'product.product',
        string='Component',
        readonly=True,
    )

    quantity = fields.Float(
        string='Quantity',
        readonly=True,
        help='Material quantity per unit of the parent product (BOM).',
    )

    onhand_qty = fields.Float(
        string='On Hand',
        readonly=True,
        help='Total on-hand di lokasi toko+gudang cabang (lokasi internal '
             'lain seperti Karantina/Transit tidak ikut).',
    )

    incoming_qty = fields.Float(
        string='Incoming',
        readonly=True,
        help='Incoming company-wide (PO waiting/confirmed/assigned), '
             'definisi sama seperti analisa.',
    )

    plan_qty = fields.Float(
        string='Plan Qty',
        help='Quantity of this material to add into the analysis via the '
             'Add button (preview only; other views are reference).')

    product_uom_id = fields.Many2one(
        'uom.uom',
        string='Unit of Measure',
        readonly=True,
    )

    component_sku = fields.Char(
        string='Component SKU',
        readonly=True,
    )


class StockBomWizard(models.TransientModel):
    _name = 'fky.stock.bom.wizard'
    _description = 'Stock Alert Product Components'

    product_id = fields.Many2one(
        'product.product',
        string='Product',
        readonly=True,
    )

    company_id = fields.Many2one(
        'res.company',
        string='Company',
        readonly=True,
        help='Company untuk angka On Hand (toko+gudang) dan Incoming.',
    )

    bom_id = fields.Char(
        string='BOM Reference',
        readonly=True,
    )

    final_qty = fields.Float(
        string='Final Qty',
        readonly=True,
        help='Final Quantity of the analysis/report line this popup was '
             'opened from (reference only; 0 from the dashboard).')

    preview_line_id = fields.Many2one(
        'fky.stock.analysis.preview.line', string='Analysis Line',
        readonly=True, ondelete='cascade',
        help='Set when opened from an analysis preview line: quantities can '
             'then be added back into the analysis. Empty for dashboard and '
             'saved-report views (read-only reference).')

    report_line_id = fields.Many2one(
        'fky.stock.analysis.line', string='Report Line',
        readonly=True, ondelete='cascade',
        help='Set when opened from a saved-report line: quantities can '
             'then be added back into the report.')

    line_ids = fields.One2many(
        'fky.stock.bom.line',
        'wizard_id',
        string='Components',
    )

    @api.model
    def _component_line_vals(self, bom, company):
        """Build ``(0, 0)`` line vals for a BOM with live stock figures.

        Each line carries toko+gudang on-hand plus company-wide incoming
        (see ``fky.stock.analysis.service._component_stock``), filled once
        at popup open so every entry point (dashboard, preview, report)
        shows the same numbers.
        """
        vals = []
        if not bom:
            return vals
        comp_products = bom.bom_line_ids.mapped('product_id').filtered('id')
        stock = self.env['fky.stock.analysis.service']._component_stock(
            company, comp_products)
        for line in bom.bom_line_ids:
            if not line.product_id:
                continue
            figures = stock.get(line.product_id.id, {})
            vals.append((0, 0, {
                'component_id': line.product_id.id,
                'quantity': line.product_qty,
                'plan_qty': 0.0,
                'product_uom_id': line.product_uom_id.id,
                'component_sku': line.product_id.default_code,
                'onhand_qty': figures.get('onhand_qty', 0.0),
                'incoming_qty': figures.get('incoming_qty', 0.0),
            }))
        return vals

    def action_add_to_analysis(self):
        """Add material quantities back into the analysis preview.

        Existing material lines accumulate (final_qty += plan); missing
        materials get fresh lines with full metrics. Read-only contexts
        (no preview line) are rejected.
        """
        self.ensure_one()
        if not self.preview_line_id or not self.preview_line_id.exists():
            raise UserError(_(
                'Materials can only be added from an analysis preview. '
                'From a saved report, use Regenerate first.'))
        preview = self.preview_line_id.preview_id
        self._add_material_lines(
            'fky.stock.analysis.preview.line', 'preview_id', preview.id,
            preview.company_id, preview.average_months,
            preview.stock_coverage_months)
        return {
            'name': _('Stock Purchase Analysis Preview'),
            'type': 'ir.actions.act_window',
            'res_model': 'fky.stock.analysis.preview',
            'view_mode': 'form',
            'res_id': preview.id,
            'target': 'main',
        }

    def action_add_to_report(self):
        """Add material quantities back into the saved report.

        Same accumulate-or-create semantics as the preview flow; the
        report is reused again and again.
        """
        self.ensure_one()
        if not self.report_line_id or not self.report_line_id.exists():
            raise UserError(_(
                'Materials can only be added from a saved report line.'))
        report = self.report_line_id.report_id
        self._add_material_lines(
            'fky.stock.analysis.line', 'report_id', report.id,
            report.company_id, report.average_months,
            report.stock_coverage_months)
        return {
            'name': _('Stock Purchase Analysis Report'),
            'type': 'ir.actions.act_window',
            'res_model': 'fky.stock.analysis.report',
            'view_mode': 'form',
            'res_id': report.id,
            'target': 'main',
        }

    def _add_material_lines(self, line_model, parent_field, parent_id,
                            company, average_months, coverage_default):
        """Shared accumulate-or-create engine for preview and report flows."""
        self.ensure_one()
        wanted = [(line.component_id, line.plan_qty or 0.0)
                  for line in self.line_ids
                  if line.component_id and (line.plan_qty or 0.0) > 0.0]
        if not wanted:
            raise UserError(_(
                'Fill Plan Qty (> 0) on at least one material first.'))
        products = self.env['product.product'].concat(
            *[product for product, _qty in wanted])
        service = self.env['fky.stock.analysis.service']
        values = service.analyze(
            company, products, average_months, coverage_default,
            combine='min')
        coverage_map = self.env['fky.stock.coverage.rule'].resolve_coverage_batch(
            products, company, fallback=coverage_default)
        LineModel = self.env[line_model]
        existing_lines = LineModel.search([
            (parent_field, '=', parent_id),
            ('product_id', 'in', products.ids),
        ])
        # One batched MoQ pass for every material, instead of a seller
        # lookup per line inside the loop.
        vendor_map = {product.id: values.get(product.id, {}).get('vendor_id')
                      for product, _qty in wanted}
        moq_map = service.default_moq_batch(products, vendor_map, company)
        for product, qty in wanted:
            metrics = values.get(product.id, {})
            existing = existing_lines.filtered(
                lambda l: l.product_id == product)[:1]
            if existing:
                existing.write({'final_qty': existing.final_qty + qty})
                continue
            coverage = coverage_map.get(product.id, coverage_default)
            future_stock = (metrics.get('on_hand_qty', 0.0)
                            + metrics.get('incoming_qty', 0.0)
                            - metrics.get('sales_order_qty', 0.0))
            target_stock = metrics.get('average_monthly_sales', 0.0) * coverage
            # Same rounding rule as the wizard/preview: target stays raw,
            # only the plannable quantity is rounded to whole dus.
            recommended = service.round_to_isi_perdus(
                max(0.0, target_stock - future_stock), product.isi_perdus)
            done = {key: value for key, value in metrics.items()
                    if key != 'components'}
            done.update({
                'target_stock_qty': target_stock,
                'recommended_purchase_qty': recommended,
                'has_components': bool(metrics.get('components')),
                'moq_qty': moq_map.get(product.id, 0.0),
                'final_qty': qty,
            })
            LineModel.create(dict(
                done,
                **{parent_field: parent_id},
                company_id=company.id,
                product_id=product.id,
                coverage_months=coverage,
            ))