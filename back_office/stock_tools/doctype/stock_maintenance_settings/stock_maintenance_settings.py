# Copyright (c) 2025, stock repost and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import today


class StockMaintenanceSettings(Document):
	def validate(self):
		# Set default dates if not provided
		if not self.start_date:
			self.start_date = "2000-01-01"
		if not self.end_date:
			self.end_date = today()
