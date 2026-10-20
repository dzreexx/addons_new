# -*- coding: utf-8 -*-
import logging

_logger = logging.getLogger(__name__)


def uninstall_hook(cr, registry):
    """Remove the menu that Odoo leaves behind during module uninstall."""
    cr.execute(
        """
        DELETE FROM ir_ui_menu
         WHERE id IN (
               SELECT res_id
                 FROM ir_model_data
                WHERE module = 'fky_sales_return_condition_fix'
                  AND model = 'ir.ui.menu'
         )
        """
    )
    cr.execute(
        """
        DELETE FROM ir_model_data
         WHERE module = 'fky_sales_return_condition_fix'
           AND model = 'ir.ui.menu'
        """
    )
    _logger.info("fky_sales_return_condition_fix: menu removed on uninstall")
