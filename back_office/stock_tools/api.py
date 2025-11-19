# Copyright (c) 2025, stock repost and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import today

try:
	from erpnext.controllers.stock_controller import create_repost_item_valuation_entry
except ImportError:
	create_repost_item_valuation_entry = None


@frappe.whitelist()
def repost_and_create_sles(start_date=None, end_date=None, transaction_doctype=None, enqueue=False):
	"""Main API method to recreate SLEs and create repost entries
	
	Args:
		start_date: Start date for transactions (default: 2000-01-01)
		end_date: End date for transactions (default: today)
		transaction_doctype: Filter by specific doctype (optional)
		enqueue: If True, run in background using RQ (default: False)
	"""
	if enqueue:
		# Enqueue the job to run in background
		job = frappe.enqueue(
			"back_office.stock_tools.api._repost_and_create_sles_background",
			start_date=start_date,
			end_date=end_date,
			transaction_doctype=transaction_doctype,
			queue="long",
			timeout=3600,  # 1 hour timeout
			job_name=f"Repost and Create SLEs - {frappe.session.user}"
		)
		return {"job_id": job.id, "status": "queued"}
	else:
		# Run synchronously (original behavior)
		return _repost_and_create_sles_background(start_date, end_date, transaction_doctype)


def _repost_and_create_sles_background(start_date=None, end_date=None, transaction_doctype=None):
	"""Background function to recreate SLEs and create repost entries"""
	try:
		frappe.flags.in_progress = True
		
		# Get settings
		start_date = start_date or "2000-01-01"
		end_date = end_date or today()
		doctype_filter = transaction_doctype if transaction_doctype else None
		
		# Step 1: Recreate Stock Ledger Entries
		frappe.publish_realtime("stock_maintenance_progress", {
			"stage": "Step 1: Recreating Stock Ledger Entries...",
			"progress": 0
		})
		result = _recreate_sles_from_transactions(start_date, end_date, doctype_filter)
		recreated_count = result.get("recreated", 0)
		skipped_count = result.get("skipped", 0)
		
		# Step 2: Create Repost Item Valuation entries
		frappe.publish_realtime("stock_maintenance_progress", {
			"stage": "Step 2: Creating Repost Item Valuation entries...",
			"progress": 50
		})
		repost_count = _create_repost_entries()
		
		# Publish completion
		frappe.publish_realtime("stock_maintenance_progress", {
			"stage": "Completed",
			"progress": 100,
			"recreated_count": recreated_count,
			"skipped_count": skipped_count,
			"repost_count": repost_count
		})
		
		# Log completion
		frappe.logger().info(
			f"Stock Maintenance Repost Completed: "
			f"Recreated {recreated_count} transactions, "
			f"Skipped {skipped_count} transactions, "
			f"Created {repost_count} Repost Item Valuation entries"
		)
		
		return {
			"recreated_count": recreated_count,
			"skipped_count": skipped_count,
			"repost_count": repost_count,
			"status": "completed"
		}
		
	except Exception as e:
		error_msg = str(e)
		frappe.log_error(frappe.get_traceback(), "Stock Maintenance Repost Error")
		frappe.publish_realtime("stock_maintenance_progress", {
			"stage": "Error",
			"error": error_msg,
			"progress": 0
		})
		raise
	finally:
		frappe.flags.in_progress = False
		frappe.db.commit()


