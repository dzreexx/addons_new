# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class StockCoverageRule(models.Model):
    """Saved target stock coverage (ketahanan bulan) per product or.

    per internal category.  These rules are remembered across reports so the
    analysis always uses the right coverage period for each product without
    re-entering it every time.
    """

    _name = 'fky.stock.coverage.rule'
    _description = 'Stock Analysis Coverage Rule'
    _order = 'sequence, id'
    _rec_name = 'name'

    name = fields.Char(
        string='Name', compute='_compute_name', store=True)

    sequence = fields.Integer(string='Order', default=10)
    config_id = fields.Many2one(
        'fky.stock.config', string='Config', ondelete='cascade',
        help='Settings record yang menampilkan aturan ini.')
    company_id = fields.Many2one(
        'res.company', string='Perusahaan',
        default=lambda self: self.env.company.id,
        ondelete='cascade',
    )
    product_id = fields.Many2one(
        'product.product', string='Product',
        help='Produk spesifik yang diberi ketahanan bulan khusus.')
    internal_category_id = fields.Many2one(
        'internal.category', string='Internal Category',
        help='Semua produk dalam kategori internal ini (mis. carburator, '
             'CDI, as gear depan) memakai ketahanan bulan ini.')
    coverage_months = fields.Integer(
        string='Ketahanan Bulan', required=True,
        help='Target ketahanan stok dalam bulan (mis. 3 = stok cukup untuk '
             '3 bulan penjualan rata-rata).')
    active = fields.Boolean(string='Active', default=True)

    _sql_constraints = [
        ('product_company_uniq',
         'unique(product_id, company_id)',
         'A coverage rule already exists for this product!'),
        ('internal_category_company_uniq',
         'unique(internal_category_id, company_id)',
         'A coverage rule already exists for this internal category!'),
    ]

    @api.constrains('product_id', 'internal_category_id')
    def _check_single_target(self):
        for rec in self:
            if bool(rec.product_id) == bool(rec.internal_category_id):
                raise ValidationError(
                    _('Choose exactly one: either a Product or an Internal '
                      'Category per coverage rule.'))

    @api.constrains('coverage_months')
    def _check_coverage_positive(self):
        for rec in self:
            if rec.coverage_months <= 0:
                raise ValidationError(
                    _('Coverage months must be a positive number.'))

    @api.model
    def resolve_coverage_batch(self, products, company=None, fallback=None):
        """Batched coverage resolution — ONE search for many products.

        Returns ``{product_id: coverage_months}`` with priority: product rule
        -> internal-category rule -> fallback (wizard value) -> global config
        default. This is the only resolver: the whole module works on sets of
        products, so a single-record variant would just re-wrap this one.
        """
        config = self.env['fky.stock.config'].sudo()._get()
        default = config.stock_coverage_months or 0
        if fallback is None:
            fallback = default
        result = {p.id: fallback for p in products}
        if not products:
            return result
        company = company or self.env.company
        rules = self.search([
            '|', ('company_id', '=', False),
            ('company_id', '=', company.id),
            ('active', '=', True),
        ])
        has_categ = 'internal_category' in self.env['product.product']._fields
        product_rules = {}
        categ_rules = {}
        for rule in rules:
            if rule.product_id:
                product_rules.setdefault(rule.product_id.id, rule)
            elif rule.internal_category_id:
                categ_rules.setdefault(rule.internal_category_id.id, rule)
        for p in products:
            rule = product_rules.get(p.id)
            if not rule and has_categ and p.internal_category:
                rule = categ_rules.get(p.internal_category.id)
            if rule:
                result[p.id] = rule.coverage_months
        return result

    @api.depends('product_id', 'internal_category_id', 'coverage_months', 'active')
    def _compute_name(self):
        for rec in self:
            if rec.product_id:
                rec.name = '%s: %d bln' % (
                    rec.product_id.display_name, rec.coverage_months)
            elif rec.internal_category_id:
                rec.name = '%s: %d bln' % (
                    rec.internal_category_id.name, rec.coverage_months)
            else:
                rec.name = '%d bln' % rec.coverage_months