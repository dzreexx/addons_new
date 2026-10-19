# -*- coding: utf-8 -*-
import base64
import io

from odoo import _, fields, models

try:
    import xlsxwriter
except ImportError:
    xlsxwriter = None


class StockAnalysisReport(models.Model):
    """A saved stock purchase analysis report.

    :model:`fky.stock.analysis.line` rows (with manual MoQ / Final Qty /
    coverage edits) are persisted under this header so the report can be
    reopened and turned into a purchase order at any time.
    """

    _name = 'fky.stock.analysis.report'
    _description = 'Stock Purchase Analysis Report'
    _order = 'create_date desc, id desc'
    _rec_name = 'name'

    name = fields.Char(
        string='Name',
        default=lambda self: _('New'))
    company_id = fields.Many2one(
        'res.company', string='Perusahaan', required=True, readonly=True,
        default=lambda self: self.env.company.id)
    user_id = fields.Many2one(
        'res.users', string='Dibuat oleh', readonly=True,
        default=lambda self: self.env.user.id)
    create_date = fields.Datetime(string='Tanggal', readonly=True)

    average_months = fields.Integer(
        string='Average Month', readonly=True,
        help='Jumlah bulan riwayat penjualan yang dipakai saat generate.')
    stock_coverage_months = fields.Integer(
        string='Ketahanan Bulan (Default)', readonly=True,
        help='Target ketahanan stok default (dipakai bila tidak ada aturan '
             'per produk / kategori).')
    product_ids = fields.Many2many(
        'product.product', string='Filter Product', readonly=True)
    internal_category_ids = fields.Many2many(
        'internal.category', string='Internal Category', readonly=True)

    line_ids = fields.One2many(
        'fky.stock.analysis.line', 'report_id', string='Hasil Analisa')
    line_count = fields.Integer(
        string='Jumlah Baris', compute='_compute_line_count')

    # ------------------------------------------------------------------ #
    # Computed helpers
    # ------------------------------------------------------------------ #

    def _compute_line_count(self):
        for report in self:
            report.line_count = len(report.line_ids)

    # ------------------------------------------------------------------ #
    # Actions
    # ------------------------------------------------------------------ #

    def action_create_purchase_order(self):
        self.ensure_one()
        return self.line_ids.action_create_purchase_order()

    def action_generate_final_report(self):
        """Create a NEW Final Report snapshot from this report (same title)."""
        self.ensure_one()
        final = self.env['fky.stock.analysis.final.report'].create_from_report(
            self)
        return {
            'name': _('Final Report'),
            'type': 'ir.actions.act_window',
            'res_model': 'fky.stock.analysis.final.report',
            'view_mode': 'form',
            'res_id': final.id,
        }

    def action_regenerate(self):
        """Re-run the analysis live, rebuilding this report's lines.

        Manual MoQ values are carried over into the new preview; Final
        quantities restart from zero. The report itself is reused.
        """
        self.ensure_one()
        wizard = self.env['fky.stock.analysis.wizard'].create({
            'company_id': self.company_id.id,
            'average_months': self.average_months,
            'stock_coverage_months': self.stock_coverage_months,
            'product_ids': [(6, 0, self.product_ids.ids)],
            'target_report_id': self.id,
        })
        return {
            'name': _('Regenerate Report'),
            'type': 'ir.actions.act_window',
            'res_model': 'fky.stock.analysis.wizard',
            'view_mode': 'form',
            'res_id': wizard.id,
            'target': 'new',
            'context': {'form_view_initial_mode': 'edit'},
        }

    def action_view_lines(self):
        """Open the report lines in a searchable list view."""
        self.ensure_one()
        return {
            'name': _('Analysis Lines'),
            'type': 'ir.actions.act_window',
            'res_model': 'fky.stock.analysis.line',
            'view_mode': 'tree',
            'domain': [('report_id', '=', self.id)],
            'view_id': self.env.ref(
                'fky_stock_management.fky_stock_analysis_line_tree').id,
            'search_view_id': self.env.ref(
                'fky_stock_management.fky_stock_analysis_line_search').id,
        }

    # ------------------------------------------------------------------ #
    # Excel export
    # ------------------------------------------------------------------ #

    def action_export_excel(self):
        self.ensure_one()
        if not xlsxwriter:
            from odoo.exceptions import ValidationError
            raise ValidationError(
                _('The xlsxwriter library is not available in this environment.'))

        lines = self.line_ids
        buffer = io.BytesIO()
        workbook = xlsxwriter.Workbook(buffer)
        sheet = workbook.add_worksheet('Analysis')

        title_style = workbook.add_format({'bold': True})
        money_style = workbook.add_format({'num_format': '#,##0'})

        headers = [
            'Internal Category', 'Brand', 'Code', 'Product Name', 'Stok Saat Ini',
            'Incoming (PO)', 'Backorder', 'Average Month', 'Ketahanan Bulan',
            'Target Stok', 'Planning Pembelian', 'Isi Per Dus',
            'MoQ', 'Final Quantity', 'Ketahanan Stok',
            'Ketahanan Stok + PO', 'Vendor Pilihan',
        ]
        for col, header in enumerate(headers):
            sheet.write(0, col, header, title_style)

        row = 1
        for line in lines:
            sheet.write(row, 0, line.internal_category_id.name or '')
            sheet.write(row, 1, line.product_brand_id.name or '')
            sheet.write(row, 2, line.default_code or '')
            sheet.write(row, 3, line.product_name or '')
            sheet.write_number(row, 4, line.on_hand_qty, money_style)
            sheet.write_number(row, 5, line.incoming_qty, money_style)
            sheet.write_number(row, 6, line.sales_order_qty, money_style)
            sheet.write_number(row, 7, line.average_monthly_sales, money_style)
            sheet.write_number(row, 8, line.coverage_months, money_style)
            sheet.write_number(row, 9, line.target_stock_qty, money_style)
            sheet.write_number(row, 10, line.recommended_purchase_qty, money_style)
            sheet.write_number(row, 11, line.isi_perdus, money_style)
            sheet.write_number(row, 12, line.moq_qty, money_style)
            sheet.write_number(row, 13, line.final_qty, money_style)
            sheet.write_number(row, 14, line.months_of_stock, money_style)
            sheet.write_number(row, 15, line.future_months_of_stock, money_style)
            sheet.write(row, 16, line.vendor_id.name or '')
            row += 1

        workbook.close()
        buffer.seek(0)
        data = base64.b64encode(buffer.getvalue())

        attachment = self.env['ir.attachment'].sudo().create({
            'name': '%s.xlsx' % (self.name or 'stock_purchase_analysis'),
            'datas': data,
            'mimetype': 'application/vnd.openxmlformats-officedocument.'
                        'spreadsheetml.sheet',
        })

        return {
            'name': _('Download Report Excel'),
            'type': 'ir.actions.act_url',
            'url': '/web/content/%s?download=true' % attachment.id,
            'target': 'new',
        }