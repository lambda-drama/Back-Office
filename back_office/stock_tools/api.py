# Copyright (c) 2025, stock repost and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import today

try:
	from erpnext.controllers.stock_controller import create_repost_item_valuation_entry
except ImportError:
	create_repost_item_valuation_entry = None


def _get_item_based_reposting_setting():
	"""Get item_based_reposting setting from Stock Reposting Settings doctype"""
	try:
		# Try to get the setting from Stock Reposting Settings (single doctype)
		if frappe.db.exists("DocType", "Stock Reposting Settings"):
			setting = frappe.get_single("Stock Reposting Settings")
			return getattr(setting, "item_based_reposting", False)
	except Exception:
		pass
	
	# Default to False if setting doesn't exist
	return False


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


@frappe.whitelist()
def create_repost_entries_for_transactions(start_date=None, end_date=None, transaction_doctype=None, enqueue=False):
	"""Create Repost Item Valuation entries based on transactions
	
	This method creates Repost Item Valuation entries for each transaction
	with based_on="Transaction", with recreate_stock_ledgers checked. 
	ERPNext will then handle recreating the stock ledger entries from these repost entries.
	
	Args:
		start_date: Start date for transactions (default: 2000-01-01)
		end_date: End date for transactions (default: today)
		transaction_doctype: Filter by specific doctype (optional)
		enqueue: If True, run in background using RQ (default: False)
	"""
	if enqueue:
		# Enqueue the job to run in background
		job = frappe.enqueue(
			"back_office.stock_tools.api._create_repost_entries_for_transactions_background",
			start_date=start_date,
			end_date=end_date,
			transaction_doctype=transaction_doctype,
			queue="long",
			timeout=3600,  # 1 hour timeout
			job_name=f"Create Repost Entries for Transactions - {frappe.session.user}"
		)
		return {"job_id": job.id, "status": "queued"}
	else:
		# Run synchronously
		return _create_repost_entries_for_transactions_background(start_date, end_date, transaction_doctype)


def _create_repost_entries_for_transactions_background(start_date=None, end_date=None, transaction_doctype=None):
	"""Background function to create Repost Item Valuation entries for transactions"""
	try:
		frappe.flags.in_progress = True
		
		# Get settings
		start_date = start_date or "2000-01-01"
		end_date = end_date or today()
		doctype_filter = transaction_doctype if transaction_doctype else None
		
		# Get all transactions in chronological order
		frappe.publish_realtime("stock_maintenance_progress", {
			"stage": "Fetching transactions...",
			"progress": 0
		})
		
		all_transactions = _get_all_transactions_chronological(start_date, end_date, doctype_filter)
		
		frappe.logger().info(
			f"Fetched {len(all_transactions) if all_transactions else 0} transactions "
			f"for date range {start_date} to {end_date}, "
			f"doctype filter: {doctype_filter or 'All'}"
		)
		
		if not all_transactions:
			frappe.publish_realtime("stock_maintenance_progress", {
				"stage": "Completed",
				"progress": 100,
				"created_count": 0,
				"skipped_count": 0
			})
			return {
				"created_count": 0,
				"skipped_count": 0,
				"status": "completed"
			}
		
		# Create Repost Item Valuation entries for each transaction
		frappe.publish_realtime("stock_maintenance_progress", {
			"stage": "Creating Repost Item Valuation entries...",
			"progress": 0,
			"total": len(all_transactions)
		})
		
		result = _create_repost_entries_from_transactions(all_transactions)
		created_count = result.get("created", 0)
		skipped_count = result.get("skipped", 0)
		skip_reasons = result.get("skip_reasons", {})
		errors_count = result.get("errors_count", 0)
		
		# Publish completion
		frappe.publish_realtime("stock_maintenance_progress", {
			"stage": "Completed",
			"progress": 100,
			"created_count": created_count,
			"skipped_count": skipped_count,
			"skip_reasons": skip_reasons,
			"errors_count": errors_count
		})
		
		# Log completion
		frappe.logger().info(
			f"Repost Entries Created for Transactions: "
			f"Created {created_count} entries, "
			f"Skipped {skipped_count} transactions"
		)
		
		return {
			"created_count": created_count,
			"skipped_count": skipped_count,
			"skip_reasons": skip_reasons,
			"errors_count": errors_count,
			"status": "completed"
		}
		
	except Exception as e:
		error_msg = str(e)
		frappe.log_error(frappe.get_traceback(), "Create Repost Entries for Transactions Error")
		frappe.publish_realtime("stock_maintenance_progress", {
			"stage": "Error",
			"error": error_msg,
			"progress": 0
		})
		raise
	finally:
		frappe.flags.in_progress = False
		frappe.db.commit()


