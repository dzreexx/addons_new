# -*- coding: utf-8 -*-
from odoo import api, fields, models


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    raw_material_ids = fields.Many2many(
        'product.template',
        'fky_product_raw_material_rel',
        'finished_product_id',
        'raw_material_id',
        string='Bahan Baku',
        help='Material mentah untuk produk jadi ini. Otomatis: M- + kode '
             'sama (mis. M-X untuk FG X). Manual: tambah di sini bila kode '
             'tidak sama (mis. M-Y untuk FG X). Preview + saved report '
             'menghitung min(stok/rasio BoM); dashboard tetap sum. '
             'Kosongkan bila tidak ada.')
    raw_material_auto_filled = fields.Boolean(
        string='Bahan Baku Auto-Filled',
        copy=False,
        help='Technical: penanda auto M- + kode sama. Isian manual '
             '(mis. M-Y) digabung dan tidak pernah dihapus auto.')
    equivalent_product_ids = fields.Many2many(
        'product.template',
        'fky_product_equivalent_rel',
        'product_id',
        'equivalent_id',
        string='Produk Setara / Kombinasi',
        help='Produk jadi lain yang digabung ke total stok produk ini '
             '(mis. A setara B). On-hand fisiknya dijumlah 1:1 tanpa rasio '
             'BoM, hanya di Preview Analisa + Saved Report (dashboard '
             'tetap stok fisik murni). Kosongkan bila tidak ada.')

    def _sync_raw_materials(self, records, old_codes=None):
        """Run the shared auto-fill without hard-depending on the service."""
        service = self.env.get('fky.stock.analysis.service')
        if service is not None:
            service._sync_raw_materials_for_products(
                records, old_codes=old_codes)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        if not self.env.context.get('fky_autofill'):
            self._sync_raw_materials(records)
        return records

    def write(self, vals):
        old_codes = (
            {rec.id: rec.default_code for rec in self}
            if 'default_code' in vals else {})
        res = super().write(vals)
        if ('raw_material_ids' in vals
                and not self.env.context.get('fky_autofill')):
            # Manual edit wins: drop the auto marker so a later BoM change
            # never wipes the user's own selection.
            stale = self.filtered('raw_material_auto_filled')
            if stale:
                stale.write({'raw_material_auto_filled': False})
        if old_codes and not self.env.context.get('fky_autofill'):
            self._sync_raw_materials(self, old_codes=old_codes)
        return res