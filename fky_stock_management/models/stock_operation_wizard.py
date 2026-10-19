# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class StockOperationLine(models.TransientModel):
    _name = 'fky.stock.operation.line'
    _description = 'Stock Alert Operation Line'

    wizard_id = fields.Many2one(
        'fky.stock.operation.wizard',
        string='Wizard',
        ondelete='cascade',
    )

    rule_id = fields.Many2one(
        'fky.stock.rule',
        string='Alert Rule',
        readonly=True,
    )

    product_id = fields.Many2one(
        'product.product',
        related='rule_id.product_id',
        string='Product',
        readonly=True,
    )

    product_sku = fields.Char(
        related='product_id.default_code',
        string='SKU',
        readonly=True,
    )

    company_id = fields.Many2one(
        'res.company',
        related='rule_id.company_id',
        string='Company',
        readonly=True,
    )

    has_bom = fields.Boolean(
        string='Has BOM',
        compute='_compute_bom',
    )

    bom_ref = fields.Char(
        string='BOM Reference',
        compute='_compute_bom',
    )

    move_qty = fields.Float(
        string='Quantity',
        required=True,
    )

    @api.depends('rule_id')
    def _compute_bom(self):
        Bom = self.env.get('mrp.bom')
        for rec in self:
            bom = rec.rule_id._get_bom() if rec.rule_id else Bom
            rec.has_bom = bool(bom)
            rec.bom_ref = bom.display_name if bom else False


