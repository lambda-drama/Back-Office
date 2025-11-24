// Copyright (c) 2025, stock repost and contributors
// For license information, please see license.txt

frappe.ui.form.on("Stock Maintenance Settings", {
	refresh(frm) {
		// Filter transaction_doctype to show only stock-affecting doctypes
		// Note: Material Request and Work Order don't directly create SLEs
		frm.set_query("transaction_doctype", function() {
			return {
				filters: {
					name: ["in", [
						"Stock Entry",
						"Purchase Receipt",
						"Delivery Note",
						"Sales Invoice",
						"Purchase Invoice",
						"Stock Reconciliation",
						"Subcontracting Receipt"
					]]
				}
			};
		});

		// Add button to create Repost Item Valuation entries for transactions (runs in background)
		if (frm.doc.name) {
			frm.add_custom_button(__("Create Repost Entries for Transactions"), function() {
				frappe.confirm(
					__("This will create Repost Item Valuation entries for all transactions and run in the background. Do you want to continue?"),
					function() {
						// Yes - run in background
						frappe.call({
							method: "back_office.stock_tools.api.create_repost_entries_for_transactions",
							args: {
								start_date: frm.doc.start_date || "2000-01-01",
								end_date: frm.doc.end_date || frappe.datetime.get_today(),
								transaction_doctype: frm.doc.transaction_doctype || null,
								enqueue: true
							},
							callback: function(r) {
								if (r.message && r.message.job_id) {
									const jobId = r.message.job_id;

									// Create progress dialog
									const progress_dialog = new frappe.ui.Dialog({
										title: __("Stock Maintenance Progress"),
										fields: [
											{
												fieldtype: "HTML",
												options: `
													<div id="stock_maintenance_progress_container">
														<div class="progress-info">
															<p><strong>Status:</strong> <span id="job_status">Queued...</span></p>
															<p><strong>Stage:</strong> <span id="job_stage">Waiting to start</span></p>
															<div class="progress-bar-container" style="margin-top: 15px;">
																<div class="progress" style="height: 25px; margin-bottom: 10px;">
																	<div id="progress_bar" class="progress-bar progress-bar-striped progress-bar-animated"
																		 role="progressbar" style="width: 0%; background-color: #5e72e4;">
																		<span id="progress_text">0%</span>
																	</div>
																</div>
															</div>
															<div id="job_details" style="margin-top: 15px; font-size: 12px; color: #6c757d;">
																<p>Job ID: <code>${jobId}</code></p>
															</div>
														</div>
													</div>
												`
											}
										],
										primary_action_label: __("Close"),
										primary_action: function() {
											progress_dialog.hide();
										}
									});

									progress_dialog.show();

									// Function to update progress
									const updateProgress = function(data) {
										const statusEl = document.getElementById("job_status");
										const stageEl = document.getElementById("job_stage");
										const progressBar = document.getElementById("progress_bar");
										const progressText = document.getElementById("progress_text");
										const detailsEl = document.getElementById("job_details");

										if (data.stage) {
											stageEl.textContent = data.stage;
										}

										if (data.progress !== undefined) {
											const progress = Math.min(100, Math.max(0, data.progress));
											progressBar.style.width = progress + "%";
											progressText.textContent = progress + "%";

											if (progress === 100) {
												progressBar.classList.remove("progress-bar-animated");
												progressBar.style.backgroundColor = "#28a745";
											}
										}

										if (data.stage === "Completed") {
											statusEl.textContent = "Completed";
											statusEl.style.color = "#28a745";
											detailsEl.innerHTML = `
												<p><strong>Job Completed Successfully!</strong></p>
												<p>Repost Entries Created: <strong>${data.created_count || 0}</strong></p>
												<p>Skipped: <strong>${data.skipped_count || 0}</strong></p>
												<p style="margin-top: 10px;">Job ID: <code>${jobId}</code></p>
											`;
											progressBar.style.width = "100%";
											progressText.textContent = "100%";
											progressBar.classList.remove("progress-bar-animated");
											progressBar.style.backgroundColor = "#28a745";
										} else if (data.stage === "Error") {
											statusEl.textContent = "Error";
											statusEl.style.color = "#dc3545";
											detailsEl.innerHTML = `
												<p style="color: #dc3545;"><strong>Error occurred!</strong></p>
												<p>${data.error || "Unknown error"}</p>
												<p style="margin-top: 10px;">Job ID: <code>${jobId}</code></p>
											`;
											progressBar.style.backgroundColor = "#dc3545";
										} else if (data.total) {
											detailsEl.innerHTML = `
												<p>Progress: ${data.current || 0} / ${data.total || 0}</p>
												<p style="margin-top: 10px;">Job ID: <code>${jobId}</code></p>
											`;
										}
									};

									// Listen for progress updates
									const realtimeListener = frappe.realtime.on("stock_maintenance_progress", function(data) {
										updateProgress(data);

										// Also show alert for completion/error
										if (data.stage === "Completed") {
											frappe.show_alert({
												message: __("Process completed! Created: {0}, Skipped: {1}")
													.replace("{0}", data.created_count || 0)
													.replace("{1}", data.skipped_count || 0),
												indicator: "green"
											}, 10);
										} else if (data.stage === "Error") {
											frappe.show_alert({
												message: __("Error: {0}").replace("{0}", data.error || "Unknown error"),
												indicator: "red"
											}, 10);
										}
									});

									// Clean up listener when dialog is closed
									progress_dialog.onhide = function() {
										if (realtimeListener) {
											frappe.realtime.off("stock_maintenance_progress", realtimeListener);
										}
									};
								}
							}
						});
					},
					function() {
						// No - cancel
					}
				);
			}, __("Actions"));

			// Add button to create Repost Item Valuation entries for transactions (runs synchronously for debugging)
			frm.add_custom_button(__("Create Repost Entries (Debug)"), function() {
				frappe.confirm(
					__("This will create Repost Item Valuation entries synchronously (not in background) for debugging. This may take a while. Do you want to continue?"),
					function() {
						// Show loading indicator
						frappe.show_alert({
							message: __("Creating repost entries..."),
							indicator: "blue"
						}, 5);

						// Run synchronously
						frappe.call({
							method: "back_office.stock_tools.api.create_repost_entries_for_transactions",
							args: {
								start_date: frm.doc.start_date || "2000-01-01",
								end_date: frm.doc.end_date || frappe.datetime.get_today(),
								transaction_doctype: frm.doc.transaction_doctype || null,
								enqueue: false
							},
							freeze: true,
							freeze_message: __("Creating Repost Item Valuation entries..."),
							callback: function(r) {
								if (r.message) {
									if (r.message.status === "completed") {
										frappe.show_alert({
											message: __("Completed! Created: {0}, Skipped: {1}")
												.replace("{0}", r.message.created_count || 0)
												.replace("{1}", r.message.skipped_count || 0),
											indicator: "green"
										}, 10);
									} else if (r.message.error) {
										frappe.show_alert({
											message: __("Error: {0}").replace("{0}", r.message.error || "Unknown error"),
											indicator: "red"
										}, 10);
									}
								}
							},
							error: function(r) {
								frappe.show_alert({
									message: __("Error occurred. Check console for details."),
									indicator: "red"
								}, 10);
								console.error("Error creating repost entries:", r);
							}
						});
					},
					function() {
						// No - cancel
					}
				);
			}, __("Actions"));

			// Add button to repost and create SLEs (runs in background)
			// frm.add_custom_button(__("Repost and Create SLEs"), function() {
			// 	frappe.confirm(
			// 		__("This will run in the background. Do you want to continue?"),
			// 		function() {
			// 			// Yes - run in background
			// 			frappe.call({
			// 				method: "back_office.stock_tools.api.repost_and_create_sles",
			// 				args: {
			// 					start_date: frm.doc.start_date || "2000-01-01",
			// 					end_date: frm.doc.end_date || frappe.datetime.get_today(),
			// 					transaction_doctype: frm.doc.transaction_doctype || null,
			// 					enqueue: true
			// 				},
			// 				callback: function(r) {
			// 					if (r.message && r.message.job_id) {
			// 						const jobId = r.message.job_id;

			// 						// Create progress dialog
			// 						const progress_dialog = new frappe.ui.Dialog({
			// 							title: __("Stock Maintenance Progress"),
			// 							fields: [
			// 								{
			// 									fieldtype: "HTML",
			// 									options: `
			// 										<div id="stock_maintenance_progress_container">
			// 											<div class="progress-info">
			// 												<p><strong>Status:</strong> <span id="job_status">Queued...</span></p>
			// 												<p><strong>Stage:</strong> <span id="job_stage">Waiting to start</span></p>
			// 												<div class="progress-bar-container" style="margin-top: 15px;">
			// 													<div class="progress" style="height: 25px; margin-bottom: 10px;">
			// 														<div id="progress_bar" class="progress-bar progress-bar-striped progress-bar-animated"
			// 															 role="progressbar" style="width: 0%; background-color: #5e72e4;">
			// 															<span id="progress_text">0%</span>
			// 														</div>
			// 													</div>
			// 												</div>
			// 												<div id="job_details" style="margin-top: 15px; font-size: 12px; color: #6c757d;">
			// 													<p>Job ID: <code>${jobId}</code></p>
			// 												</div>
			// 											</div>
			// 										</div>
			// 									`
			// 								}
			// 							],
			// 							primary_action_label: __("Close"),
			// 							primary_action: function() {
			// 								progress_dialog.hide();
			// 							}
			// 						});

			// 						progress_dialog.show();

			// 						// Function to update progress
			// 						const updateProgress = function(data) {
			// 							const statusEl = document.getElementById("job_status");
			// 							const stageEl = document.getElementById("job_stage");
			// 							const progressBar = document.getElementById("progress_bar");
			// 							const progressText = document.getElementById("progress_text");
			// 							const detailsEl = document.getElementById("job_details");

			// 							if (data.stage) {
			// 								stageEl.textContent = data.stage;
			// 							}

			// 							if (data.progress !== undefined) {
			// 								const progress = Math.min(100, Math.max(0, data.progress));
			// 								progressBar.style.width = progress + "%";
			// 								progressText.textContent = progress + "%";

			// 								if (progress === 100) {
			// 									progressBar.classList.remove("progress-bar-animated");
			// 									progressBar.style.backgroundColor = "#28a745";
			// 								}
			// 							}

			// 							if (data.stage === "Completed") {
			// 								statusEl.textContent = "Completed";
			// 								statusEl.style.color = "#28a745";
			// 								detailsEl.innerHTML = `
			// 									<p><strong>Job Completed Successfully!</strong></p>
			// 									<p>Recreated SLEs: <strong>${data.recreated_count || 0}</strong></p>
			// 									<p>Skipped: <strong>${data.skipped_count || 0}</strong></p>
			// 									<p>Repost Entries Created: <strong>${data.repost_count || 0}</strong></p>
			// 									<p style="margin-top: 10px;">Job ID: <code>${jobId}</code></p>
			// 								`;
			// 								progressBar.style.width = "100%";
			// 								progressText.textContent = "100%";
			// 								progressBar.classList.remove("progress-bar-animated");
			// 								progressBar.style.backgroundColor = "#28a745";
			// 							} else if (data.stage === "Error") {
			// 								statusEl.textContent = "Error";
			// 								statusEl.style.color = "#dc3545";
			// 								detailsEl.innerHTML = `
			// 									<p style="color: #dc3545;"><strong>Error occurred!</strong></p>
			// 									<p>${data.error || "Unknown error"}</p>
			// 									<p style="margin-top: 10px;">Job ID: <code>${jobId}</code></p>
			// 								`;
			// 								progressBar.style.backgroundColor = "#dc3545";
			// 							} else if (data.doctype) {
			// 								detailsEl.innerHTML = `
			// 									<p>Processing: <strong>${data.doctype}</strong></p>
			// 									<p>Progress: ${data.progress || 0} / ${data.total || 0}</p>
			// 									<p style="margin-top: 10px;">Job ID: <code>${jobId}</code></p>
			// 								`;
			// 							}
			// 						};

			// 						// Listen for progress updates
			// 						const realtimeListener = frappe.realtime.on("stock_maintenance_progress", function(data) {
			// 							updateProgress(data);

			// 							// Also show alert for completion/error
			// 							if (data.stage === "Completed") {
			// 								frappe.show_alert({
			// 									message: __("Process completed! Recreated: {0}, Skipped: {1}, Repost entries: {2}")
			// 										.replace("{0}", data.recreated_count || 0)
			// 										.replace("{1}", data.skipped_count || 0)
			// 										.replace("{2}", data.repost_count || 0),
			// 									indicator: "green"
			// 								}, 10);
			// 							} else if (data.stage === "Error") {
			// 								frappe.show_alert({
			// 									message: __("Error: {0}").replace("{0}", data.error || "Unknown error"),
			// 									indicator: "red"
			// 								}, 10);
			// 							}
			// 						});

			// 						// Clean up listener when dialog is closed
			// 						progress_dialog.onhide = function() {
			// 							if (realtimeListener) {
			// 								frappe.realtime.off("stock_maintenance_progress", realtimeListener);
			// 							}
			// 						};
			// 					}
			// 				}
			// 			});
			// 		},
			// 		function() {
			// 			// No - cancel
			// 		}
			// 	);
			// }, __("Actions"));
		}
	}
});
