# -*- coding: utf-8 -*-
from odoo import fields, models


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    raw_material_ids = fields.Many2many(
        'product.template',
        'fky_product_raw_material_rel',
        'finished_product_id',
        'raw_material_id',
        string='Bahan Baku',
        help='Material mentah untuk produk jadi ini. Otomatis: M- + nama '
             'sama (mis. M-X untuk FG X). Manual: tambah di sini bila nama '
             'tidak sama (mis. M-Y untuk FG X). Preview + saved report '
             'menghitung min(stok/rasio BoM); dashboard tetap sum. '
             'Kosongkan bila tidak ada.')
    raw_material_auto_filled = fields.Boolean(
        string='Bahan Baku Auto-Filled',
        copy=False,
        help='Technical: penanda auto M- + nama sama. Isian manual '
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

    def write(self, vals):
        res = super().write(vals)
        if ('raw_material_ids' in vals
                and not self.env.context.get('fky_autofill')):
            # Manual edit wins: drop the auto marker so a later BoM change
            # never wipes the user's own selection.
            stale = self.filtered('raw_material_auto_filled')
            if stale:
                stale.write({'raw_material_auto_filled': False})
        return res