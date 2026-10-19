# -*- coding: utf-8 -*-
"""Live bahan baku auto-fill: keep the 1:1 BoM mapping current on BoM saves.

The install-time / migration-time backfill only covers BoMs that already
exist. These hooks re-run the targeted fill whenever a BoM or its lines
change, so a product gets its bahan baku the moment a 1:1 BoM is created
and loses it again when the BoM grows past one component.
"""
from odoo import api, models


class MrpBom(models.Model):
    _inherit = 'mrp.bom'

    def _fky_autofill_raw_materials(self):
        self.env['fky.stock.analysis.service']._fill_raw_from_boms(self)

    @api.model_create_multi
    def create(self, vals_list):
        boms = super().create(vals_list)
        boms._fky_autofill_raw_materials()
        return boms

    def write(self, vals):
        res = super().write(vals)
        if {'type', 'active', 'product_tmpl_id', 'bom_line_ids'} & set(vals):
            self._fky_autofill_raw_materials()
        return res


class MrpBomLine(models.Model):
    _inherit = 'mrp.bom.line'

    @api.model_create_multi
    def create(self, vals_list):
        lines = super().create(vals_list)
        lines.mapped('bom_id')._fky_autofill_raw_materials()
        return lines

    def write(self, vals):
        res = super().write(vals)
        if {'product_id', 'bom_id'} & set(vals):
            self.mapped('bom_id')._fky_autofill_raw_materials()
        return res

    def unlink(self):
        boms = self.mapped('bom_id')
        res = super().unlink()
        boms._fky_autofill_raw_materials()
        return res
