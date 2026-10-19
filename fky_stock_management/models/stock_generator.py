# -*- coding: utf-8 -*-
from odoo import _, api, fields, models

_ALLOWED_LOCATIONS = ['TKADR/Stock', 'GDADR/Stock', 'TKANA/Stock', 'GDANA/Stock']


class StockRuleGenerator(models.TransientModel):
    _name = 'fky.stock.rule.generator'
    _description = 'Generate Stock Alert Rules'

    location_ids = fields.Many2many(
        'stock.location',
        string='Locations',
        required=True,
        domain=[('complete_name', 'in', _ALLOWED_LOCATIONS)],
    )

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            if not rec.location_ids:
                rec.location_ids = self.env['stock.location'].search([
                    ('complete_name', 'in', _ALLOWED_LOCATIONS),
                ])
        return records

    def _dashboard_views(self, dashboard):
        """Return ``[[view_id, 'tree'], [view_id, 'form']]`` for the action.

        Read straight off the action record so we avoid ``_for_xml_id``
        (which computes ``search_view`` → ``fields_view_get`` and blows up
        under ``simplify_access_management`` outside an HTTP request).
        Falls back to the module's own tree/form views when the action or
        its view ids are unavailable, so the chained action ALWAYS carries a
        usable ``views`` list.
        """
        default = [
            ('fky_stock_management.view_stock_rule_tree', 'tree'),
            ('fky_stock_management.view_stock_rule_form', 'form'),
        ]
        allowed = ('tree', 'form')
        result = []
        if dashboard:
            for view in dashboard.view_ids:
                if view.view_mode in allowed:
                    result.append([view.view_id.id, view.view_mode])
        if not result:
            for xid, vtype in default:
                view = self.env.ref(xid, raise_if_not_found=False)
                if view:
                    result.append([view.id, vtype])
        return result

    def action_generate_rules(self):
        self.ensure_one()
        # Exclusion list lives ONLY in the Settings singleton now: the wizard
        # reads it, never writes it. Previously the wizard kept a private
        # copy and pushed it onto the config on Generate, which made the two
        # silently overwrite each other.
        config = self.env['fky.stock.config'].sudo()._get()
        excluded = config.exclude_product_ids

        products = self.env['product.product'].search([
            ('active', '=', True),
        ]) - excluded

        # Rules of a now-excluded product are removed, not merely skipped:
        # the exclusion list is the user's single control for "this product
        # must not appear on the dashboard", and historically old rules kept
        # showing up after a product was excluded (user decision 2026-10-13).
        # Scoped to the locations selected in this wizard, so an exclusion
        # run for one location never wipes rules belonging to another.
        removed = 0
        if excluded:
            stale = self.env['fky.stock.rule'].search([
                ('product_id', 'in', excluded.ids),
                ('location_id', 'in', self.location_ids.ids),
            ])
            if stale:
                removed = len(stale)
                stale.unlink()

        created = 0
        for location in self.location_ids:
            existing_rules = self.env['fky.stock.rule'].search([
                ('location_id', '=', location.id),
            ])
            existing_product_ids = set(existing_rules.mapped('product_id.id'))

            vals_list = []
            for product in products:
                if product.id not in existing_product_ids:
                    min_qty = product.isi_perdus if (hasattr(product, 'isi_perdus') and product.isi_perdus > 0.0) else 1.0
                    vals_list.append({
                        'product_id': product.id,
                        'location_id': location.id,
                        'min_qty': min_qty,
                    })

            if vals_list:
                self.env['fky.stock.rule'].create(vals_list)
                created += len(vals_list)

        # Report the outcome on the dashboard we are about to open: the
        # wizard's own return value is the dashboard action, so a
        # display_notification returned directly here would never render.
        # A silent deletion of dashboard rows would be alarming, and a
        # silent skip leaves the user wondering why an excluded product
        # still shows up.
        if removed:
            summary = _('%s rule(s) created, %s rule(s) removed for '
                        'excluded products.') % (created, removed)
        else:
            summary = _('%s rule(s) created.') % created

        # Chain to the dashboard action. A nested ``params.next`` window
        # action is NOT passed through the server's ``_preprocessAction``,
        # so the browser-side ``_preprocessAction`` receives it verbatim and
        # raises "action.views is not iterable" unless a real ``views`` list
        # is present (``view_mode`` alone is not enough in that position).
        #
        # We deliberately do NOT use ``_for_xml_id``: reading that record
        # triggers a compute of ``search_view`` → ``fields_view_get``, which
        # ``simplify_access_management`` overrides with a ``request``-based
        # lookup that raises "object unbound" outside an HTTP request. We
        # only need the view ids, so we build the action by hand and read
        # them off the stored action record.
        action = {
            'type': 'ir.actions.act_window',
            'name': _('Stock & Purchase Dashboard'),
            'res_model': 'fky.stock.rule',
            'view_mode': 'tree,form',
            'domain': [],
            'target': 'current',
        }
        dashboard = self.env.ref(
            'fky_stock_management.action_stock_rule', raise_if_not_found=False)
        views = self._dashboard_views(dashboard)
        if views:
            action['views'] = views
            action['view_mode'] = ','.join(v[1] for v in views)
        # Merge default filters on top of the dashboard action's own context
        # (which already sets hide_snoozed + needs_attention).
        # NOTE: the search view names the filter `urgent_stock` — the old
        # `search_default_urgent` silently matched nothing, so the Urgent
        # filter never applied. Use the real name now.
        context = {'search_default_hide_snoozed': 1,
                   'search_default_urgent_stock': 1,
                   'search_default_best_seller': 1}
        if dashboard and dashboard.context:
            stored = dashboard.context
            if isinstance(stored, dict):
                merged = dict(stored)
                merged.update(context)
                context = merged
        action['context'] = context
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Generate Rules'),
                'message': summary,
                'type': 'success',
                'sticky': False,
                'next': action,
            },
        }