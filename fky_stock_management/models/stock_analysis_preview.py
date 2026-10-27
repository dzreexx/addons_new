# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class StockAnalysisPreview(models.TransientModel):
    """Editable preview of an analysis run before it is persisted.

    The user reviews the rows, ticks the ones to purchase and (optionally)
    fine-tunes MoQ / Final Quantity.  Rows become a saved
    :model:`fky.stock.analysis.report` only when ``action_save_report`` is
    pressed, so nothing is persisted on a mere preview.
    """

    _name = 'fky.stock.analysis.preview'
    _description = 'Stock Purchase Analysis Preview'

    company_id = fields.Many2one(
        'res.company', string='Perusahaan', readonly=True)
    average_months = fields.Integer(
        string='Average Month', readonly=True)
    stock_coverage_months = fields.Integer(
        string='Ketahanan Bulan (Default)', readonly=True)
    product_ids = fields.Many2many(
        'product.product', string='Filter Product', readonly=True)
    internal_category_ids = fields.Many2many(
        'internal.category', string='Internal Category', readonly=True)
    vendor_ids = fields.Many2many(
        'res.partner', string='Vendor', readonly=True)

    target_report_id = fields.Many2one(
        'fky.stock.analysis.report', string='Previous Report',
        help='Bila diisi, Save Report membuat report versi baru (bukan '
             'menimpa): report lama dikunci Done dan MoQ terbawa '
             '(dipakai alur Regenerate).')

    preview_line_ids = fields.One2many(
        'fky.stock.analysis.preview.line', 'preview_id',
        string='Hasil Analisa')

    line_count = fields.Integer(
        string='Jumlah Baris', compute='_compute_line_count')

    # ------------------------------------------------------------------ #
    # Computed helpers
    # ------------------------------------------------------------------ #

    def _compute_line_count(self):
        for preview in self:
            preview.line_count = len(preview.preview_line_ids)

    def _internal_category_command(self):
        """Build an m2m command, tolerating a missing internal.category model.

        The internal.category comodel comes from an optional addon; when it is
        not loaded the field comodel is left as ``_unknown`` by the registry,
        so reading ``self.internal_category_ids`` would fail.  Return an empty
        command instead.
        """
        if not self.env.get('internal.category'):
            return [(6, 0, [])]
        return [(6, 0, self.internal_category_ids.ids)]

    # ------------------------------------------------------------------ #
    # Actions
    # ------------------------------------------------------------------ #

    def action_save_report(self):
        """Persist the preview rows as a report (reused again and again).

        Regenerate flow (``target_report_id`` set): the previous report's
        lines are rebuilt in place — MoQ values are carried over into the
        new preview, Final quantities restart from zero.
        """
        self.ensure_one()
        report = self.target_report_id
        if report:
            if report.company_id.id != self.company_id.id:
                raise ValidationError(
                    _('The target report belongs to a different company.'))
            report.write({
                'vendor_ids': [(6, 0, self.vendor_ids.ids)],
            })
            report.line_ids.unlink()
        else:
            report = self.env['fky.stock.analysis.report'].create({
                'company_id': self.company_id.id,
                'average_months': self.average_months,
                'stock_coverage_months': self.stock_coverage_months,
                'product_ids': [(6, 0, self.product_ids.ids)],
                'internal_category_ids': self._internal_category_command(),
                'vendor_ids': [(6, 0, self.vendor_ids.ids)],
            })
            now = fields.Datetime.context_timestamp(self, fields.Datetime.now())
            report.write({
                'name': '%s - %s' % (report.company_id.name or 'Analysis',
                                     now.strftime('%d %b %Y %H:%M')),
            })

        lines = []
        for line in self.preview_line_ids:
            lines.append({
                'report_id': report.id,
                'company_id': line.company_id.id,
                'product_id': line.product_id.id,
                'coverage_months': line.coverage_months,
                'moq_qty': line.moq_qty,
                'final_qty': line.final_qty,
                'on_hand_qty': line.on_hand_qty,
                'incoming_qty': line.incoming_qty,
                'sales_order_qty': line.sales_order_qty,
                'average_monthly_sales': line.average_monthly_sales,
                'target_stock_qty': line.target_stock_qty,
                'recommended_purchase_qty': line.recommended_purchase_qty,
                'months_of_stock': line.months_of_stock,
                'future_months_of_stock': line.future_months_of_stock,
                'vendor_id': line.vendor_id.id,
                'has_components': line.has_components,
                'isi_perdus': line.isi_perdus,
            })
        if lines:
            self.env['fky.stock.analysis.line'].create(lines)

        self._remember_moq_lines()

        return {
            'name': _('Stock Purchase Analysis'),
            'type': 'ir.actions.act_window',
            'res_model': 'fky.stock.analysis.report',
            'view_mode': 'form',
            'res_id': report.id,
            'target': 'main',
        }

    def _remember_moq_lines(self):
        """Persist manual MoQs per product (0 clears the remembered value)."""
        self.ensure_one()
        MoqRule = self.env['fky.stock.moq.rule']
        for line in self.preview_line_ids:
            if not line.product_id:
                continue
            rule = MoqRule.search([
                ('product_id', '=', line.product_id.id),
                ('company_id', '=', self.company_id.id),
            ], limit=1)
            if line.moq_qty and line.moq_qty > 0.0:
                vals = {
                    'moq_qty': line.moq_qty,
                    'active': True,
                    'company_id': self.company_id.id,
                }
                if rule:
                    rule.write(vals)
                else:
                    MoqRule.create(dict(
                        vals, product_id=line.product_id.id))
            elif rule:
                rule.unlink()

    def action_create_purchase_order(self):
        """Create a draft PO from the preview rows with a Final Quantity."""
        self.ensure_one()
        lines = self.preview_line_ids.filtered(
            lambda line: line.final_qty > 0.0)
        return self.env['fky.stock.analysis.line']._create_purchase_order_from_lines(
            lines)

    def action_view_lines(self):
        """Open the preview lines in a searchable list view."""
        self.ensure_one()
        return {
            'name': _('Analysis Lines'),
            'type': 'ir.actions.act_window',
            'res_model': 'fky.stock.analysis.preview.line',
            'view_mode': 'tree',
            'domain': [('preview_id', '=', self.id)],
            'view_id': self.env.ref(
                'fky_stock_management.fky_stock_analysis_preview_line_tree').id,
            'search_view_id': self.env.ref(
                'fky_stock_management.fky_stock_analysis_preview_line_search').id,
        }


