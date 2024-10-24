import frappe
import csv
import requests
from frappe.utils.file_manager import save_file
from frappe import _
from io import StringIO
import time
import random


@frappe.whitelist()
def validate_and_reconcile(doc):
    # Deserialize the document from JSON string if passed as a string
    if isinstance(doc, str):
        doc = frappe.get_doc(frappe.parse_json(doc))

    # Validation logic moved out of the background job
    genesis_doc = frappe.get_doc("RI Genesis", doc.genesis)
    smart_policy_doc = frappe.get_doc("RI Smart Policy", doc.smart_policy)

    # Fetch the correct file paths from the related documents
    genesis_file_path = frappe.get_site_path(genesis_doc.gs_report_upload.lstrip("/"))
    smart_policy_file_path = frappe.get_site_path(
        smart_policy_doc.sm_report_file.lstrip("/")
    )

    # Validate the file extensions to ensure they are CSV
    if not genesis_file_path.endswith(".csv") or not smart_policy_file_path.endswith(
        ".csv"
    ):
        frappe.throw(
            _("Both the Genesis and Smart Policy files must be in CSV format.")
        )

    # URLs for the templates
    genesis_template_url = "https://reliance-insurance.aakvaerp.com/files/Genesis%20Template%20Download.csv"
    smart_policy_template_url = (
        "https://reliance-insurance.aakvaerp.com/files/Smart%20Policy%20Template.csv"
    )

    # Fetch the Genesis template
    genesis_template_response = requests.get(genesis_template_url)
    if genesis_template_response.status_code != 200:
        frappe.throw(_("Unable to fetch the Genesis template."))

    genesis_template_content = genesis_template_response.content.decode("utf-8")
    genesis_template_reader = csv.DictReader(genesis_template_content.splitlines())
    genesis_template_columns = [
        col.strip() for col in genesis_template_reader.fieldnames
    ]

    # Fetch the Smart Policy template
    smart_policy_template_response = requests.get(smart_policy_template_url)
    if smart_policy_template_response.status_code != 200:
        frappe.throw(_("Unable to fetch the Smart Policy template."))

    smart_policy_template_content = smart_policy_template_response.content.decode(
        "utf-8"
    )
    smart_policy_template_reader = csv.DictReader(
        smart_policy_template_content.splitlines()
    )
    smart_policy_template_columns = [
        col.strip() for col in smart_policy_template_reader.fieldnames
    ]

    # Open both Genesis and Smart Policy CSV files for reading
    with (
        open(genesis_file_path, mode="r") as genesis_file,
        open(smart_policy_file_path, mode="r") as smart_policy_file,
    ):
        genesis_csv_reader = csv.DictReader(genesis_file)
        smart_policy_csv_reader = csv.DictReader(smart_policy_file)

        # Strip spaces from the actual columns in the Genesis and Smart Policy files
        actual_genesis_columns = [col.strip() for col in genesis_csv_reader.fieldnames]
        actual_smart_policy_columns = [
            col.strip() for col in smart_policy_csv_reader.fieldnames
        ]

        # Validate Genesis file columns against the template
        if actual_genesis_columns != genesis_template_columns:
            frappe.throw(
                _(
                    "The Genesis file does not match the required columns. Expected: {0}, Found: {1}"
                ).format(genesis_template_columns, actual_genesis_columns)
            )

        # Validate Smart Policy file columns against the template
        if actual_smart_policy_columns != smart_policy_template_columns:
            frappe.throw(
                _(
                    "The Smart Policy file does not match the required columns. Expected: {0}, Found: {1}"
                ).format(smart_policy_template_columns, actual_smart_policy_columns)
            )

    # After validation, enqueue the reconciliation process in the background
    frappe.enqueue(
        reconcile_background_job,
        doc=doc,
        genesis_file_path=genesis_file_path,
        smart_policy_file_path=smart_policy_file_path,
        queue="long",
        timeout=1500,
    )

    # Return a message to the frontend that the process has started
    return {
        "status": "success",
        "message": _(
            "Reconciliation process has been started and will take a few minutes."
        ),
    }


