# -*- coding: utf-8 -*-


def uninstall_hook(cr, registry):
    """Clean up structures Odoo's own uninstaller will not remove.

    - Orphan M2M tables created while the `internal.category` comodel was
      unresolved (`_unknown`); they are not tracked in ir_model_relation and
      would survive a plain uninstall.
    - The module's own menu tree.  `_module_data_uninstall` refuses to delete
      a menu whose children are also scheduled for deletion (restricted FK),
      so the whole menu subtree would be left behind.
    """
    # 1) Drop orphan M2M relation tables.
    tables = [
        '_unknown_fky_stock_analysis_wizard_rel',
        '_unknown_fky_stock_analysis_report_rel',
        '_unknown_fky_stock_analysis_preview_rel',
        'fky_stock_analysis_wizard_internal_category_rel',
        'fky_stock_analysis_report_internal_category_rel',
        'fky_stock_analysis_preview_internal_category_rel',
        'fky_stock_analysis_wizard_res_partner_rel',
        'fky_stock_analysis_report_res_partner_rel',
        'fky_stock_analysis_preview_res_partner_rel',
    ]
    cr.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = current_schema()
    """)
    existing = {row[0] for row in cr.fetchall()}
    for table in tables:
        if table in existing:
            cr.execute('DROP TABLE IF EXISTS %s CASCADE' % table)

    # 2) Delete the module's menus (children first, then parents) using raw
    #    SQL.  Using the ORM's unlink() is not safe here: other modules (e.g.
    #    simplify_access_management) override ir.ui.menu.search and require an
    #    HTTP request, which does not exist during a module (un)install.
    cr.execute(
        "SELECT res_id FROM ir_model_data "
        "WHERE module = 'fky_stock_management' AND model = 'ir.ui.menu'")
    menu_ids = [row[0] for row in cr.fetchall()]
    if menu_ids:
        cr.execute(
            "DELETE FROM ir_ui_menu "
            "WHERE id IN %s AND parent_id IN %s",
            (tuple(menu_ids), tuple(menu_ids)))
        cr.execute(
            "DELETE FROM ir_ui_menu WHERE id IN %s",
            (tuple(menu_ids),))
        cr.execute(
            "DELETE FROM ir_model_data "
            "WHERE module = 'fky_stock_management' AND model = 'ir.ui.menu'")


def post_init_hook(cr, registry):
    """Prefill bahan baku from 1:1 BoMs (exactly one component line)."""
    from odoo import api, SUPERUSER_ID
    env = api.Environment(cr, SUPERUSER_ID, {})
    service = env['fky.stock.analysis.service']
    if hasattr(service, '_auto_fill_raw_materials'):
        filled = service._auto_fill_raw_materials()
        if filled:
            env.cr.commit()