def _get_all_transactions_chronological(start_date, end_date, doctype_filter=None):
	"""Get all transactions in chronological order (oldest first)"""
	# Get all stock-affecting doctypes
	all_stock_doctypes = _get_stock_affecting_doctypes()
	
	# Filter by doctype_filter if provided
	if doctype_filter:
		all_stock_doctypes = [doctype_filter] if doctype_filter in all_stock_doctypes else []
	
	all_transactions = []
	
	for doctype in all_stock_doctypes:
		# Check if DocType exists
		if not frappe.db.exists("DocType", doctype):
			continue
		
		try:
			transactions = _get_transactions(doctype, start_date, end_date)
			if transactions:
				# Add doctype to each transaction for later reference
				for trans in transactions:
					trans["doctype"] = doctype
				all_transactions.extend(transactions)
		except Exception as e:
			# Log error but continue with other doctypes
			error_msg = str(e)
			if "doesn't exist" in error_msg.lower() or ("table" in error_msg.lower() and "exist" in error_msg.lower()):
				frappe.log_error(
					title=f"Table does not exist for {doctype}",
					message=f"Error: {error_msg}\n\nDoctype: {doctype}"
				)
			else:
				frappe.log_error(
					title=f"Error getting transactions for {doctype}",
					message=f"Error: {error_msg}\n\nDoctype: {doctype}"
				)
			continue
	
	# Sort all transactions by posting_date, posting_time, and creation (oldest first)
	all_transactions.sort(key=lambda x: (
		x.get("posting_date", "2000-01-01"),
		x.get("posting_time", "00:00:00"),
		x.get("creation", "2000-01-01 00:00:00")
	))
	
	return all_transactions


def _create_stock_reconciliation_sles(doc):
	"""Create Stock Ledger Entries for Stock Reconciliation using stored document values.
	
	This is needed because Stock Reconciliation's update_stock_ledger() recalculates
	qty_diff based on CURRENT bin values, not the stored document values. When called
	later (after stock has changed), it may result in incorrect or no SLEs being created.
	
	This function creates SLEs using the STORED qty and current_qty values from the document.
	
	Returns:
		dict: {
			"created": int,  # Number of SLEs created
			"failed_items": list  # List of dicts with failed item details
		}
	"""
	from frappe.utils import flt
	
	sles_created = 0
	failed_items = []
	
	# Get items from Stock Reconciliation
	items = doc.items if hasattr(doc, "items") and doc.items else []
	
	for item in items:
		item_code = getattr(item, "item_code", None)
		warehouse = getattr(item, "warehouse", None)
		
		if not item_code or not warehouse:
			continue
		
		# Use STORED values from the document, not recalculated ones
		qty = flt(getattr(item, "qty", 0))
		current_qty = flt(getattr(item, "current_qty", 0))
		qty_diff = qty - current_qty
		
		# Skip if no difference
		if qty_diff == 0:
			continue
		
		try:
			# Get valuation rate - use stored value or fetch from item
			valuation_rate = flt(getattr(item, "valuation_rate", 0))
			if not valuation_rate:
				valuation_rate = frappe.db.get_value("Item", item_code, "valuation_rate") or 0
			
			# Get serial/batch info if applicable
			serial_no = getattr(item, "serial_no", None)
			batch_no = getattr(item, "batch_no", None)
			
			# Create Stock Ledger Entry
			sle = frappe.get_doc({
				"doctype": "Stock Ledger Entry",
				"item_code": item_code,
				"warehouse": warehouse,
				"posting_date": doc.posting_date,
				"posting_time": doc.posting_time,
				"voucher_type": "Stock Reconciliation",
				"voucher_no": doc.name,
				"voucher_detail_no": item.name,
				"actual_qty": qty_diff,
				"qty_after_transaction": qty,
				"incoming_rate": valuation_rate if qty_diff > 0 else 0,
				"valuation_rate": valuation_rate,
				"stock_value": qty * valuation_rate,
				"stock_value_difference": qty_diff * valuation_rate,
				"company": doc.company,
				"batch_no": batch_no,
				"serial_no": serial_no,
				"is_cancelled": 0,
				"docstatus": 1
			})
			
			# Set additional fields if they exist
			if hasattr(item, "serial_and_batch_bundle") and item.serial_and_batch_bundle:
				sle.serial_and_batch_bundle = item.serial_and_batch_bundle
			
			sle.flags.ignore_permissions = True
			sle.flags.ignore_validate = True
			sle.flags.ignore_links = True
			sle.db_insert()
			
			sles_created += 1
		except Exception as e:
			# Collect failed item details
			failed_items.append({
				"item_code": item_code,
				"warehouse": warehouse,
				"error": str(e)
			})
	
	return {
		"created": sles_created,
		"failed_items": failed_items
	}


def _get_item_warehouse_combinations(doctype, voucher_no):
	"""Extract unique item-warehouse combinations from a transaction"""
	try:
		doc = frappe.get_doc(doctype, voucher_no)
		item_warehouses = set()
		
		# Get items from the document
		items = []
		if hasattr(doc, "items") and doc.items:
			items = doc.items
		elif hasattr(doc, "reconciliation_items") and doc.reconciliation_items:
			items = doc.reconciliation_items
		elif hasattr(doc, "item_code") and doc.item_code:
			# Single item document
			items = [doc]
		
		# Extract item_code and warehouse from each item
		for item in items:
			item_code = getattr(item, "item_code", None)
			warehouse = getattr(item, "warehouse", None) or getattr(item, "target_warehouse", None) or getattr(item, "source_warehouse", None)
			
			# For Stock Entry, check both source and target warehouses
			if doctype == "Stock Entry":
				source_warehouse = getattr(item, "s_warehouse", None)
				target_warehouse = getattr(item, "t_warehouse", None)
				
				if item_code and source_warehouse:
					item_warehouses.add((item_code, source_warehouse))
				if item_code and target_warehouse:
					item_warehouses.add((item_code, target_warehouse))
			else:
				if item_code and warehouse:
					item_warehouses.add((item_code, warehouse))
		
		return list(item_warehouses)
	except Exception as e:
		frappe.logger().error(f"Error extracting item-warehouse combinations from {doctype} {voucher_no}: {str(e)}")
		return []