def _recreate_sles_from_transactions(start_date, end_date, doctype_filter=None):
	"""Recreate Stock Ledger Entries from transactions in specified order"""
	
	# Define transaction order: Purchase Receipt, Stock Reconciliation, Sales Invoice/Delivery Note, then others
	priority_doctypes = [
		"Purchase Receipt",
		"Stock Reconciliation",
		"Sales Invoice",
		"Delivery Note",
	]
	
	# Get all stock-affecting doctypes
	all_stock_doctypes = _get_stock_affecting_doctypes()
	
	# Filter by doctype_filter if provided
	if doctype_filter:
		all_stock_doctypes = [doctype_filter] if doctype_filter in all_stock_doctypes else []
	
	# Organize doctypes in priority order
	ordered_doctypes = []
	for dt in priority_doctypes:
		if dt in all_stock_doctypes:
			ordered_doctypes.append(dt)
	
	# Add remaining doctypes
	for dt in all_stock_doctypes:
		if dt not in ordered_doctypes:
			ordered_doctypes.append(dt)
	
	total_recreated = 0
	total_skipped = 0
	
	for doctype in ordered_doctypes:
		# Check if DocType exists
		if not frappe.db.exists("DocType", doctype):
			continue
		
		try:
			transactions = _get_transactions(doctype, start_date, end_date)
		except Exception as e:
			# Log error but continue with other doctypes
			error_msg = str(e)
			# Check if it's a table doesn't exist error
			if "doesn't exist" in error_msg.lower() or ("table" in error_msg.lower() and "exist" in error_msg.lower()):
				frappe.msgprint(_("Skipping {0} - table does not exist in database").format(doctype), indicator="orange")
				frappe.log_error(
					title=f"Table does not exist for {doctype}",
					message=f"Error: {error_msg}\n\nDoctype: {doctype}\n\nNote: DocType exists but table is missing. This can happen if the table was never created or is in a different database."
				)
			else:
				frappe.log_error(
					title=f"Error getting transactions for {doctype}",
					message=f"Error: {error_msg}\n\nDoctype: {doctype}"
				)
				frappe.msgprint(_("Skipping {0} - error: {1}").format(doctype, error_msg), indicator="orange")
			continue
		
		if not transactions:
			continue
		
		# Publish progress update
		frappe.publish_realtime("stock_maintenance_progress", {
			"stage": f"Processing {doctype}",
			"doctype": doctype,
			"total": len(transactions),
			"progress": 0
		})
		
		result = _process_transactions(doctype, transactions)
		total_recreated += result.get("recreated", 0)
		total_skipped += result.get("skipped", 0)
		
		frappe.db.commit()
	
	return {
		"recreated": total_recreated,
		"skipped": total_skipped
	}


def _get_stock_affecting_doctypes():
	"""Get list of doctypes that directly create Stock Ledger Entries"""
	return [
		"Stock Entry",
		"Purchase Receipt",
		"Delivery Note",
		"Sales Invoice",
		"Purchase Invoice",
		"Stock Reconciliation",
		"Subcontracting Receipt",
		# Note: Material Request and Work Order don't directly create SLEs
		# They create other documents (Stock Entry, Purchase Order) that create SLEs
	]


def _get_transactions(doctype, start_date, end_date):
	"""Get submitted transactions for a doctype within date range"""
	# Get the proper table name using Frappe's method
	try:
		table_name = frappe.db.get_table_name(doctype)
	except Exception:
		# Fallback to standard format
		table_name = f"tab{doctype}"
	
	# Ensure table name is properly escaped with backticks
	table_name_escaped = f"`{table_name}`"
	
	# Determine which date field to use based on doctype
	# All stock-affecting doctypes use posting_date
	date_field = "posting_date"
	date_field_time = "posting_time"
	
	# For Sales Invoice and Purchase Invoice, only get documents where update_stock = 1
	# These doctypes only update stock when update_stock is checked
	additional_filter = ""
	if doctype in ["Sales Invoice", "Purchase Invoice"]:
		additional_filter = "AND update_stock = 1"
	
	# Build the query based on available date fields
	if date_field_time:
		query = """
			SELECT name, {date_field} as posting_date, {date_field_time} as posting_time
			FROM {table_name}
			WHERE docstatus = 1
				AND {date_field} >= %s
				AND {date_field} <= %s
				{additional_filter}
			ORDER BY {date_field} ASC, {date_field_time} ASC, creation ASC
		""".format(
			table_name=table_name_escaped,
			date_field=date_field,
			date_field_time=date_field_time,
			additional_filter=additional_filter
		)
	else:
		# For doctypes without time field
		query = """
			SELECT name, {date_field} as posting_date, '00:00:00' as posting_time
			FROM {table_name}
			WHERE docstatus = 1
				AND {date_field} >= %s
				AND {date_field} <= %s
				{additional_filter}
			ORDER BY {date_field} ASC, creation ASC
		""".format(
			table_name=table_name_escaped,
			date_field=date_field,
			additional_filter=additional_filter
		)
	
	return frappe.db.sql(query, (start_date, end_date), as_dict=True)