class StockAnalysisPreviewLine(models.TransientModel):
    """One editable row of a :model:`fky.stock.analysis.preview`."""

    _name = 'fky.stock.analysis.preview.line'
    _description = 'Stock Purchase Analysis Preview Line'
    _order = 'months_of_stock asc, average_monthly_sales desc'

    preview_id = fields.Many2one(
        'fky.stock.analysis.preview', string='Preview', ondelete='cascade')

    company_id = fields.Many2one(
        'res.company', string='Perusahaan', readonly=True)
    product_id = fields.Many2one(
        'product.product', string='Product Name', required=True, readonly=True)

    default_code = fields.Char(
        string='Code', related='product_id.default_code',
        search='_search_default_code')
    product_name = fields.Char(string='Product Name', related='product_id.display_name')

    def _search_default_code(self, operator, value):
        return [('product_id.default_code', operator, value)]

    internal_category_id = fields.Many2one(
        'internal.category', string='Internal Category', readonly=True,
        compute='_compute_internal_category', store=True,
        help='Kategori internal produk (mis. carburator, CDI, as gear depan).')

    product_brand_id = fields.Many2one(
        'product.brand', string='Brand', readonly=True,
        compute='_compute_product_brand', store=True,
        help='Brand produk dari master produk.')

    on_hand_qty = fields.Float(
        string='Stok Saat Ini', readonly=True)
    incoming_qty = fields.Float(
        string='Incoming (PO)', readonly=True)
    sales_order_qty = fields.Float(
        string='Backorder', readonly=True)
    average_monthly_sales = fields.Float(
        string='Average Month', readonly=True)
    target_stock_qty = fields.Float(
        string='Target Stok', readonly=True)
    recommended_purchase_qty = fields.Float(
        string='Planning Pembelian', readonly=True,
        help='Target stok dikurangi proyeksi stok, dibulatkan ke kelipatan '
             'Isi Per Dus.')
    months_of_stock = fields.Float(
        string='Ketahanan Stok', readonly=True)
    future_months_of_stock = fields.Float(
        string='Ketahanan Stok + PO', readonly=True)
    vendor_id = fields.Many2one(
        'res.partner', string='Vendor Pilihan', readonly=True)

    coverage_months = fields.Integer(
        string='Ketahanan Bulan', readonly=True)

    has_components = fields.Boolean(
        string='Punya Komponen', readonly=True,
        help='Technical: dipakai tombol Components supaya tidak perlu '
             'menghitung jumlah komponen. True bila produk punya BoM aktif.')

    isi_perdus = fields.Float(
        string='Isi Per Dus', readonly=True,
        compute='_compute_isi_perdus')

    moq_qty = fields.Float(
        string='MoQ',
        help='Minimum order quantity (input manual). Kosongkan bila produk '
             'tidak memiliki MoQ.')
    final_qty = fields.Float(
        string='Final Quantity',
        help='Jumlah akhir yang ingin dibeli. Purchase order akan dibuat '
             'berdasarkan angka ini. Baris dengan Final Quantity > 0 yang '
             'ikut dibuatkan purchase order.')

    # ------------------------------------------------------------------ #
    # Computed helpers
    # ------------------------------------------------------------------ #

    @api.depends('product_id')
    def _compute_internal_category(self):
        has_field = 'internal_category' in self.env['product.product']._fields
        for rec in self:
            if has_field and rec.product_id:
                rec.internal_category_id = rec.product_id.internal_category
            else:
                rec.internal_category_id = False

    @api.depends('product_id')
    def _compute_product_brand(self):
        has_field = 'product_brand' in self.env['product.product']._fields
        for rec in self:
            if has_field and rec.product_id:
                rec.product_brand_id = rec.product_id.product_brand
            else:
                rec.product_brand_id = False

    @api.depends('product_id')
    def _compute_isi_perdus(self):
        has_field = 'isi_perdus' in self.env['product.product']._fields
        for rec in self:
            rec.isi_perdus = rec.product_id.isi_perdus if has_field and rec.product_id else 0.0

    def action_view_components(self):
        """Open the read-only BOM popup for this line's product.

        Plan quantities default to per-unit need times this line's Final
        (falling back to recommended), and can be added back into the
        analysis from the popup.
        """
        self.ensure_one()
        bom = self.env['fky.stock.analysis.service'].get_first_bom(
            self.product_id)
        wizard = self.env['fky.stock.bom.wizard'].create({
            'product_id': self.product_id.id,
            'company_id': self.company_id.id,
            'bom_id': bom.display_name if bom else '',
            'final_qty': self.final_qty or 0.0,
            'preview_line_id': self.id,
            'line_ids': self.env['fky.stock.bom.wizard']._component_line_vals(
                bom, self.company_id),
        })
        return {
            'name': 'Product Components',
            'type': 'ir.actions.act_window',
            'res_model': 'fky.stock.bom.wizard',
            'view_mode': 'form',
            'views': [(self.env.ref(
                'fky_stock_management.view_fky_stock_bom_wizard_form'
            ).id, 'form')],
            'res_id': wizard.id,
            'target': 'new',
            'context': {'form_view_initial_mode': 'edit'},
        }