def _create_repost_entries_from_transactions(transactions):
	"""Create Repost Item Valuation entries for each transaction or per item based on setting
	
	When item_based_reposting is False (transaction-based):
		- Creates one repost entry per transaction with based_on="Transaction"
		- Each repost entry directly recreates stock ledger entries for that specific transaction
		- Uses recreate_stock_ledgers=1 to ensure SLEs are created
	
	When item_based_reposting is True (item-based):
		- FIRST: Ensures Stock Ledger Entries (SLEs) exist for each transaction by calling update_stock_ledger()
		- THEN: Creates repost entries per item-warehouse combination with based_on="Item and Warehouse"
		- Each repost entry processes ALL existing SLEs for that item-warehouse from posting_date onwards
		- Stock ledger entries are updated/recalculated when the repost entry is processed by ERPNext's background job
		- This ensures that item-based repost entries have SLEs to process (they don't create new SLEs, only process existing ones)
		
		SPECIAL CASE - Stock Reconciliation:
		- Stock Reconciliation's update_stock_ledger() recalculates qty_diff based on CURRENT bin values,
		  not the stored document values. This causes issues when called later after stock has changed.
		- For Stock Reconciliation, we manually create SLEs using the stored document values (qty, current_qty)
		  via _create_stock_reconciliation_sles(), then continue with item-based reposting.
	"""
	created = 0
	skipped = 0
	errors = []
	# Collect failures for item-based reposting (structured format)
	sle_creation_failures = []  # List of dicts: {doctype, voucher_no, item_code, warehouse, error, reason}
	repost_entry_failures = []  # List of dicts: {doctype, voucher_no, item_code, warehouse, error, reason}
	skipped_items = []  # List of dicts: {doctype, voucher_no, item_code, warehouse, reason} for tracking skipped items
	skip_reasons = {
		"no_doctype_or_voucher": 0,
		"already_exists": 0,
		"voucher_not_found": 0,
		"other_error": 0
	}
	
	if not transactions:
		frappe.logger().info("No transactions found to create repost entries for")
		return {
			"created": 0,
			"skipped": 0
		}
	
	# Get the item_based_reposting setting
	item_based_reposting = _get_item_based_reposting_setting()
	frappe.logger().info(f"Starting to create repost entries for {len(transactions)} transactions. Item-based reposting: {item_based_reposting}")
	
	for idx, trans in enumerate(transactions, 1):
		try:
			doctype = trans.get("doctype")
			voucher_no = trans.get("name")
			
			if not doctype or not voucher_no:
				skipped += 1
				skip_reasons["no_doctype_or_voucher"] += 1
				if skipped <= 5:
					frappe.logger().info(f"Skipping transaction {idx}: Missing doctype or voucher_no. Data: {trans}")
				continue
			
			# Verify the transaction document exists and is submitted
			if not frappe.db.exists(doctype, voucher_no):
				skipped += 1
				skip_reasons["voucher_not_found"] += 1
				if skipped <= 5:
					frappe.logger().info(f"Skipping {doctype} {voucher_no}: Document does not exist")
				continue
			
			# Check if transaction is submitted (docstatus = 1)
			docstatus = frappe.db.get_value(doctype, voucher_no, "docstatus")
			if docstatus != 1:
				skipped += 1
				skip_reasons["not_submitted"] = skip_reasons.get("not_submitted", 0) + 1
				if skipped <= 5:
					frappe.logger().info(f"Skipping {doctype} {voucher_no}: Document not submitted (docstatus={docstatus})")
				continue
			
			if item_based_reposting:
				# Item-based reposting: First ensure SLEs exist, then create repost entries per item-warehouse
				# IMPORTANT: Item-based repost entries only process existing SLEs, so we must ensure
				# SLEs are created first for the transactions, then create repost entries per item-warehouse
				
				# Step 1: Ensure Stock Ledger Entries exist for this transaction
				try:
					doc = frappe.get_doc(doctype, voucher_no)
					
					# Check if SLEs already exist
					existing_sles_count = frappe.db.count(
						"Stock Ledger Entry",
						{
							"voucher_type": doctype,
							"voucher_no": voucher_no,
							"is_cancelled": 0
						}
					)
					
					# If no SLEs exist, create them first
					if existing_sles_count == 0:
						if doctype == "Stock Reconciliation":
							# SPECIAL CASE: Stock Reconciliation
							# update_stock_ledger() recalculates qty_diff based on CURRENT bin values,
							# not the stored values. We create SLEs manually using stored document values.
							sles_result = _create_stock_reconciliation_sles(doc)
							sles_created = sles_result.get("created", 0)
							failed_items = sles_result.get("failed_items", [])
							
							# Collect failed items
							for failed_item in failed_items:
								sle_creation_failures.append({
									"doctype": doctype,
									"voucher_no": voucher_no,
									"item_code": failed_item.get("item_code", "N/A"),
									"warehouse": failed_item.get("warehouse", "N/A"),
									"error": failed_item.get("error", "Unknown error"),
									"reason": "Stock Reconciliation SLE creation failed"
								})
							
							if sles_created > 0:
								frappe.db.commit()
								frappe.logger().info(f"Manually created {sles_created} SLEs for Stock Reconciliation {voucher_no}")
							else:
								# No SLEs created - all items have qty_diff = 0 or all failed
								if not failed_items:
									# All items have qty_diff = 0 (not an error)
									skipped += 1
									skip_reasons["no_sle_needed"] = skip_reasons.get("no_sle_needed", 0) + 1
									if skipped <= 5:
										frappe.logger().info(f"Skipping {doctype} {voucher_no}: No SLEs needed (qty_diff = 0 for all items)")
									continue
								else:
									# All items failed - skip transaction
									skipped += 1
									skip_reasons["sle_creation_error"] = skip_reasons.get("sle_creation_error", 0) + 1
									continue
						elif hasattr(doc, "update_stock_ledger"):
							try:
								doc.update_stock_ledger(allow_negative_stock=True)
								frappe.db.commit()
								
								# Verify that SLEs were actually created
								new_sles_count = frappe.db.count(
									"Stock Ledger Entry",
									{
										"voucher_type": doctype,
										"voucher_no": voucher_no,
										"is_cancelled": 0
									}
								)
								
								if new_sles_count == 0:
									# No SLEs were created - track all items as failed
									item_warehouses = _get_item_warehouse_combinations(doctype, voucher_no)
									error_msg = "No Stock Ledger Entries were created (items may have zero quantity or invalid data)"
									
									if not item_warehouses:
										sle_creation_failures.append({
											"doctype": doctype,
											"voucher_no": voucher_no,
											"item_code": "Multiple/Unknown",
											"warehouse": "Multiple/Unknown",
											"error": error_msg,
											"reason": "No SLEs created"
										})
									else:
										# Log failure for each item-warehouse combination
										for item_code, warehouse in item_warehouses:
											sle_creation_failures.append({
												"doctype": doctype,
												"voucher_no": voucher_no,
												"item_code": item_code,
												"warehouse": warehouse,
												"error": error_msg,
												"reason": "No SLEs created"
											})
									
									skipped += 1
									skip_reasons["sle_creation_error"] = skip_reasons.get("sle_creation_error", 0) + 1
									continue
								else:
									frappe.logger().info(f"Created {new_sles_count} SLEs for {doctype} {voucher_no} before item-based reposting")
							except Exception as sle_error:
								# Get item-warehouse combinations to report which items failed
								item_warehouses = _get_item_warehouse_combinations(doctype, voucher_no)
								error_msg = str(sle_error)
								
								# If we can't determine specific items, log the whole transaction
								if not item_warehouses:
									sle_creation_failures.append({
										"doctype": doctype,
										"voucher_no": voucher_no,
										"item_code": "Multiple/Unknown",
										"warehouse": "Multiple/Unknown",
										"error": error_msg,
										"reason": "Exception during SLE creation"
									})
								else:
									# Log failure for each item-warehouse combination
									for item_code, warehouse in item_warehouses:
										sle_creation_failures.append({
											"doctype": doctype,
											"voucher_no": voucher_no,
											"item_code": item_code,
											"warehouse": warehouse,
											"error": error_msg,
											"reason": "Exception during SLE creation"
										})
								
								frappe.db.rollback()
								skipped += 1
								skip_reasons["sle_creation_error"] = skip_reasons.get("sle_creation_error", 0) + 1
								continue
						else:
							# Document doesn't have update_stock_ledger method, skip
							skipped += 1
							skip_reasons["no_update_stock_ledger"] = skip_reasons.get("no_update_stock_ledger", 0) + 1
							if skipped <= 5:
								frappe.logger().info(f"Skipping {doctype} {voucher_no}: No update_stock_ledger method")
							continue
				except Exception as sle_error:
					# General exception - try to get item details
					error_msg = str(sle_error)
					try:
						item_warehouses = _get_item_warehouse_combinations(doctype, voucher_no)
						if item_warehouses:
							for item_code, warehouse in item_warehouses:
								sle_creation_failures.append({
									"doctype": doctype,
									"voucher_no": voucher_no,
									"item_code": item_code,
									"warehouse": warehouse,
									"error": error_msg,
									"reason": "General exception during SLE creation"
								})
						else:
							sle_creation_failures.append({
								"doctype": doctype,
								"voucher_no": voucher_no,
								"item_code": "Unknown",
								"warehouse": "Unknown",
								"error": error_msg,
								"reason": "General exception during SLE creation"
							})
					except:
						# If we can't get item details, log with unknown
						sle_creation_failures.append({
							"doctype": doctype,
							"voucher_no": voucher_no,
							"item_code": "Unknown",
							"warehouse": "Unknown",
							"error": error_msg,
							"reason": "General exception during SLE creation"
						})
					
					frappe.db.rollback()
					skipped += 1
					skip_reasons["sle_creation_error"] = skip_reasons.get("sle_creation_error", 0) + 1
					continue
				
				# Step 2: Get item-warehouse combinations and create repost entries
				item_warehouses = _get_item_warehouse_combinations(doctype, voucher_no)
				
				if not item_warehouses:
					# Track skipped transaction
					skipped_items.append({
						"doctype": doctype,
						"voucher_no": voucher_no,
						"item_code": "N/A",
						"warehouse": "N/A",
						"reason": "No item-warehouse combinations found"
					})
					skipped += 1
					skip_reasons["no_items"] = skip_reasons.get("no_items", 0) + 1
					if skipped <= 5:
						frappe.logger().info(f"Skipping {doctype} {voucher_no}: No item-warehouse combinations found")
					continue
				
				# Create repost entry for each item-warehouse combination
				transaction_created = 0
				for item_code, warehouse in item_warehouses:
					try:
						# Check if repost entry already exists and is queued
						existing = frappe.db.exists(
							"Repost Item Valuation",
							{
								"item_code": item_code,
								"warehouse": warehouse,
								"posting_date": "1900-01-01",
								"docstatus": 1,
								"status": ["in", ["Queued", "In Progress"]]
							}
						)
						
						if existing:
							# Track skipped items
							skipped_items.append({
								"doctype": doctype,
								"voucher_no": voucher_no,
								"item_code": item_code,
								"warehouse": warehouse,
								"reason": "Repost entry already exists"
							})
							continue  # Skip if already exists
						
						# Create Repost Item Valuation entry for item-warehouse
						# Note: create_repost_item_valuation_entry automatically creates and submits the entry
						# When submitted, it will process all transactions for this item-warehouse from posting_date
						# and create/update stock ledger entries chronologically
						if not create_repost_item_valuation_entry:
							frappe.throw(_("create_repost_item_valuation_entry function not available. Please ensure ERPNext is installed."))
						
						create_repost_item_valuation_entry({
							"based_on": "Item and Warehouse",
							"item_code": item_code,
							"warehouse": warehouse,
							"posting_date": "1900-01-01",  # Start from beginning - will process all transactions for this item-warehouse
							"posting_time": "00:01",
							"allow_negative_stock": 1,
							"allow_zero_rate": 0
						})
						
						transaction_created += 1
						created += 1
						
					except Exception as item_error:
						# Collect failure instead of logging immediately
						repost_entry_failures.append({
							"doctype": doctype,
							"voucher_no": voucher_no,
							"item_code": item_code,
							"warehouse": warehouse,
							"error": str(item_error),
							"reason": "Exception during repost entry creation"
						})
						frappe.db.rollback()
				
				if transaction_created > 0:
					frappe.db.commit()
					# Log first few successful creations
					if created <= 5:
						frappe.logger().info(f"Created {transaction_created} repost entries for {doctype} {voucher_no} (item-based)")
				else:
					skipped += 1
					skip_reasons["already_exists"] += 1
				
			else:
				# Transaction-based reposting: Create one repost entry per transaction (original behavior)
				# Check if Repost Item Valuation entry already exists for this voucher
				existing = frappe.db.exists(
					"Repost Item Valuation",
					{
						"voucher_type": doctype,
						"voucher_no": voucher_no,
						"docstatus": ["!=", 2]  # Not cancelled
					}
				)
				
				if existing:
					skipped += 1
					skip_reasons["already_exists"] += 1
					if skipped <= 10:
						frappe.logger().info(f"Skipping {doctype} {voucher_no}: Repost entry already exists")
					continue
				
				# Create Repost Item Valuation entry
				repost_doc = frappe.get_doc({
					"doctype": "Repost Item Valuation",
					"based_on": "Transaction",
					"voucher_type": doctype,
					"voucher_no": voucher_no,
					"recreate_stock_ledgers": 1,  # Check the recreate stock ledgers checkbox (plural)
					"allow_negative_stock": 1,
					"allow_zero_rate": 0
				})
				
				repost_doc.insert(ignore_permissions=True)
				frappe.db.commit()
				
				# Submit the repost entry (submit doesn't accept ignore_permissions, use frappe.flags instead)
				frappe.flags.ignore_permissions = True
				try:
					repost_doc.submit()
				finally:
					frappe.flags.ignore_permissions = False
				frappe.db.commit()
				
				created += 1
				
				# Log first few successful creations
				if created <= 5:
					frappe.logger().info(f"Created repost entry for {doctype} {voucher_no}: {repost_doc.name}")
			
			# Progress update every 50 transactions
			if idx % 50 == 0:
				progress_pct = int((idx / len(transactions)) * 100) if transactions else 0
				frappe.publish_realtime(
					"stock_maintenance_progress",
					{
						"stage": "Creating Repost Item Valuation entries...",
						"progress": progress_pct,
						"current": idx,
						"total": len(transactions)
					}
				)
				frappe.db.commit()
				
		except Exception as e:
			error_msg = f"{trans.get('doctype', 'Unknown')} {trans.get('name', 'Unknown')}: {str(e)}"
			errors.append(error_msg)
			skip_reasons["other_error"] += 1
			frappe.log_error(
				title=f"Error creating repost entry for {trans.get('doctype', 'Unknown')} {trans.get('name', 'Unknown')}",
				message=f"Error: {str(e)}\n\nTraceback:\n{frappe.get_traceback()}\n\nTransaction data: {trans}"
			)
			frappe.db.rollback()
			skipped += 1
	
	frappe.db.commit()
	
	# Log summary
	frappe.logger().info(
		f"Repost entries creation completed. "
		f"Created: {created}, Skipped: {skipped}. "
		f"Skip reasons: {skip_reasons}"
	)
	
	# Debug log for item-based reposting
	if item_based_reposting:
		frappe.logger().info(
			f"Item-based reposting summary: "
			f"SLE failures: {len(sle_creation_failures)}, "
			f"Repost entry failures: {len(repost_entry_failures)}, "
			f"Skipped items: {len(skipped_items)}"
		)
	
	# Create consolidated error log for item-based reposting failures and skipped items
	if item_based_reposting and (sle_creation_failures or repost_entry_failures or skipped_items):
		error_message_parts = []
		
		if sle_creation_failures:
			error_message_parts.append(f"\n{'='*80}")
			error_message_parts.append(f"STOCK LEDGER ENTRY CREATION FAILURES ({len(sle_creation_failures)} items)")
			error_message_parts.append(f"{'='*80}\n")
			error_message_parts.append(f"{'Transaction':<35} {'Item Code':<25} {'Warehouse':<25} {'Reason':<30} {'Error'}")
			error_message_parts.append("-" * 140)
			
			for failure in sle_creation_failures:
				transaction = f"{failure['doctype']} - {failure['voucher_no']}"
				if len(transaction) > 34:
					transaction = transaction[:31] + "..."
				item_code = failure.get('item_code', 'N/A')
				if len(item_code) > 24:
					item_code = item_code[:21] + "..."
				warehouse = failure.get('warehouse', 'N/A')
				if len(warehouse) > 24:
					warehouse = warehouse[:21] + "..."
				reason = failure.get('reason', 'Unknown')
				if len(reason) > 29:
					reason = reason[:26] + "..."
				error = failure.get('error', 'Unknown error')
				# Truncate long errors
				if len(error) > 50:
					error = error[:47] + "..."
				error_message_parts.append(f"{transaction:<35} {item_code:<25} {warehouse:<25} {reason:<30} {error}")
		
		if repost_entry_failures:
			error_message_parts.append(f"\n{'='*80}")
			error_message_parts.append(f"REPOST ITEM VALUATION ENTRY CREATION FAILURES ({len(repost_entry_failures)} items)")
			error_message_parts.append(f"{'='*80}\n")
			error_message_parts.append(f"{'Transaction':<35} {'Item Code':<25} {'Warehouse':<25} {'Reason':<30} {'Error'}")
			error_message_parts.append("-" * 140)
			
			for failure in repost_entry_failures:
				transaction = f"{failure['doctype']} - {failure['voucher_no']}"
				if len(transaction) > 34:
					transaction = transaction[:31] + "..."
				item_code = failure.get('item_code', 'N/A')
				if len(item_code) > 24:
					item_code = item_code[:21] + "..."
				warehouse = failure.get('warehouse', 'N/A')
				if len(warehouse) > 24:
					warehouse = warehouse[:21] + "..."
				reason = failure.get('reason', 'Unknown')
				if len(reason) > 29:
					reason = reason[:26] + "..."
				error = failure.get('error', 'Unknown error')
				# Truncate long errors
				if len(error) > 50:
					error = error[:47] + "..."
				error_message_parts.append(f"{transaction:<35} {item_code:<25} {warehouse:<25} {reason:<30} {error}")
		
		if skipped_items:
			error_message_parts.append(f"\n{'='*80}")
			error_message_parts.append(f"SKIPPED ITEMS ({len(skipped_items)} items)")
			error_message_parts.append(f"{'='*80}\n")
			error_message_parts.append(f"{'Transaction':<35} {'Item Code':<25} {'Warehouse':<25} {'Reason'}")
			error_message_parts.append("-" * 90)
			
			for skipped in skipped_items:
				transaction = f"{skipped['doctype']} - {skipped['voucher_no']}"
				if len(transaction) > 34:
					transaction = transaction[:31] + "..."
				item_code = skipped.get('item_code', 'N/A')
				if len(item_code) > 24:
					item_code = item_code[:21] + "..."
				warehouse = skipped.get('warehouse', 'N/A')
				if len(warehouse) > 24:
					warehouse = warehouse[:21] + "..."
				reason = skipped.get('reason', 'Unknown')
				error_message_parts.append(f"{transaction:<35} {item_code:<25} {warehouse:<25} {reason}")
		
		error_message_parts.append(f"\n{'='*80}")
		error_message_parts.append(f"SUMMARY:")
		error_message_parts.append(f"  - SLE Creation Failures: {len(sle_creation_failures)}")
		error_message_parts.append(f"  - Repost Entry Creation Failures: {len(repost_entry_failures)}")
		error_message_parts.append(f"  - Skipped Items: {len(skipped_items)}")
		error_message_parts.append(f"  - Total Issues: {len(sle_creation_failures) + len(repost_entry_failures) + len(skipped_items)}")
		error_message_parts.append(f"{'='*80}")
		
		# Create single consolidated error log
		frappe.log_error(
			title="Item-Based Reposting - Consolidated Failures and Skipped Items",
			message="\n".join(error_message_parts)
		)
	
	# Log other errors (for transaction-based reposting or general errors)
	if errors:
		frappe.log_error(
			f"Total errors: {len(errors)}\n\n" + "\n".join(errors[:20]),  # Log first 20 errors
			"Create Repost Entries - Transaction Errors"
		)
	
	return {
		"created": created,
		"skipped": skipped,
		"skip_reasons": skip_reasons,
		"errors_count": len(errors),
		"sle_creation_failures": len(sle_creation_failures) if item_based_reposting else 0,
		"repost_entry_failures": len(repost_entry_failures) if item_based_reposting else 0,
		"skipped_items_count": len(skipped_items) if item_based_reposting else 0
	}


