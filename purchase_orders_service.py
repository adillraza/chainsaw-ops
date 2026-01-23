import os
import json
from google.cloud import bigquery
from google.oauth2 import service_account
from flask import current_app
from datetime import datetime

class BigQueryService:
    def __init__(self):
        self.client = None
        self.project_id = "chainsawspares-385722"
        self.credentials_path = None
        self._initialize_client()
    
    def _initialize_client(self):
        """Initialize BigQuery client with service account credentials"""
        try:
            # Try to get credentials from environment variable first
            credentials_path = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
            
            if credentials_path and os.path.exists(credentials_path):
                self.credentials_path = credentials_path
                credentials = service_account.Credentials.from_service_account_file(credentials_path)
            else:
                # Fallback to hardcoded credentials (for development)
                # In production, use environment variables
                service_account_info = {
                    "type": "service_account",
                    "project_id": "chainsawspares-385722",
                    "private_key_id": "237a3a78a461bfda00790ac3be42932bb3351eda",
                    "private_key": "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQCtk8XBIXrJjtfE\nhuUSbFA26bf/A8skr1v4WHGglT42Fw8/NtG7460GuAyAMB39mF0pvNAacbhc9F5X\nvJY+xjsNThLEZe23Ht8hTBn+L8YyfPlQ3TQlpwlG+hiGyslkQtCIUrEAD04PK4Bs\nYTc2ikuvT090WEzKRu6+UrtudyTEu8dgj6L5yKL7kzdQE8Cfb/P8VS0E4Gy6vP1y\ndq06reQjaiSA1ek5kQ00PsmKHNEnA7siTd5vmyMaKSoDDSrDf5at25vIt36RezZJ\n8bBQnB5IlJnEm39I7JyTJ8soUqbrKLU/dXnxsl94F7pWWGaKx/nxMkTnl3G6cFYs\n7g1WCQv3AgMBAAECggEAU9XjNkA6434MBd5XZpoM9jDVTyTgaZQO+jiRjQt4VDy3\n/wK1syeoqu2bEBDtc04zdRS+eH9DmhXnOT4bXS92VxtF4jkO4xrUj2sHxOcDNeB5\ntI5agPMX/oGN9hCcy8GfobA60DoULCyJJw+fUMbj/kTyrdF2KV6wMhmYcKa4ChKS\nFVkM2n9IvX+US/IAwTXl1Wd7pjYCk3y23nn7bpvXVD+xmZ49D7nj9K4RSe+eCqzU\nRcmeW536UnpTXWPE/14ZaltBnI3cpCIhgkDMMLZRxTC0jZahb+0ZJZ6lKGOfwA7V\ntAT6NfxTVFXNHc3k2HIt6UKCYPhh1j+wGUmwPgCxyQKBgQDgismOG4di2/sXCu64\nOcRI7oid+L1RIqhVAeO8ZdtWjMyYS5kdhvXcjIzmyJ8LxT82djywtBWdxkYIHGgu\nd3TTeFC8WBMepGVBGOBToo8ePRJXxX32QMWNr/44a/0pMc5NGY1ZU94GNrHFXEPF\nGGAsXw92EwBeZil8uqjuyTt2KQKBgQDF5SB+sp8Oa0A7YiwuO67GANggq7IRyr0o\n4H/GINK/aDVwBS6znwryXCYOUMJl92WaWIwwgCbquuOt+uL3L0DoE6NSYdNsLtC5\nE+KoIPePZrJHo6jcFSpJ3kZvfFfBJxe+Lkm+GBCVFP0EW8xxnT7CfcnMjMZAeydN\nDvTRTM11HwKBgBxKQCSwYTlaX+NCVFtPo+RQcVP939RWGoFDUK8O4jigWrKha7Ql\nqdpUtvfotOklu9YfxXu55LNRJMem1JVuJYDzOrDQI/CLY9p0yOagp4l2xlXaowkg\nlgNI9i2jpWzIQqbCHmXvxBxiTbmA62TssklE3MzSjgxWsMlvNxOGtQApAoGBAIdp\n4xNvTLGEKD66TbsRMeXhfALXGhFcppWLEUPVAADj4PUXRr64jLgM4CmZj3xQjqDQ\nCJXDi13BprxNWfOEzShBD9f2gsKbQk3y0nzzmhLxVfr5fTmj7fz/8TimYDIWewOz\nDtjaBLbO1tedrUbtL53Mj0K97Yb//oOXQWTa5hhHAoGAKHcfbKAnpq0RVbQhcwhg\np9bODjoSWbc1xFz7YxMzkfmsJaj0Y4txanlTW9ZgfWwqxNBLWVWw/N5joW5GHBeU\noTYdj8heke6ZN5060EVuYfeFyIkQa1o4jgi+bjWHkUlkP21Ik6PsIg1Io9cF/eAD\nwi/kSa5ApxYVjycop8nTMY4=\n-----END PRIVATE KEY-----\n",
                    "client_email": "airbyte@chainsawspares-385722.iam.gserviceaccount.com",
                    "client_id": "116822538674813489800",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                    "client_x509_cert_url": "https://www.googleapis.com/robot/v1/metadata/x509/airbyte%40chainsawspares-385722.iam.gserviceaccount.com",
                    "universe_domain": "googleapis.com"
                }
                credentials = service_account.Credentials.from_service_account_info(service_account_info)
            
            self.client = bigquery.Client(credentials=credentials, project=self.project_id)
            print(f"BigQuery client initialized successfully for project: {self.project_id}")
            
        except Exception as e:
            print(f"Error initializing BigQuery client: {str(e)}")
            self.client = None
    
    def get_purchase_order_summary(self, limit=100, offset=0, search_term=None):
        """Fetch purchase order summary data from ops_po table (pre-aggregated)"""
        if not self.client:
            return None, "BigQuery client not initialized"
        
        try:
            # Add search filter if provided
            where_clause = ""
            if search_term:
                where_clause = f"""
                WHERE CAST(po_id AS STRING) LIKE '%{search_term}%' 
                   OR CAST(order_id AS STRING) LIKE '%{search_term}%'
                """
            
            # Build the query from ops_po table with latest PO notes
            query = f"""
            WITH po_summary AS (
                SELECT 
                    CAST(po_id AS STRING) as po_id,
                    po_status,
                    rex_po_created_by,
                    supplier,
                    requested_date,
                    order_id as OrderID,
                    CONCAT('https://www.chainsawspares.com.au/_cpanel/order/vieworder?id=', order_id) as order_link,
                    entered_in_neto as entered_date,
                    received_date,
                    neto_entered_by as neto_order_created_by,
                    completed_date,
                    neto_complete_status as completion_status,
                    neto_order_status as order_status,
                    disparity,
                    po_item_count as item_count,
                    po_item_qty as total_quantity_ordered,
                    no_of_neto_orders,
                    neto_order_ids
                FROM `{self.project_id}.dataform.ops_po`
                {where_clause}
            ),
            latest_po_notes AS (
                SELECT 
                    CAST(po_id AS STRING) as po_id,
                    comment as latest_po_note,
                    username as latest_po_note_user,
                    created_at as latest_po_note_date
                FROM (
                    SELECT 
                        po_id,
                        comment,
                        username,
                        created_at,
                        ROW_NUMBER() OVER (PARTITION BY CAST(po_id AS STRING) ORDER BY created_at DESC) as rn
                    FROM `{self.project_id}.operations.item_notes`
                    WHERE po_item_id = 'PO' 
                    AND deleted_at IS NULL
                )
                WHERE rn = 1
            )
            SELECT 
                ps.*,
                lpn.latest_po_note,
                lpn.latest_po_note_user,
                lpn.latest_po_note_date
            FROM po_summary ps
            LEFT JOIN latest_po_notes lpn ON ps.po_id = lpn.po_id
            ORDER BY SAFE_CAST(ps.po_id AS INT64) DESC
            """
            
            # Add LIMIT only if specified
            if limit is not None:
                query += f" LIMIT {limit} OFFSET {offset}"
            
            query_job = self.client.query(query)
            results = query_job.result()
            
            # Convert to list of dictionaries
            data = []
            for row in results:
                row_dict = dict(row)
                # Convert any datetime objects to strings for JSON serialization
                for key, value in row_dict.items():
                    if hasattr(value, 'isoformat'):
                        row_dict[key] = value.isoformat()
                data.append(row_dict)
            
            # Debug: Print first row to check if po_status is present
            if data and len(data) > 0:
                print(f"DEBUG: First row keys: {data[0].keys()}")
                print(f"DEBUG: po_status value: {data[0].get('po_status', 'KEY NOT FOUND')}")
            
            return data, None
        
        except Exception as e:
            return None, f"Error fetching summary data: {str(e)}"
    
    def get_purchase_order_summary_by_sku(self, sku_search_term):
        """Fetch purchase order summary data filtered by SKU search term"""
        if not self.client:
            return None, "BigQuery client not initialized"
        
        try:
            # Build the summary query with SKU filtering
            query = f"""
            WITH po_summary AS (
                SELECT 
                    COALESCE(CAST(r.po_id AS STRING), r.PurchaseOrderNumber) as po_id,
                    ops.supplier,
                    r.requested_date,
                    r.OrderID,
                    CONCAT('https://www.chainsawspares.com.au/_cpanel/order/vieworder?id=', r.OrderID) as order_link,
                    r.entered_in_neto as entered_date,
                    r.received_date,
                    r.neto_entered_by as neto_order_created_by,
                    r.completed_date,
                    r.neto_complete_status as completion_status,
                    r.neto_order_status as order_status,
                    ROUND(SUM(r.rex_supplier_buy_ex), 2) - ROUND(SUM(r.neto_cost_price), 2) as difference,
                    CASE 
                        WHEN ABS(ROUND(SUM(r.rex_supplier_buy_ex), 2) - ROUND(SUM(r.neto_cost_price), 2)) > 0.01 THEN true 
                        ELSE false 
                    END as has_disparity,
                    MAX(r.modified_on) as modified_on
                FROM `{self.project_id}.dataform.neto_rex_purchase_order_report` r
                LEFT JOIN `{self.project_id}.dataform.ops_po` ops
                    ON COALESCE(CAST(r.po_id AS STRING), r.PurchaseOrderNumber) = CAST(ops.po_id AS STRING)
                WHERE r.manufacturer_sku LIKE '%{sku_search_term}%'
                GROUP BY COALESCE(CAST(r.po_id AS STRING), r.PurchaseOrderNumber), r.PurchaseOrderNumber, r.requested_date, r.OrderID, r.entered_in_neto, 
                         r.received_date, r.neto_entered_by, r.completed_date, r.neto_complete_status, r.neto_order_status, ops.supplier
            ),
            latest_po_notes AS (
                SELECT 
                    CAST(po_id AS STRING) as po_id,
                    comment as latest_po_note,
                    username as latest_po_note_user,
                    created_at as latest_po_note_date,
                    ROW_NUMBER() OVER (PARTITION BY CAST(po_id AS STRING) ORDER BY created_at DESC) as rn
                FROM `{self.project_id}.operations.item_notes`
                WHERE po_item_id = 'PO' AND deleted_at IS NULL
            )
            SELECT 
                ps.*,
                lpn.latest_po_note,
                lpn.latest_po_note_user,
                lpn.latest_po_note_date
            FROM po_summary ps
            LEFT JOIN latest_po_notes lpn ON ps.po_id = lpn.po_id AND lpn.rn = 1
            ORDER BY SAFE_CAST(ps.po_id AS INT64) DESC
            """
            
            print(f"Executing SKU search query for: {sku_search_term}")
            query_job = self.client.query(query)
            results = query_job.result()
            
            data = []
            for row in results:
                # Helper function to safely format dates
                def safe_isoformat(date_obj):
                    if date_obj is None:
                        return None
                    if hasattr(date_obj, 'isoformat'):
                        return date_obj.isoformat()
                    return str(date_obj)
                
                data.append({
                    'po_id': str(row.po_id),
                    'requested_date': safe_isoformat(row.requested_date),
                    'order_id': row.OrderID,
                    'order_link': row.order_link,
                    'supplier': row.supplier,
                    'entered_date': safe_isoformat(row.entered_date),
                    'received_date': safe_isoformat(row.received_date),
                    'neto_order_created_by': row.neto_order_created_by,
                    'completed_date': safe_isoformat(row.completed_date),
                    'completion_status': row.completion_status,
                    'order_status': row.order_status,
                    'difference': float(row.difference) if row.difference else 0.0,
                    'has_disparity': row.has_disparity,
                    'modified_on': safe_isoformat(row.modified_on),
                    'latest_po_note': row.latest_po_note,
                    'latest_po_note_user': row.latest_po_note_user,
                    'latest_po_note_date': safe_isoformat(row.latest_po_note_date)
                })
            
            print(f"Found {len(data)} POs with SKU matching '{sku_search_term}'")
            return data, None
            
        except Exception as e:
            return None, f"Error fetching summary data by SKU: {str(e)}"
    
    def get_all_purchase_order_items_by_sku(self, sku_search_term):
        """Fetch all purchase order items data filtered by SKU search term"""
        if not self.client:
            return None, "BigQuery client not initialized"
        
        try:
            # Build the items query with SKU filtering
            query = f"""
            WITH latest_item_notes AS (
                SELECT 
                    CAST(po_item_id AS STRING) as po_item_id,
                    comment as latest_item_note,
                    username as latest_item_note_user,
                    created_at as latest_item_note_date,
                    ROW_NUMBER() OVER (PARTITION BY CAST(po_item_id AS STRING) ORDER BY created_at DESC) as rn
                FROM `{self.project_id}.operations.item_notes`
                WHERE po_item_id != 'PO' AND deleted_at IS NULL
            )
            SELECT DISTINCT
                COALESCE(CAST(r.po_id AS STRING), r.PurchaseOrderNumber) as po_id,
                r.po_item_id,
                r.manufacturer_sku as sku,
                r.supplier_sku,
                r.manufacturer_sku,
                r.short_description,
                r.quantity_ordered as neto_qty_ordered,
                r.quantity_ordered as rex_qty_ordered,
                r.quantity_received as rex_qty_received,
                r.neto_qty_available,
                ROUND(r.neto_cost_price, 2) as neto_cost_price,
                ROUND(r.rex_supplier_buy_ex, 2) as rex_supplier_buy_ex,
                ROUND(r.rex_supplier_buy_ex, 2) - ROUND(r.neto_cost_price, 2) as diff,
                CASE 
                    WHEN ABS(ROUND(r.rex_supplier_buy_ex, 2) - ROUND(r.neto_cost_price, 2)) > 0.01 THEN true 
                    ELSE false 
                END as has_disparity,
                r.modified_on,
                lin.latest_item_note,
                lin.latest_item_note_user,
                lin.latest_item_note_date
            FROM `{self.project_id}.dataform.neto_rex_purchase_order_report` r
            LEFT JOIN latest_item_notes lin ON CAST(r.po_item_id AS STRING) = lin.po_item_id AND lin.rn = 1
            WHERE r.manufacturer_sku LIKE '%{sku_search_term}%'
            ORDER BY r.modified_on DESC
            """
            
            print(f"Executing SKU search query for items: {sku_search_term}")
            query_job = self.client.query(query)
            results = query_job.result()
            
            data = []
            for row in results:
                # Helper function to safely format dates
                def safe_isoformat(date_obj):
                    if date_obj is None:
                        return None
                    if hasattr(date_obj, 'isoformat'):
                        return date_obj.isoformat()
                    return str(date_obj)
                
                data.append({
                    'po_id': str(row.po_id),
                    'po_item_id': str(row.po_item_id) if row.po_item_id else None,
                    'sku': row.sku,
                    'supplier_sku': row.supplier_sku,
                    'manufacturer_sku': row.manufacturer_sku,
                    'short_description': row.short_description,
                    'neto_qty_available': int(row.neto_qty_available) if row.neto_qty_available else 0,
                    'neto_qty_ordered': int(row.neto_qty_ordered) if row.neto_qty_ordered else 0,
                    'rex_qty_ordered': int(row.rex_qty_ordered) if row.rex_qty_ordered else 0,
                    'rex_qty_received': int(row.rex_qty_received) if row.rex_qty_received else 0,
                    'neto_cost_price': float(row.neto_cost_price) if row.neto_cost_price else 0.0,
                    'rex_supplier_buy_ex': float(row.rex_supplier_buy_ex) if row.rex_supplier_buy_ex else 0.0,
                    'diff': float(row.diff) if row.diff else 0.0,
                    'has_disparity': row.has_disparity,
                    'modified_on': safe_isoformat(row.modified_on),
                    'latest_item_note': row.latest_item_note,
                    'latest_item_note_user': row.latest_item_note_user,
                    'latest_item_note_date': safe_isoformat(row.latest_item_note_date)
                })
            
            print(f"Found {len(data)} items with SKU matching '{sku_search_term}'")
            return data, None
            
        except Exception as e:
            return None, f"Error fetching items data by SKU: {str(e)}"
    
    def get_purchase_order_items(self, po_id=None, order_id=None, limit=None, offset=0):
        """Fetch purchase order items data for a specific PO or Order ID"""
        if not self.client:
            return None, "BigQuery client not initialized"
        
        try:
            # Build the items query with calculated fields
            base_query = f"""
            SELECT DISTINCT
                COALESCE(CAST(po_id AS STRING), PurchaseOrderNumber) as po_id,
                po_item_id,
                manufacturer_sku as sku,
                supplier_sku,
                manufacturer_sku,
                short_description,
                quantity_ordered as neto_qty_ordered,
                quantity_ordered as rex_qty_ordered,
                quantity_received as rex_qty_received,
                neto_qty_available,
                ROUND(neto_cost_price, 2) as neto_cost_price,
                ROUND(rex_supplier_buy_ex, 2) as rex_supplier_buy_ex,
                ROUND(rex_supplier_buy_ex, 2) - ROUND(neto_cost_price, 2) as difference,
                CASE 
                    WHEN ABS(ROUND(rex_supplier_buy_ex, 2) - ROUND(neto_cost_price, 2)) > 0 
                    THEN true 
                    ELSE false 
                END as disparity,
                OrderID,
                created_on,
                modified_on
            FROM `{self.project_id}.dataform.neto_rex_purchase_order_report`
            """
            
            # Add filter for specific PO or Order ID
            where_clause = ""
            if po_id:
                where_clause = f"WHERE CAST(COALESCE(CAST(po_id AS STRING), PurchaseOrderNumber) AS STRING) = '{po_id}'"
            elif order_id:
                where_clause = f"WHERE CAST(OrderID AS STRING) = '{order_id}'"
            
            # Complete query with ordering, optional pagination, and latest item note
            query = f"""
            WITH items_data AS (
                {base_query}
                {where_clause}
            ),
            latest_item_notes AS (
                SELECT 
                    CAST(po_item_id AS STRING) as po_item_id,
                    comment as latest_item_note,
                    username as latest_item_note_user,
                    created_at as latest_item_note_date
                FROM (
                    SELECT 
                        po_item_id,
                        comment,
                        username,
                        created_at,
                        ROW_NUMBER() OVER (PARTITION BY CAST(po_item_id AS STRING) ORDER BY created_at DESC) as rn
                    FROM `{self.project_id}.operations.item_notes`
                    WHERE po_item_id IS NOT NULL 
                    AND po_item_id != 'PO'
                    AND deleted_at IS NULL
                )
                WHERE rn = 1
            )
            SELECT 
                id.*,
                lin.latest_item_note,
                lin.latest_item_note_user,
                lin.latest_item_note_date
            FROM items_data id
            LEFT JOIN latest_item_notes lin ON CAST(id.po_item_id AS STRING) = lin.po_item_id
            ORDER BY id.po_item_id
            """
            
            # Add LIMIT only if specified
            if limit is not None:
                query += f" LIMIT {limit} OFFSET {offset}"
            
            query_job = self.client.query(query)
            results = query_job.result()
            
            # Convert to list of dictionaries
            data = []
            for row in results:
                row_dict = dict(row)
                # Convert any datetime objects to strings for JSON serialization
                for key, value in row_dict.items():
                    if hasattr(value, 'isoformat'):
                        row_dict[key] = value.isoformat()
                data.append(row_dict)
            
            return data, None
            
        except Exception as e:
            return None, f"Error fetching items data: {str(e)}"
    
    def get_all_purchase_order_items(self, limit=None, offset=0):
        """Fetch all purchase order items data (no PO/Order ID filter)"""
        if not self.client:
            return None, "BigQuery client not initialized"
        
        try:
            # Build the items query with calculated fields and notes - same as get_purchase_order_items but without WHERE clause
            base_query = f"""
            SELECT DISTINCT
                COALESCE(CAST(po_id AS STRING), PurchaseOrderNumber) as po_id,
                po_item_id,
                manufacturer_sku as sku,
                supplier_sku,
                manufacturer_sku,
                short_description,
                quantity_ordered as neto_qty_ordered,
                quantity_ordered as rex_qty_ordered,
                quantity_received as rex_qty_received,
                neto_qty_available,
                ROUND(neto_cost_price, 2) as neto_cost_price,
                ROUND(rex_supplier_buy_ex, 2) as rex_supplier_buy_ex,
                ROUND(rex_supplier_buy_ex, 2) - ROUND(neto_cost_price, 2) as difference,
                CASE 
                    WHEN ABS(ROUND(rex_supplier_buy_ex, 2) - ROUND(neto_cost_price, 2)) > 0 
                    THEN true 
                    ELSE false 
                END as disparity,
                OrderID,
                created_on,
                modified_on
            FROM `{self.project_id}.dataform.neto_rex_purchase_order_report`
            """
            
            # Complete query with ordering, optional pagination, and latest item note
            query = f"""
            WITH items_data AS (
                {base_query}
            ),
            latest_item_notes AS (
                SELECT 
                    CAST(po_item_id AS STRING) as po_item_id,
                    comment as latest_item_note,
                    username as latest_item_note_user,
                    created_at as latest_item_note_date
                FROM (
                    SELECT 
                        po_item_id,
                        comment,
                        username,
                        created_at,
                        ROW_NUMBER() OVER (PARTITION BY CAST(po_item_id AS STRING) ORDER BY created_at DESC) as rn
                    FROM `{self.project_id}.operations.item_notes`
                    WHERE po_item_id IS NOT NULL 
                    AND po_item_id != 'PO'
                    AND deleted_at IS NULL
                )
                WHERE rn = 1
            )
            SELECT 
                id.*,
                lin.latest_item_note,
                lin.latest_item_note_user,
                lin.latest_item_note_date
            FROM items_data id
            LEFT JOIN latest_item_notes lin ON CAST(id.po_item_id AS STRING) = lin.po_item_id
            ORDER BY id.po_item_id
            """
            
            # Add LIMIT only if specified
            if limit is not None:
                query += f" LIMIT {limit} OFFSET {offset}"
            
            query_job = self.client.query(query)
            results = query_job.result()
            
            # Convert to list of dictionaries
            data = []
            for row in results:
                row_dict = dict(row)
                # Convert any datetime objects to strings for JSON serialization
                for key, value in row_dict.items():
                    if hasattr(value, 'isoformat'):
                        row_dict[key] = value.isoformat()
                data.append(row_dict)
            
            return data, None
            
        except Exception as e:
            return None, f"Error fetching all items data: {str(e)}"
    
    def get_summary_count(self, search_term=None):
        """Get total count of unique POs for summary pagination"""
        if not self.client:
            return 0, "BigQuery client not initialized"
        
        try:
            base_query = f"""
            SELECT COUNT(*) as total
            FROM `{self.project_id}.dataform.ops_po`
            """
            
            where_clause = ""
            if search_term:
                where_clause = f"""
                WHERE CAST(po_id AS STRING) LIKE '%{search_term}%' 
                   OR CAST(order_id AS STRING) LIKE '%{search_term}%'
                """
            
            query = f"{base_query} {where_clause}"
            
            query_job = self.client.query(query)
            results = list(query_job.result())
            
            if results:
                return results[0].total, None
            else:
                return 0, None
                
        except Exception as e:
            return 0, f"Error getting summary count: {str(e)}"
    
    def get_items_count(self):
        """Get total count of items for progress tracking"""
        if not self.client:
            return 0, "BigQuery client not initialized"
        
        try:
            query = f"""
            SELECT COUNT(*) as total
            FROM `{self.project_id}.dataform.neto_rex_purchase_order_report`
            WHERE po_item_id IS NOT NULL
            """
            
            query_job = self.client.query(query)
            results = list(query_job.result())
            
            if results:
                return results[0].total, None
            else:
                return 0, None
                
        except Exception as e:
            return 0, f"Error getting items count: {str(e)}"
    
    def get_comparison_count(self):
        """Get total count of comparison records for progress tracking"""
        if not self.client:
            return 0, "BigQuery client not initialized"
        
        try:
            query = f"""
            SELECT COUNT(*) as total
            FROM `{self.project_id}.dataform.neto_rex_purchase_order_compared`
            """
            
            query_job = self.client.query(query)
            results = list(query_job.result())
            
            if results:
                return results[0].total, None
            else:
                return 0, None
                
        except Exception as e:
            return 0, f"Error getting comparison count: {str(e)}"
    
    def get_table_schema(self):
        """Get the schema of the purchase order table"""
        if not self.client:
            return None, "BigQuery client not initialized"
        
        try:
            table_ref = self.client.dataset('dataform').table('neto_rex_purchase_order_report')
            table = self.client.get_table(table_ref)
            
            schema_info = []
            for field in table.schema:
                schema_info.append({
                    'name': field.name,
                    'type': field.field_type,
                    'mode': field.mode,
                    'description': field.description
                })
            
            return schema_info, None
            
        except Exception as e:
            return None, f"Error fetching schema: {str(e)}"
    
    def test_connection(self):
        """Test the BigQuery connection"""
        if not self.client:
            return False, "BigQuery client not initialized"
        
        try:
            # Simple query to test connection
            query = "SELECT 1 as test"
            query_job = self.client.query(query)
            results = list(query_job.result())
            
            if results and results[0].test == 1:
                return True, "Connection successful"
            else:
                return False, "Connection test failed"
                
        except Exception as e:
            return False, f"Connection test failed: {str(e)}"

    def get_all_purchase_order_comparison_by_sku(self, sku_search_term):
        """Fetch all purchase order comparison data filtered by SKU search term"""
        if not self.client:
            return None, "BigQuery client not initialized"
        
        try:
            # Build the comparison query with SKU filtering
            # po_item_id is now directly in the comparison table
            query = f"""
            SELECT DISTINCT
                c.po_id,
                c.po_item_id,
                c.manufacturer_sku,
                c.short_description,
                c.change_type,
                c.rex_available_qty,
                c.neto_qty_available,
                c.original_po_quantity_ordered,
                c.neto_quantity_shipped,
                c.latest_po_quantity_ordered,
                c.quantity_received,
                c.OrderID
            FROM `{self.project_id}.dataform.neto_rex_purchase_order_compared` c
            WHERE c.manufacturer_sku LIKE '%{sku_search_term}%'
            ORDER BY c.po_id, c.manufacturer_sku
            """
            
            print(f"Executing SKU search query for comparison: {sku_search_term}")
            query_job = self.client.query(query)
            results = query_job.result()
            
            data = []
            for row in results:
                # Helper function to safely format dates
                def safe_isoformat(date_obj):
                    if date_obj is None:
                        return None
                    if hasattr(date_obj, 'isoformat'):
                        return date_obj.isoformat()
                    return str(date_obj)
                
                data.append({
                    'po_id': str(row.po_id),
                    'order_id': row.OrderID,
                    'sku': row.manufacturer_sku,
                    'name': row.short_description,
                    'change_log': row.change_type,
                    'rex_available_qty': float(row.rex_available_qty) if row.rex_available_qty else 0.0,
                    'neto_qty_available': float(row.neto_qty_available) if row.neto_qty_available else 0.0,
                    'original_rex_qty_ordered': float(row.original_po_quantity_ordered) if row.original_po_quantity_ordered else 0.0,
                    'neto_qty_shipped': float(row.neto_quantity_shipped) if row.neto_quantity_shipped else 0.0,
                    'final_rex_qty_ordered': float(row.latest_po_quantity_ordered) if row.latest_po_quantity_ordered else 0.0,
                    'rex_qty_received': float(row.quantity_received) if row.quantity_received else 0.0,
                    'po_item_id': str(row.po_item_id) if row.po_item_id else None
                })
            
            print(f"Found {len(data)} comparison records with SKU matching '{sku_search_term}'")
            return data, None
            
        except Exception as e:
            return None, f"Error fetching comparison data by SKU: {str(e)}"

    def get_purchase_order_comparison(self, po_id=None, order_id=None, limit=None, offset=0):
        """Get comparison data from dataform.neto_rex_purchase_order_compared table"""
        if not self.client:
            return [], "BigQuery client not initialized"
        
        try:
            # Build the query with deduplication, joining with notes to get latest notes
            # po_item_id is now directly in the comparison table
            query = """
            SELECT DISTINCT
                c.po_id,
                c.po_item_id,
                c.manufacturer_sku,
                c.short_description,
                c.change_type,
                c.rex_available_qty,
                c.neto_qty_available,
                c.original_po_quantity_ordered,
                c.neto_quantity_shipped,
                c.latest_po_quantity_ordered,
                c.quantity_received,
                c.OrderID,
                n.comment as latest_item_note,
                n.username as latest_item_note_user,
                n.created_at as latest_item_note_date
            FROM `chainsawspares-385722.dataform.neto_rex_purchase_order_compared` c
            LEFT JOIN (
                SELECT 
                    po_item_id,
                    comment,
                    username,
                    created_at,
                    ROW_NUMBER() OVER (PARTITION BY po_item_id ORDER BY created_at DESC) as rn
                FROM `chainsawspares-385722.operations.item_notes`
                WHERE deleted_at IS NULL
            ) n ON CAST(c.po_item_id AS STRING) = n.po_item_id AND n.rn = 1
            WHERE 1=1
            """
            
            # Add filters
            if po_id:
                query += f" AND c.po_id = {po_id}"
            # Note: comparison table doesn't have order_id field, so we only filter by po_id
            
            # Add ordering and limits
            query += " ORDER BY c.po_id, c.manufacturer_sku"
            
            if limit:
                query += f" LIMIT {limit}"
            if offset:
                query += f" OFFSET {offset}"
            
            # Execute query
            query_job = self.client.query(query)
            results = list(query_job.result())
            
            # Convert to list of dictionaries with mapped field names
            data = []
            for row in results:
                data.append({
                    'po_id': str(row.po_id),
                    'modified_on': None,  # No modified_on field in comparison table - this is comparison data, not transaction data
                    'sku': row.manufacturer_sku,
                    'name': row.short_description,
                    'change_log': row.change_type,
                    'rex_available_qty': row.rex_available_qty,
                    'neto_qty_available': row.neto_qty_available,
                    'original_rex_qty_ordered': row.original_po_quantity_ordered,
                    'neto_qty_shipped': row.neto_quantity_shipped,
                    'final_rex_qty_ordered': row.latest_po_quantity_ordered,
                    'rex_qty_received': row.quantity_received,
                    'OrderID': row.OrderID,
                    'po_item_id': str(row.po_item_id) if row.po_item_id else None,
                    'latest_item_note': row.latest_item_note,
                    'latest_item_note_user': row.latest_item_note_user,
                    'latest_item_note_date': row.latest_item_note_date.isoformat() if row.latest_item_note_date else None
                })
            
            print(f"Found {len(data)} comparison records for po_id={po_id}")
            
            # Debug: Check po_item_id values in the data
            for i, record in enumerate(data[:3]):
                print(f"Backend Record {i+1}: po_id={record['po_id']}, sku={record['sku']}, po_item_id={record.get('po_item_id', 'MISSING')}")
            
            return data, None
            
        except Exception as e:
            return [], f"Error fetching comparison data: {str(e)}"
    
    def get_all_purchase_order_comparison(self, limit=None, offset=0):
        """Get all comparison data from dataform.neto_rex_purchase_order_compared table with deduplication"""
        if not self.client:
            return [], "BigQuery client not initialized"
        
        try:
            # Use DISTINCT to remove duplicates at the BigQuery level
            # po_item_id is now directly in the comparison table
            query = """
            SELECT DISTINCT
                c.po_id,
                c.po_item_id,
                c.manufacturer_sku,
                c.short_description,
                c.change_type,
                c.rex_available_qty,
                c.neto_qty_available,
                c.original_po_quantity_ordered,
                c.neto_quantity_shipped,
                c.latest_po_quantity_ordered,
                c.quantity_received,
                c.OrderID
            FROM `chainsawspares-385722.dataform.neto_rex_purchase_order_compared` c
            ORDER BY c.po_id, c.manufacturer_sku
            """
            
            if limit:
                query += f" LIMIT {limit}"
            if offset:
                query += f" OFFSET {offset}"
            
            query_job = self.client.query(query)
            results = list(query_job.result())
            
            data = []
            for row in results:
                data.append({
                    'po_id': str(row.po_id),
                    'modified_on': None,
                    'sku': row.manufacturer_sku,
                    'name': row.short_description,
                    'change_log': row.change_type,
                    'rex_available_qty': row.rex_available_qty,
                    'neto_qty_available': row.neto_qty_available,
                    'original_rex_qty_ordered': row.original_po_quantity_ordered,
                    'neto_qty_shipped': row.neto_quantity_shipped,
                    'final_rex_qty_ordered': row.latest_po_quantity_ordered,
                    'rex_qty_received': row.quantity_received,
                    'OrderID': row.OrderID,
                    'po_item_id': str(row.po_item_id) if row.po_item_id else None
                })
            
            print(f"Found {len(data)} comparison records for caching")
            
            # Debug: Check po_item_id values in the data
            for i, record in enumerate(data[:3]):
                print(f"Backend Record {i+1}: po_id={record['po_id']}, sku={record['sku']}, po_item_id={record.get('po_item_id', 'MISSING')}")
            
            return data, None
            
        except Exception as e:
            return [], f"Error fetching all comparison data: {str(e)}"
    
    def save_item_note(self, po_item_id, po_id, sku, comment, username):
        """Save a note for a specific item"""
        if not self.client:
            return False, "BigQuery client not initialized"
        
        try:
            import uuid
            from datetime import datetime
            import pytz
            
            # Generate unique note ID
            note_id = str(uuid.uuid4())
            
            # Get Melbourne timezone
            melbourne_tz = pytz.timezone('Australia/Melbourne')
            now = datetime.now(melbourne_tz)
            
            print(f"Saving note for po_item_id: {po_item_id}, po_id: {po_id}, sku: {sku}")
            print(f"Note will be saved with po_item_id='{po_item_id}' in database")
            
            # Insert note into BigQuery
            insert_query = """
            INSERT INTO `chainsawspares-385722.operations.item_notes`
            (note_id, po_item_id, po_id, sku, comment, username, created_at, updated_at)
            VALUES (@note_id, @po_item_id, @po_id, @sku, @comment, @username, @created_at, @updated_at)
            """
            
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("note_id", "STRING", note_id),
                    bigquery.ScalarQueryParameter("po_item_id", "STRING", po_item_id),
                    bigquery.ScalarQueryParameter("po_id", "STRING", po_id),
                    bigquery.ScalarQueryParameter("sku", "STRING", sku),
                    bigquery.ScalarQueryParameter("comment", "STRING", comment),
                    bigquery.ScalarQueryParameter("username", "STRING", username),
                    bigquery.ScalarQueryParameter("created_at", "TIMESTAMP", now),
                    bigquery.ScalarQueryParameter("updated_at", "TIMESTAMP", now)
                ]
            )
            
            query_job = self.client.query(insert_query, job_config=job_config)
            query_job.result()  # Wait for completion
            
            print(f"Note saved successfully for {po_item_id}")
            return True, "Note saved successfully"
            
        except Exception as e:
            print(f"Error saving note for {po_item_id}: {str(e)}")
            return False, f"Error saving note: {str(e)}"
    
    def save_po_note(self, po_id, order_id, comment, username):
        """Save a note for a specific PO (po_item_id will be NULL)"""
        if not self.client:
            return False, "BigQuery client not initialized"
        
        try:
            import uuid
            from datetime import datetime
            import pytz
            
            # Generate unique note ID
            note_id = str(uuid.uuid4())
            
            # Get Melbourne timezone
            melbourne_tz = pytz.timezone('Australia/Melbourne')
            now = datetime.now(melbourne_tz)
            
            # Insert note into BigQuery (po_item_id is NULL for PO-level notes)
            insert_query = """
            INSERT INTO `chainsawspares-385722.operations.item_notes`
            (note_id, po_item_id, po_id, sku, comment, username, created_at, updated_at)
            VALUES (@note_id, @po_item_id, @po_id, @sku, @comment, @username, @created_at, @updated_at)
            """
            
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("note_id", "STRING", note_id),
                    bigquery.ScalarQueryParameter("po_item_id", "STRING", "PO"),  # "PO" for PO-level notes
                    bigquery.ScalarQueryParameter("po_id", "STRING", po_id),
                    bigquery.ScalarQueryParameter("sku", "STRING", None),  # NULL for PO-level notes
                    bigquery.ScalarQueryParameter("comment", "STRING", comment),
                    bigquery.ScalarQueryParameter("username", "STRING", username),
                    bigquery.ScalarQueryParameter("created_at", "TIMESTAMP", now),
                    bigquery.ScalarQueryParameter("updated_at", "TIMESTAMP", now)
                ]
            )
            
            query_job = self.client.query(insert_query, job_config=job_config)
            query_job.result()  # Wait for completion
            
            return True, "PO note saved successfully"
            
        except Exception as e:
            return False, f"Error saving PO note: {str(e)}"
    
    def get_item_notes(self, po_item_id):
        """Get all notes for a specific item (excluding soft deleted)"""
        if not self.client:
            return [], "BigQuery client not initialized"
        
        try:
            print(f"Fetching notes for po_item_id: {po_item_id}")
            
            # Simple direct match since we're starting fresh
            query = """
            SELECT 
                note_id,
                po_item_id,
                po_id,
                sku,
                comment,
                username,
                created_at,
                updated_at,
                deleted_at,
                deleted_by
            FROM `chainsawspares-385722.operations.item_notes`
            WHERE po_item_id = @po_item_id 
            AND deleted_at IS NULL
            ORDER BY created_at DESC
            """
            
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("po_item_id", "STRING", str(po_item_id))
                ]
            )
            
            query_job = self.client.query(query, job_config=job_config)
            results = list(query_job.result())
            
            print(f"Found {len(results)} notes for {po_item_id}")
            
            notes = []
            for row in results:
                notes.append({
                    'note_id': row.note_id,
                    'po_item_id': str(row.po_item_id) if row.po_item_id else None,
                    'po_id': str(row.po_id),
                    'sku': row.sku,
                    'comment': row.comment,
                    'username': row.username,
                    'created_at': row.created_at.isoformat() if row.created_at else None,
                    'updated_at': row.updated_at.isoformat() if row.updated_at else None,
                    'deleted_at': row.deleted_at.isoformat() if row.deleted_at else None,
                    'deleted_by': row.deleted_by
                })
            
            return notes, None
            
        except Exception as e:
            print(f"Error fetching notes for {po_item_id}: {str(e)}")
            return [], f"Error fetching notes: {str(e)}"
    
    def get_all_item_notes_for_po(self, po_id):
        """Get all item notes for all items in a specific PO (excluding PO-level notes and soft deleted)"""
        if not self.client:
            return [], "BigQuery client not initialized"
        
        try:
            query = """
            SELECT 
                note_id,
                po_item_id,
                po_id,
                sku,
                comment,
                username,
                created_at,
                updated_at,
                deleted_at,
                deleted_by
            FROM `chainsawspares-385722.operations.item_notes`
            WHERE CAST(po_id AS STRING) = @po_id 
                AND po_item_id != 'PO'
                AND deleted_at IS NULL
            ORDER BY created_at DESC
            """
            
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("po_id", "STRING", str(po_id))
                ]
            )
            
            query_job = self.client.query(query, job_config=job_config)
            results = query_job.result()
            
            notes = []
            for row in results:
                notes.append({
                    'note_id': row.note_id,
                    'po_item_id': row.po_item_id,
                    'po_id': row.po_id,
                    'sku': row.sku,
                    'comment': row.comment,
                    'username': row.username,
                    'created_at': row.created_at.isoformat() if row.created_at else None,
                    'updated_at': row.updated_at.isoformat() if row.updated_at else None,
                    'deleted_at': row.deleted_at,
                    'deleted_by': row.deleted_by
                })
            
            print(f"Retrieved {len(notes)} item notes for PO {po_id}")
            return notes, None
            
        except Exception as e:
            error_msg = f"Error retrieving item notes for PO: {str(e)}"
            print(error_msg)
            return [], error_msg
    
    def get_po_notes(self, po_id):
        """Get all notes for a specific PO (po_item_id is NULL, excluding soft deleted)"""
        if not self.client:
            return [], "BigQuery client not initialized"
        
        try:
            query = """
            SELECT 
                note_id,
                po_item_id,
                po_id,
                sku,
                comment,
                username,
                created_at,
                updated_at,
                deleted_at,
                deleted_by
            FROM `chainsawspares-385722.operations.item_notes`
                WHERE po_id = @po_id 
                AND po_item_id = 'PO'
                AND deleted_at IS NULL
            ORDER BY created_at DESC
            """
            
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("po_id", "STRING", po_id)
                ]
            )
            
            query_job = self.client.query(query, job_config=job_config)
            results = list(query_job.result())
            
            notes = []
            for row in results:
                notes.append({
                    'note_id': row.note_id,
                    'po_item_id': str(row.po_item_id) if row.po_item_id else None,
                    'po_id': str(row.po_id),
                    'sku': row.sku,
                    'comment': row.comment,
                    'username': row.username,
                    'created_at': row.created_at.isoformat() if row.created_at else None,
                    'updated_at': row.updated_at.isoformat() if row.updated_at else None,
                    'deleted_at': row.deleted_at.isoformat() if row.deleted_at else None,
                    'deleted_by': row.deleted_by
                })
            
            return notes, None
            
        except Exception as e:
            return [], f"Error fetching PO notes: {str(e)}"

    def insert_item_review(self, review_data):
        """Insert a review row into BigQuery"""
        if not self.client:
            return False, "BigQuery client not initialized"
        try:
            table_id = f"{self.project_id}.operations.item_reviews"
            errors = self.client.insert_rows_json(table_id, [review_data])
            if errors:
                return False, errors
            return True, None
        except Exception as e:
            return False, str(e)
    
    def delete_item_note(self, note_id, username):
        """Soft delete a note"""
        if not self.client:
            return False, "BigQuery client not initialized"
        
        try:
            from datetime import datetime
            import pytz
            
            # Get Melbourne timezone
            melbourne_tz = pytz.timezone('Australia/Melbourne')
            now = datetime.now(melbourne_tz)
            
            # Update note with soft delete fields
            update_query = """
            UPDATE `chainsawspares-385722.operations.item_notes`
            SET deleted_at = @deleted_at,
                deleted_by = @deleted_by,
                updated_at = @updated_at
            WHERE note_id = @note_id
            """
            
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("note_id", "STRING", note_id),
                    bigquery.ScalarQueryParameter("deleted_at", "TIMESTAMP", now),
                    bigquery.ScalarQueryParameter("deleted_by", "STRING", username),
                    bigquery.ScalarQueryParameter("updated_at", "TIMESTAMP", now)
                ]
            )
            
            query_job = self.client.query(update_query, job_config=job_config)
            query_job.result()  # Wait for completion
            
            return True, "Note deleted successfully"
            
        except Exception as e:
            return False, f"Error deleting note: {str(e)}"

# Global instance
purchase_orders_service = BigQueryService()