class StockOperationWizard(models.TransientModel):
    _name = 'fky.stock.operation.wizard'
    _description = 'Create Draft Mutasi / Manufacturing Orders'

    rule_ids = fields.Many2many(
        'fky.stock.rule',
        string='Alert Rules',
        readonly=True,
    )

    line_ids = fields.One2many(
        'fky.stock.operation.line',
        'wizard_id',
        string='Products',
    )

    operation = fields.Selection([
        ('mutasi', 'Mutasi'),
        ('manufacture', 'Manufacture'),
    ], string='Operation', default='mutasi', required=True,
        help='Mutasi creates draft internal transfers; Manufacture creates '
             'draft manufacturing orders (every product must have a BOM).')

    source_location_id = fields.Many2one(
        'stock.location',
        string='Source Location',
        domain=[('complete_name', 'in', ['TKADR/Stock', 'GDADR/Stock', 'TKANA/Stock', 'GDANA/Stock'])],
        help=('Source of the mutasi (e.g. the other warehouse stock). '
              'Mutasi runs GD/Stock > Transit > Toko (and vice versa).'),
    )

    qty_mode = fields.Selection([
        ('dus', 'Per Dus'),
        ('manual', 'Manual Quantity'),
    ], string='Quantity Mode', default='dus', required=True)

    qty_value = fields.Float(
        string='Quantity',
        default=1.0,
        help='Number of dus (Per Dus) or number of units (Manual Quantity).',
    )

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            if not rec.line_ids:
                rec._update_lines()
            if rec.line_ids and all(l.has_bom for l in rec.line_ids):
                rec.operation = 'manufacture'
            else:
                rec.operation = 'mutasi'
        return records

    @api.onchange('qty_mode', 'qty_value')
    def _onchange_quantity(self):
        for line in self.line_ids:
            line.move_qty = self._qty_to_units(self.qty_mode, self.qty_value, line.product_id)

    def _qty_to_units(self, mode, value, product):
        if mode == 'dus':
            per_dus = product.isi_perdus if (hasattr(product, 'isi_perdus') and product.isi_perdus) else 1.0
            return (value or 0.0) * per_dus
        return value or 0.0

    def _update_lines(self):
        """(Re)build the product lines from the selected checklist rules."""
        self.ensure_one()
        vals_list = []
        for rule in self.rule_ids:
            product = rule.product_id
            qty = self._qty_to_units(self.qty_mode, self.qty_value, product)
            vals_list.append((0, 0, {
                'rule_id': rule.id,
                'move_qty': qty,
            }))
        self.line_ids = vals_list

    # ------------------------------------------------------------------ #
    # Picking / picking-type helpers
    # ------------------------------------------------------------------ #

    def _get_mutasi_picking_type(self, company, stock_loc):
        """Mutasi picking type anchored to a warehouse stock location."""
        Ptype = self.env['stock.picking.type']
        types = Ptype.search([
            ('code', '=', 'internal'),
            ('company_id', '=', company.id),
            ('name', 'ilike', 'mutasi'),
        ])
        anchored = types.filtered(lambda t: t.default_location_src_id.id == stock_loc.id)
        return anchored[:1] or types[:1]

    def _get_transit_location(self, dest):
        """The destination warehouse's transit location (e.g. TKADR/Transit)."""
        parent = dest.location_id
        if parent:
            transit = self.env['stock.location'].search([
                ('location_id', '=', parent.id),
                ('usage', '=', 'internal'),
                ('name', 'ilike', 'transit'),
            ], limit=1)
            if transit:
                return transit
        return self.env['stock.location'].search([
            ('usage', '=', 'internal'),
            ('company_id', '=', dest.company_id.id),
            ('complete_name', 'ilike', '/Transit'),
        ], limit=1)

    def _create_mutasi(self, source, dest, lines):
        """Create draft mutasi picking(s)."""
        company = dest.company_id
        pickings = self.env['stock.picking']
        source_type = self._get_mutasi_picking_type(company, source)
        if not source_type:
            raise UserError(_('No Mutasi picking type is available for %s.') % company.name)

        if source.warehouse_id and dest.warehouse_id and source.warehouse_id.id != dest.warehouse_id.id:
            transit = self._get_transit_location(dest)
            if not transit:
                raise UserError(_('No transit location found for warehouse %s.') % dest.warehouse_id.name)
            dest_type = self._get_mutasi_picking_type(company, dest)
            if not dest_type:
                raise UserError(_('No Mutasi picking type is available for %s.') % company.name)

            picking1 = self.env['stock.picking'].create({
                'picking_type_id': source_type.id,
                'location_id': source.id,
                'location_dest_id': transit.id,
                'company_id': company.id if company else False,
                'origin': 'Stock Alert: Mutasi',
            })
            picking2 = self.env['stock.picking'].create({
                'picking_type_id': dest_type.id,
                'location_id': transit.id,
                'location_dest_id': dest.id,
                'company_id': company.id if company else False,
                'origin': picking1.name,
            })
            pickings = picking1 | picking2
            self._create_moves(picking1, source, transit, company, lines)
            self._create_moves(picking2, transit, dest, company, lines)
        else:
            picking = self.env['stock.picking'].create({
                'picking_type_id': source_type.id,
                'location_id': source.id,
                'location_dest_id': dest.id,
                'company_id': company.id if company else False,
                'origin': 'Stock Alert: Mutasi',
            })
            pickings = picking
            self._create_moves(picking, source, dest, company, lines)
        return pickings

    def _create_moves(self, picking, src, dst, company, lines):
        move_vals_list = []
        for line in lines:
            product = line.product_id
            move_vals_list.append({
                'name': product.display_name,
                'product_id': product.id,
                'product_uom': product.uom_id.id,
                'product_uom_qty': line.move_qty,
                'location_id': src.id,
                'location_dest_id': dst.id,
                'company_id': company.id if company else False,
                'picking_id': picking.id,
                'picking_type_id': picking.picking_type_id.id,
            })
        self.env['stock.move'].create(move_vals_list)

    def _get_manufacture_picking_type(self, company, stock_loc):
        """The 'Manufacturing' (mrp_operation) picking type of the company."""
        Ptype = self.env['stock.picking.type']
        types = Ptype.search([
            ('code', '=', 'mrp_operation'),
            ('company_id', '=', company.id),
        ])
        anchored = types.filtered(lambda t: t.default_location_src_id.id == stock_loc.id)
        return anchored[:1] or types[:1]

    def _create_manufacture(self, line):
        Production = self.env.get('mrp.production')
        if Production is None:
            return False
        product = line.product_id
        company = line.company_id
        warehouse = self.env['stock.warehouse'].search([('company_id', '=', company.id)], limit=1)
        lot_stock = warehouse.lot_stock_id if warehouse else False
        picking_type = self._get_manufacture_picking_type(company, lot_stock)

        production = Production.create({
            'product_id': product.id,
            'product_qty': line.move_qty,
            'product_uom_id': product.uom_id.id,
            'bom_id': line.rule_id._get_bom().id,
            'picking_type_id': picking_type.id if picking_type else False,
            'location_src_id': (picking_type.default_location_src_id or lot_stock).id,
            'location_dest_id': (picking_type.default_location_dest_id or lot_stock).id,
            'company_id': company.id if company else False,
            'origin': 'Stock Alert: Manufacture',
        })

        try:
            raw_values = production._get_moves_raw_values()
            if raw_values:
                production.move_raw_ids = [(0, 0, vals) for vals in raw_values]
            production._create_update_move_finished()
        except Exception:
            pass
        return production

    def action_create_documents(self):
        self.ensure_one()
        if not self.line_ids:
            raise UserError(_('No products selected.'))

        if self.operation == 'mutasi':
            return self._create_mutasi_documents()

        Production = self.env.get('mrp.production')
        if Production is None:
            raise UserError(_('The MRP module is not installed, cannot manufacture.'))
        productions = self.env['mrp.production']
        for line in self.line_ids:
            if line.move_qty <= 0.0:
                continue
            bom = line.rule_id._get_bom()
            if not bom:
                raise UserError(_('Product %s has no BOM, cannot manufacture. '
                                  'Switch the operation back to Mutasi.') %
                                line.product_id.display_name)
            production = self._create_manufacture(line)
            if production:
                productions |= production
        if not productions:
            raise UserError(_('No lines with a quantity greater than zero.'))
        if len(productions) == 1:
            return {
                'name': 'Manufacturing Order (Draft)',
                'type': 'ir.actions.act_window',
                'res_model': 'mrp.production',
                'view_mode': 'form',
                'res_id': productions.id,
                'view_id': self.env.ref('mrp.mrp_production_form_view').id,
            }
        return {
            'name': 'Manufacturing Orders (Draft)',
            'type': 'ir.actions.act_window',
            'res_model': 'mrp.production',
            'view_mode': 'tree,form',
            'domain': [('id', 'in', productions.ids)],
        }

    def _create_mutasi_documents(self):
        if not self.source_location_id:
            raise UserError(_('Please choose a source location for the Mutasi.'))
        source = self.source_location_id
        grouped = {}
        for line in self.line_ids:
            if line.move_qty <= 0.0:
                continue
            dest = line.rule_id.location_id
            grouped.setdefault(dest, []).append(line)
        if not grouped:
            raise UserError(_('No lines with a quantity greater than zero.'))
        pickings = self.env['stock.picking']
        for dest, lines in grouped.items():
            company = dest.company_id
            if company and source.company_id and source.company_id.id != company.id:
                raise UserError(_('Source location (%s) belongs to a different company than '
                                  'destination (%s).') % (source.complete_name, dest.complete_name))
            if source.id == dest.id:
                raise UserError(_('Source and destination locations cannot be the same.'))
            pickings |= self._create_mutasi(source, dest, lines)
        if len(pickings) == 1:
            return {
                'name': 'Mutasi (Draft)',
                'type': 'ir.actions.act_window',
                'res_model': 'stock.picking',
                'view_mode': 'form',
                'res_id': pickings.id,
                'view_id': self.env.ref('stock.view_picking_form').id,
            }
        return {
            'name': 'Mutasi (Draft)',
            'type': 'ir.actions.act_window',
            'res_model': 'stock.picking',
            'view_mode': 'tree,form',
            'domain': [('id', 'in', pickings.ids)],
        }