def reconcile_background_job(doc, genesis_file_path, smart_policy_file_path):
    # Fetch related Genesis and Smart Policy documents
    genesis_doc = frappe.get_doc("RI Genesis", doc.genesis)
    smart_policy_doc = frappe.get_doc("RI Smart Policy", doc.smart_policy)

    # Open both Genesis and Smart Policy CSV files for reading
    with (
        open(genesis_file_path, mode="r") as genesis_file,
        open(smart_policy_file_path, mode="r") as smart_policy_file,
    ):
        genesis_csv_reader = csv.DictReader(genesis_file)
        smart_policy_csv_reader = csv.DictReader(smart_policy_file)

        # Initialize lists to track reconciliation
        reconciled_data = []
        unreconciled_genesis_data = []
        unreconciled_smart_data = []
        matched_smart_policy = []

        # Reconciliation logic
        for genesis_row in genesis_csv_reader:
            matching_policy = None

            smart_policy_file.seek(0)  # Reset the Smart Policy file pointer

            for smart_row in smart_policy_csv_reader:
                if (
                    not matching_policy
                    and smart_row["Cover No"] == genesis_row["TXT VEHICLE COVERNOTE"]
                ):
                    matching_policy = smart_row
                    break

                if (
                    not matching_policy
                    and smart_row["Sticker No"] == genesis_row["TXT VEHICLESUBCLASS"]
                ):
                    matching_policy = smart_row
                    break

                if (
                    not matching_policy
                    and smart_row["Vehicle RegNo"] == genesis_row["TXT REGISTRATION NO"]
                ):
                    matching_policy = smart_row
                    break

                if smart_row["Policy No"] == genesis_row["TXT Policy No Char"]:
                    matching_policy = smart_row
                    break

            if matching_policy:
                reconciled_data.append({**genesis_row, **matching_policy})
                matched_smart_policy.append(matching_policy)
            else:
                unreconciled_genesis_data.append(genesis_row)

        # Find unmatched Smart Policy records
        smart_policy_file.seek(0)  # Reset Smart Policy file pointer
        for smart_row in smart_policy_csv_reader:
            if smart_row not in matched_smart_policy:
                unreconciled_smart_data.append(smart_row)

    # Update reconciliation details using frappe.db.set_value to avoid TimestampMismatchError
    frappe.db.set_value(
        "RI Reconciliation", doc.name, "total_reconciled", len(reconciled_data)
    )
    frappe.db.set_value(
        "RI Reconciliation",
        doc.name,
        "total_gs_unreconciled",
        len(unreconciled_genesis_data),
    )
    frappe.db.set_value(
        "RI Reconciliation",
        doc.name,
        "total_sm_un_reconciled",
        len(unreconciled_smart_data),
    )

    # Add a timestamp and random number to filenames to ensure they are unique
    timestamp = str(int(time.time()))  # Use UNIX timestamp to make the name unique
    random_number = str(
        random.randint(1000, 9999)
    )  # Add a random number for uniqueness

    # Save CSV files only if they contain data to prevent duplicates
    if reconciled_data:
        reconciled_file = save_file(
            f"{doc.name}_reconciled_{timestamp}_{random_number}.csv",
            generate_csv_string(reconciled_data).encode(
                "utf-8"
            ),  # Ensuring CSV data is correctly handled
            doc.doctype,
            doc.name,
            is_private=True,
        )
        frappe.db.set_value(
            "RI Reconciliation", doc.name, "reconciled_file", reconciled_file.file_url
        )

    if unreconciled_genesis_data:
        unreconciled_gs_file = save_file(
            f"{doc.name}_unreconciled_genesis_{timestamp}_{random_number}.csv",
            generate_csv_string(unreconciled_genesis_data).encode(
                "utf-8"
            ),  # Ensuring CSV data is correctly handled
            doc.doctype,
            doc.name,
            is_private=True,
        )
        frappe.db.set_value(
            "RI Reconciliation",
            doc.name,
            "gs_un_reconciled_file",
            unreconciled_gs_file.file_url,
        )

    if unreconciled_smart_data:
        unreconciled_sm_file = save_file(
            f"{doc.name}_unreconciled_smart_policy_{timestamp}_{random_number}.csv",
            generate_csv_string(unreconciled_smart_data).encode(
                "utf-8"
            ),  # Ensuring CSV data is correctly handled
            doc.doctype,
            doc.name,
            is_private=True,
        )
        frappe.db.set_value(
            "RI Reconciliation",
            doc.name,
            "sm_un_reconciled_file",
            unreconciled_sm_file.file_url,
        )

    # Mark the related Genesis and Smart Policy documents as reconciled
    frappe.db.set_value("RI Genesis", genesis_doc.name, "is_reconciled", 1)
    frappe.db.set_value("RI Smart Policy", smart_policy_doc.name, "is_reconciled", 1)

    frappe.db.commit()


# Function to generate CSV string from list of dictionaries
def generate_csv_string(data):
    """Convert list of dictionaries to CSV string."""
    if not data:
        return ""

    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=data[0].keys())
    writer.writeheader()
    writer.writerows(data)

    return output.getvalue()
