# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class StockAnalysisLine(models.Model):
    """Persistent rows of a saved stock purchase analysis run.

    Rows belong to :model:`fky.stock.analysis.report` so a generated report
    can be reopened later with its manual MoQ / Final Qty / coverage edits
    still in place.
    """

    _name = 'fky.stock.analysis.line'
    _description = 'Stock Purchase Analysis Result Line'
    _order = 'months_of_stock asc, average_monthly_sales desc'

    report_id = fields.Many2one(
        'fky.stock.analysis.report', string='Report', ondelete='cascade')

    company_id = fields.Many2one(
        'res.company', string='Perusahaan', readonly=True)
    product_id = fields.Many2one(
        'product.product', string='Product Name', required=True, readonly=True)

    default_code = fields.Char(string='Code', related='product_id.default_code')
    product_name = fields.Char(string='Product Name', related='product_id.display_name')

    internal_category_id = fields.Many2one(
        'internal.category', string='Internal Category', readonly=True,
        compute='_compute_internal_category', store=True,
        help='Kategori internal produk (mis. carburator, CDI, as gear depan).')

    product_brand_id = fields.Many2one(
        'product.brand', string='Brand', readonly=True,
        compute='_compute_product_brand', store=True,
        help='Brand produk dari master produk.')

    on_hand_qty = fields.Float(
        string='Stok Saat Ini', readonly=True,
        help='Total stok fisik produk di semua lokasi internal perusahaan.')
    incoming_qty = fields.Float(
        string='Incoming (PO)', readonly=True,
        help='Pembelian Barang yang belum diterima')
    sales_order_qty = fields.Float(
        string='Backorder', readonly=True,
        help='Penjualan yang belum terkirim.')
    average_monthly_sales = fields.Float(
        string='Average Month', readonly=True,
        help='Rata-rata penjualan per bulan.')
    target_stock_qty = fields.Float(
        string='Target Stok', readonly=True,
        help='average month * ketahanan bulan')
    recommended_purchase_qty = fields.Float(
        string='Planning Pembelian', readonly=True,
        help='Jumlah yang perlu dibeli = target stok dikurangi proyeksi stok '
             '(stok saat ini + incoming - sisa permintaan), lalu dibulatkan '
             'ke kelipatan Isi Per Dus. 0 bila sudah cukup.')
    months_of_stock = fields.Float(
        string='Ketahanan Stok', readonly=True,
        help='Berapa bulan stok saat ini bertahan terhadap penjualan, '
             'tanpa memperhitungkan incoming.')
    future_months_of_stock = fields.Float(
        string='Ketahanan Stok + PO', readonly=True,
        help='Berapa bulan stok bertahan setelah memperhitungkan incoming (PO).')
    vendor_id = fields.Many2one(
        'res.partner', string='Vendor Pilihan', readonly=True,
        help='Vendor prioritas (sequence terendah) untuk produk ini.')

    coverage_months = fields.Integer(
        string='Ketahanan Bulan', readonly=True,
        help='Target ketahanan stok (dalam bulan) yang dipakai untuk produk '
             'ini. Bersumber dari pengaturan per produk / per kategori '
             'internal, atau nilai default report.')

    isi_perdus = fields.Float(
        string='Isi Per Dus', readonly=True,
        compute='_compute_isi_perdus', store=True,
        help='Jumlah unit per dus dari master produk. Planning Pembelian '
             'dibulatkan ke kelipatan angka ini.')

    has_components = fields.Boolean(
        string='Punya Komponen', readonly=True,
        help='Technical: dipakai tombol Components supaya tidak perlu '
             'menghitung jumlah komponen. True bila produk punya BoM aktif.')

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
        """Open the plannable BOM popup for this report line.

        Plan quantities default to zero and can be added back into the
        report via Add to Report (reports are reused again and again).
        """
        self.ensure_one()
        bom = self.env['fky.stock.analysis.service'].get_first_bom(
            self.product_id)
        wizard = self.env['fky.stock.bom.wizard'].create({
            'product_id': self.product_id.id,
            'company_id': self.company_id.id,
            'bom_id': bom.display_name if bom else '',
            'final_qty': self.final_qty or 0.0,
            'report_line_id': self.id,
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

    # ------------------------------------------------------------------ #
    # Purchase order generation
    # ------------------------------------------------------------------ #

    def action_create_purchase_order(self):
        """Create a single draft Purchase Order from the selected lines.

        Only lines with a ``final_qty`` greater than zero are considered;
        MoQ is informational only.
        """
        lines = self.filtered(lambda line: line.final_qty > 0.0)
        return self._create_purchase_order_from_lines(lines)

    @api.model
    def _create_purchase_order_from_lines(self, lines):
        """Build a draft PO from any recordset exposing the line contract.

        ``lines`` may be persistent :model:`fky.stock.analysis.line` records
        or transient preview rows: only ``product_id``, ``final_qty``,
        ``vendor_id`` and ``company_id`` are read.
        """
        Purchase = self.env.get('purchase.order')
        if Purchase is None:
            raise UserError(
                _('The Purchase module is not installed, cannot create a '
                  'purchase order.'))

        if not lines:
            raise UserError(
                _('No lines with a final quantity greater than zero. '
                  'Set a Final Quantity on at least one row.'))

        company = lines[0].company_id
        vendor = lines.mapped('vendor_id').filtered('id')[:1]
        if not vendor:
            raise UserError(
                _('None of the selected lines has a preferred vendor. '
                  'Set a supplier for the product(s) (Vendor Pilihan) before '
                  'generating a purchase order.'))

        order = Purchase.with_company(company.id).create({
            'partner_id': vendor.id,
            'company_id': company.id if company else False,
            'origin': 'Stock Purchase Analysis',
        })

        date_planned = fields.Datetime.now()
        line_vals_list = []
        for line in lines:
            product = line.product_id.with_company(company.id)
            seller = product._select_seller(
                partner_id=vendor, quantity=line.final_qty, date=date_planned.date())
            line_vals_list.append({
                'order_id': order.id,
                'product_id': product.id,
                'product_qty': line.final_qty,
                'product_uom': product.uom_id.id,
                'price_unit': seller.price if seller else 0.0,
                'date_planned': date_planned,
            })
        self.env['purchase.order.line'].create(line_vals_list)

        return {
            'name': _('Request for Quotation (Draft)'),
            'type': 'ir.actions.act_window',
            'res_model': 'purchase.order',
            'view_mode': 'form',
            'res_id': order.id,
        }