def _process_transactions(doctype, transactions):
	"""Process transactions to recreate SLEs"""
	recreated = 0
	skipped = 0
	errors = []
	
	for idx, trans in enumerate(transactions, 1):
		try:
			# Load the document first to check its state
			doc = frappe.get_doc(doctype, trans.name)
			
			# Check if document has update_stock_ledger method
			if not hasattr(doc, "update_stock_ledger"):
				skipped += 1
				continue
			
			# For Stock Reconciliation and similar doctypes, get items properly
			items = []
			if hasattr(doc, "items") and doc.items:
				items = doc.items
			elif hasattr(doc, "reconciliation_items") and doc.reconciliation_items:
				items = doc.reconciliation_items
			elif hasattr(doc, "item_code") and doc.item_code:
				# Single item document
				items = [doc]
			
			# If no items at all, skip
			if not items:
				skipped += 1
				continue
			
			# For Stock Reconciliation, check if items have qty_diff (the difference that creates SLEs)
			# Stock Reconciliation creates SLEs based on the difference between current_qty and qty
			has_items_needing_sle = False
			for item in items:
				# For Stock Reconciliation: qty_diff = qty - current_qty
				# If qty_diff is 0 or None, no SLE is created
				if doctype == "Stock Reconciliation":
					qty = getattr(item, "qty", None) or 0
					current_qty = getattr(item, "current_qty", None) or 0
					qty_diff = qty - current_qty
					
					# If there's a difference, we need to create SLE
					if qty_diff != 0:
						has_items_needing_sle = True
						break
				else:
					# For other doctypes, check for quantity fields
					qty = None
					if hasattr(item, "qty") and item.qty:
						qty = item.qty
					elif hasattr(item, "quantity") and item.quantity:
						qty = item.quantity
					elif hasattr(item, "transfer_qty") and item.transfer_qty:
						qty = item.transfer_qty
					
					if qty:
						has_items_needing_sle = True
						break
			
			# If no items need SLE creation, skip
			if not has_items_needing_sle:
				skipped += 1
				continue
			
			# Check if SLEs already exist - if they do, skip to avoid duplicates
			existing_sles_count = frappe.db.count(
				"Stock Ledger Entry",
				{
					"voucher_type": doctype,
					"voucher_no": trans.name,
					"is_cancelled": 0
				}
			)
			
			# If SLEs already exist, skip to avoid creating duplicates
			if existing_sles_count > 0:
				continue
			
			# Try to recreate SLEs (only if they don't exist)
			try:
				doc.update_stock_ledger(allow_negative_stock=True)
				recreated += 1
			except Exception as sle_error:
				error_msg = str(sle_error)
				# Check if it's the "No stock ledger entries" error
				if "No stock ledger entries were created" in error_msg:
					# This means items don't have proper quantities/rates
					# For Stock Reconciliation, this might mean qty_diff is 0
					skipped += 1
					# Log first few to understand the pattern
					if skipped <= 10:
						# Get item details for debugging
						item_details = []
						for item in items[:3]:  # First 3 items
							item_info = {
								"item_code": getattr(item, "item_code", "N/A"),
							}
							if doctype == "Stock Reconciliation":
								item_info["qty"] = getattr(item, "qty", None)
								item_info["current_qty"] = getattr(item, "current_qty", None)
								item_info["qty_diff"] = (getattr(item, "qty", 0) or 0) - (getattr(item, "current_qty", 0) or 0)
							else:
								item_info["qty"] = getattr(item, "qty", None) or getattr(item, "quantity", None)
								item_info["valuation_rate"] = getattr(item, "valuation_rate", None)
							item_details.append(item_info)
						
						frappe.log_error(
							title=f"Skipped {doctype} {trans.name} - No SLEs created",
							message=f"Document skipped because no stock ledger entries were created.\n\nError: {error_msg}\n\nItem details: {item_details}\n\nExisting SLEs: {existing_sles_count}"
						)
				else:
					# Other errors should be logged and raised
					raise sle_error
			
			# Progress update every 50 transactions
			if idx % 50 == 0:
				progress_pct = int((idx / len(transactions)) * 100) if transactions else 0
				frappe.publish_realtime(
					"stock_maintenance_progress",
					{
						"stage": f"Processing {doctype}",
						"progress": progress_pct,
						"current": idx,
						"total": len(transactions),
						"doctype": doctype
					}
				)
				frappe.db.commit()
				
		except Exception as e:
			error_msg = f"{doctype} {trans.name}: {str(e)}"
			errors.append(error_msg)
			frappe.log_error(
				title=f"Error recreating SLE for {doctype} {trans.name}",
				message=str(e)
			)
			frappe.db.rollback()
	
	# Only show skipped message if significant number were skipped
	if errors:
		frappe.log_error(
			"\n".join(errors[:10]),  # Log first 10 errors
			"Stock Maintenance - Transaction Errors"
		)
	
	return {
		"recreated": recreated,
		"skipped": skipped
	}