@frappe.whitelist()
def process_transactions_chronologically(start_date=None, end_date=None, transaction_doctype=None, enqueue=False):
	"""Process transactions chronologically: Create SLEs and Repost Item Valuation entries
	
	This method processes all transactions in chronological order (oldest to latest),
	creating Stock Ledger Entries first, then Repost Item Valuation entries.
	Only SLE creation failures are logged as errors (not "repost entry already exists").
	
	Args:
		start_date: Start date for transactions (default: 2000-01-01)
		end_date: End date for transactions (default: today)
		transaction_doctype: Filter by specific doctype (optional)
		enqueue: If True, run in background using RQ (default: False)
	"""
	if enqueue:
		job = frappe.enqueue(
			"back_office.stock_tools.api._process_transactions_chronologically_background",
			start_date=start_date,
			end_date=end_date,
			transaction_doctype=transaction_doctype,
			queue="long",
			timeout=7200,  # 2 hour timeout
			job_name=f"Process Transactions Chronologically - {frappe.session.user}"
		)
		return {"job_id": job.id, "status": "queued"}
	else:
		return _process_transactions_chronologically_background(start_date, end_date, transaction_doctype)


def _process_transactions_chronologically_background(start_date=None, end_date=None, transaction_doctype=None):
	"""Background function to process transactions chronologically"""
	try:
		frappe.flags.in_progress = True
		
		start_date = start_date or "2000-01-01"
		end_date = end_date or today()
		doctype_filter = transaction_doctype if transaction_doctype else None
		
		# Get all transactions in chronological order
		frappe.publish_realtime("stock_maintenance_progress", {
			"stage": "Fetching transactions chronologically...",
			"progress": 0
		})
		
		all_transactions = _get_all_transactions_chronological(start_date, end_date, doctype_filter)
		
		if not all_transactions:
			frappe.publish_realtime("stock_maintenance_progress", {
				"stage": "Completed",
				"progress": 100,
				"sles_created": 0,
				"repost_entries_created": 0,
				"sle_failures": 0
			})
			return {
				"sles_created": 0,
				"repost_entries_created": 0,
				"sle_failures": 0,
				"status": "completed"
			}
		
		frappe.logger().info(
			f"Processing {len(all_transactions)} transactions chronologically "
			f"from {start_date} to {end_date}"
		)
		
		# Track results
		sles_created = 0
		repost_entries_created = 0
		sle_creation_failures = []  # List of dicts: {doctype, voucher_no, item_code, warehouse, error}
		
		# Process each transaction chronologically
		for idx, trans in enumerate(all_transactions, 1):
			doctype = trans.get("doctype")
			voucher_no = trans.get("name")
			
			if not doctype or not voucher_no:
				continue
			
			# Verify transaction exists and is submitted
			if not frappe.db.exists(doctype, voucher_no):
				continue
			
			docstatus = frappe.db.get_value(doctype, voucher_no, "docstatus")
			if docstatus != 1:
				continue
			
			try:
				doc = frappe.get_doc(doctype, voucher_no)
				
				# Step 1: Create Stock Ledger Entries
				try:
					# Check if SLEs already exist
					existing_sles_count = frappe.db.count(
						"Stock Ledger Entry",
						{
							"voucher_type": doctype,
							"voucher_no": voucher_no,
							"is_cancelled": 0
						}
					)
					
					if existing_sles_count == 0:
						# Create SLEs
						if doctype == "Stock Reconciliation":
							# Special handling for Stock Reconciliation
							sles_result = _create_stock_reconciliation_sles(doc)
							created_count = sles_result.get("created", 0)
							failed_items = sles_result.get("failed_items", [])
							
							# Track failed items
							for failed_item in failed_items:
								sle_creation_failures.append({
									"doctype": doctype,
									"voucher_no": voucher_no,
									"item_code": failed_item.get("item_code", "N/A"),
									"warehouse": failed_item.get("warehouse", "N/A"),
									"error": failed_item.get("error", "Unknown error")
								})
							
							if created_count > 0:
								frappe.db.commit()
								sles_created += created_count
						elif hasattr(doc, "update_stock_ledger"):
							try:
								doc.update_stock_ledger(allow_negative_stock=True)
								frappe.db.commit()
								
								# Verify SLEs were created
								new_sles_count = frappe.db.count(
									"Stock Ledger Entry",
									{
										"voucher_type": doctype,
										"voucher_no": voucher_no,
										"is_cancelled": 0
									}
								)
								
								if new_sles_count == 0:
									# No SLEs created - track failure
									item_warehouses = _get_item_warehouse_combinations(doctype, voucher_no)
									error_msg = "No Stock Ledger Entries were created (items may have zero quantity or invalid data)"
									
									if not item_warehouses:
										sle_creation_failures.append({
											"doctype": doctype,
											"voucher_no": voucher_no,
											"item_code": "Multiple/Unknown",
											"warehouse": "Multiple/Unknown",
											"error": error_msg
										})
									else:
										for item_code, warehouse in item_warehouses:
											sle_creation_failures.append({
												"doctype": doctype,
												"voucher_no": voucher_no,
												"item_code": item_code,
												"warehouse": warehouse,
												"error": error_msg
											})
								else:
									sles_created += new_sles_count
							except Exception as sle_error:
								# Track SLE creation failure
								item_warehouses = _get_item_warehouse_combinations(doctype, voucher_no)
								error_msg = str(sle_error)
								
								if not item_warehouses:
									sle_creation_failures.append({
										"doctype": doctype,
										"voucher_no": voucher_no,
										"item_code": "Multiple/Unknown",
										"warehouse": "Multiple/Unknown",
										"error": error_msg
									})
								else:
									for item_code, warehouse in item_warehouses:
										sle_creation_failures.append({
											"doctype": doctype,
											"voucher_no": voucher_no,
											"item_code": item_code,
											"warehouse": warehouse,
											"error": error_msg
										})
								frappe.db.rollback()
				except Exception as sle_error:
					# General exception during SLE creation
					item_warehouses = _get_item_warehouse_combinations(doctype, voucher_no)
					error_msg = str(sle_error)
					
					if not item_warehouses:
						sle_creation_failures.append({
							"doctype": doctype,
							"voucher_no": voucher_no,
							"item_code": "Unknown",
							"warehouse": "Unknown",
							"error": error_msg
						})
					else:
						for item_code, warehouse in item_warehouses:
							sle_creation_failures.append({
								"doctype": doctype,
								"voucher_no": voucher_no,
								"item_code": item_code,
								"warehouse": warehouse,
								"error": error_msg
							})
					frappe.db.rollback()
				
				# Step 2: Create Repost Item Valuation entries (transaction-based)
				# Only create if repost entry doesn't already exist
				existing_repost = frappe.db.exists(
					"Repost Item Valuation",
					{
						"voucher_type": doctype,
						"voucher_no": voucher_no,
						"docstatus": ["!=", 2]  # Not cancelled
					}
				)
				
				if not existing_repost:
					try:
						repost_doc = frappe.get_doc({
							"doctype": "Repost Item Valuation",
							"based_on": "Transaction",
							"voucher_type": doctype,
							"voucher_no": voucher_no,
							"recreate_stock_ledgers": 1,
							"allow_negative_stock": 1,
							"allow_zero_rate": 0
						})
						
						repost_doc.insert(ignore_permissions=True)
						frappe.db.commit()
						
						# Submit the repost entry
						frappe.flags.ignore_permissions = True
						try:
							repost_doc.submit()
						finally:
							frappe.flags.ignore_permissions = False
						frappe.db.commit()
						
						repost_entries_created += 1
					except Exception:
						# Silently skip repost entry creation errors (don't log as failures)
						frappe.db.rollback()
				
			except Exception as e:
				# Log general transaction errors but continue
				frappe.log_error(
					title=f"Error processing {doctype} {voucher_no}",
					message=str(e)
				)
				frappe.db.rollback()
			
			# Progress update every 50 transactions
			if idx % 50 == 0:
				progress_pct = int((idx / len(all_transactions)) * 100)
				frappe.publish_realtime("stock_maintenance_progress", {
					"stage": f"Processing transactions... ({idx}/{len(all_transactions)})",
					"progress": progress_pct,
					"current": idx,
					"total": len(all_transactions),
					"sles_created": sles_created,
					"repost_entries_created": repost_entries_created,
					"sle_failures": len(sle_creation_failures)
				})
				frappe.db.commit()
		
		frappe.db.commit()
		
		# Create consolidated error log for SLE creation failures
		if sle_creation_failures:
			error_message_parts = []
			error_message_parts.append(f"\n{'='*80}")
			error_message_parts.append(f"STOCK LEDGER ENTRY CREATION FAILURES ({len(sle_creation_failures)} items)")
			error_message_parts.append(f"{'='*80}\n")
			error_message_parts.append(f"{'Transaction':<35} {'Item Code':<25} {'Warehouse':<25} {'Error'}")
			error_message_parts.append("-" * 90)
			
			for failure in sle_creation_failures:
				transaction = f"{failure['doctype']} - {failure['voucher_no']}"
				if len(transaction) > 34:
					transaction = transaction[:31] + "..."
				item_code = failure.get('item_code', 'N/A')
				if len(item_code) > 24:
					item_code = item_code[:21] + "..."
				warehouse = failure.get('warehouse', 'N/A')
				if len(warehouse) > 24:
					warehouse = warehouse[:21] + "..."
				error = failure.get('error', 'Unknown error')
				if len(error) > 50:
					error = error[:47] + "..."
				error_message_parts.append(f"{transaction:<35} {item_code:<25} {warehouse:<25} {error}")
			
			error_message_parts.append(f"\n{'='*80}")
			error_message_parts.append(f"SUMMARY: {len(sle_creation_failures)} SLE creation failures")
			error_message_parts.append(f"{'='*80}")
			
			frappe.log_error(
				title="Chronological Processing - SLE Creation Failures",
				message="\n".join(error_message_parts)
			)
		
		# Publish completion
		frappe.publish_realtime("stock_maintenance_progress", {
			"stage": "Completed",
			"progress": 100,
			"sles_created": sles_created,
			"repost_entries_created": repost_entries_created,
			"sle_failures": len(sle_creation_failures)
		})
		
		frappe.logger().info(
			f"Chronological processing completed: "
			f"SLEs created: {sles_created}, "
			f"Repost entries created: {repost_entries_created}, "
			f"SLE failures: {len(sle_creation_failures)}"
		)
		
		return {
			"sles_created": sles_created,
			"repost_entries_created": repost_entries_created,
			"sle_failures": len(sle_creation_failures),
			"status": "completed"
		}
		
	except Exception as e:
		error_msg = str(e)
		frappe.log_error(frappe.get_traceback(), "Chronological Processing Error")
		frappe.publish_realtime("stock_maintenance_progress", {
			"stage": "Error",
			"error": error_msg,
			"progress": 0
		})
		raise
	finally:
		frappe.flags.in_progress = False
		frappe.db.commit()