def _create_repost_entries():
	"""Create Repost Item Valuation entries for all item-warehouse combinations"""
	# Get distinct item-warehouse combinations from Bin
	item_warehouses = frappe.db.sql("""
		SELECT DISTINCT item_code, warehouse
		FROM `tabBin`
		WHERE actual_qty != 0
		ORDER BY item_code, warehouse
	""", as_dict=True)
	
	if not item_warehouses:
		return 0
	
	created = 0
	errors = []
	
	for idx, row in enumerate(item_warehouses, 1):
		try:
			# Check if repost entry already exists and is queued
			existing = frappe.db.exists(
				"Repost Item Valuation",
				{
					"item_code": row.item_code,
					"warehouse": row.warehouse,
					"posting_date": "1900-01-01",
					"docstatus": 1,
					"status": ["in", ["Queued", "In Progress"]]
				}
			)
			
			if existing:
				continue  # Skip if already queued
			
			# Create repost entry
			if not create_repost_item_valuation_entry:
				frappe.throw(_("create_repost_item_valuation_entry function not available. Please ensure ERPNext is installed."))
			
			create_repost_item_valuation_entry({
				"based_on": "Item and Warehouse",
				"item_code": row.item_code,
				"warehouse": row.warehouse,
				"posting_date": "1900-01-01",  # Start from beginning
				"posting_time": "00:01",
				"allow_negative_stock": 1,
				"allow_zero_rate": 0
			})
			
			created += 1
			
			# Progress update every 100 entries
			if idx % 100 == 0:
				frappe.db.commit()
				frappe.publish_realtime(
					"stock_maintenance_progress",
					{
						"progress": idx,
						"total": len(item_warehouses),
						"stage": "Creating Repost Entries"
					}
				)
				
		except Exception as e:
			error_msg = f"{row.item_code} - {row.warehouse}: {str(e)}"
			errors.append(error_msg)
			frappe.log_error(
				title=f"Error creating repost entry for {row.item_code} - {row.warehouse}",
				message=str(e)
			)
			frappe.db.rollback()
	
	frappe.db.commit()
	
	if errors:
		frappe.log_error(
			"\n".join(errors[:10]),  # Log first 10 errors
			"Stock Maintenance - Repost Entry Errors"
		)
	
	return created

