from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
import os
from dotenv import load_dotenv

# Load environment variables FIRST before importing services that need them
load_dotenv()

# Now import services that depend on environment variables
from purchase_orders_service import purchase_orders_service

# Global variable to store actual totals for progress tracking
actual_totals = {
    'summary_total': 0,
    'items_total': 0,
    'comparison_total': 0
}

# Global flag to track and control sync state
sync_state = {
    'is_running': False,
    'should_stop': False
}

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'your-secret-key-change-this-in-production')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///users.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Initialize extensions
db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access this page.'

# User model
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(120), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

# Login log model
class LoginLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    login_time = db.Column(db.DateTime, default=datetime.utcnow)
    ip_address = db.Column(db.String(45))
    user_agent = db.Column(db.String(200))
    
    user = db.relationship('User', backref=db.backref('login_logs', lazy=True))

# Cached Purchase Order Summary model
class CachedPurchaseOrderSummary(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    po_id = db.Column(db.String(50), nullable=False)
    po_status = db.Column(db.String(50))
    rex_po_created_by = db.Column(db.String(100))
    requested_date = db.Column(db.DateTime)
    order_id = db.Column(db.String(50))
    order_link = db.Column(db.String(200))
    entered_date = db.Column(db.DateTime)
    received_date = db.Column(db.DateTime)
    neto_order_created_by = db.Column(db.String(100))
    completed_date = db.Column(db.DateTime)
    completion_status = db.Column(db.String(50))
    order_status = db.Column(db.String(50))
    difference = db.Column(db.Float)
    disparity = db.Column(db.Boolean)
    item_count = db.Column(db.Integer)
    total_quantity_ordered = db.Column(db.Integer)
    total_quantity_received = db.Column(db.Integer)
    total_rex_cost = db.Column(db.Float)
    total_neto_cost = db.Column(db.Float)
    # Notes columns
    latest_po_note = db.Column(db.Text)
    latest_po_note_user = db.Column(db.String(100))
    latest_po_note_date = db.Column(db.DateTime)
    cached_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def to_dict(self):
        return {
            'po_id': self.po_id,
            'po_status': self.po_status,
            'rex_po_created_by': self.rex_po_created_by,
            'requested_date': self.requested_date.isoformat() if self.requested_date else None,
            'OrderID': self.order_id,
            'order_link': self.order_link,
            'entered_date': self.entered_date.isoformat() if self.entered_date else None,
            'received_date': self.received_date.isoformat() if self.received_date else None,
            'neto_order_created_by': self.neto_order_created_by,
            'completed_date': self.completed_date.isoformat() if self.completed_date else None,
            'completion_status': self.completion_status,
            'order_status': self.order_status,
            'difference': self.difference,
            'disparity': self.disparity,
            'item_count': self.item_count,
            'total_quantity_ordered': self.total_quantity_ordered,
            'total_quantity_received': self.total_quantity_received,
            'total_rex_cost': self.total_rex_cost,
            'total_neto_cost': self.total_neto_cost,
            # Notes fields
            'latest_po_note': self.latest_po_note,
            'latest_po_note_user': self.latest_po_note_user,
            'latest_po_note_date': self.latest_po_note_date.isoformat() if self.latest_po_note_date else None
        }

# Cached Purchase Order Items model
class CachedPurchaseOrderItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    po_id = db.Column(db.String(50), nullable=False)
    po_item_id = db.Column(db.String(50))
    sku = db.Column(db.String(100))
    supplier_sku = db.Column(db.String(100))
    manufacturer_sku = db.Column(db.String(100))
    short_description = db.Column(db.String(500))
    neto_qty_ordered = db.Column(db.Integer)
    rex_qty_ordered = db.Column(db.Integer)
    rex_qty_received = db.Column(db.Integer)
    neto_qty_available = db.Column(db.String(50))  # This is a string field in BigQuery
    neto_cost_price = db.Column(db.Float)
    rex_supplier_buy_ex = db.Column(db.Float)
    difference = db.Column(db.Float)
    disparity = db.Column(db.Boolean)
    order_id = db.Column(db.String(50))
    created_on = db.Column(db.DateTime)
    modified_on = db.Column(db.DateTime)
    # Notes columns
    latest_item_note = db.Column(db.Text)
    latest_item_note_user = db.Column(db.String(100))
    latest_item_note_date = db.Column(db.DateTime)
    cached_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def to_dict(self):
        return {
            'po_id': self.po_id,
            'po_item_id': self.po_item_id,
            'sku': self.sku,
            'supplier_sku': self.supplier_sku,
            'manufacturer_sku': self.manufacturer_sku,
            'short_description': self.short_description,
            'neto_qty_ordered': self.neto_qty_ordered,
            'rex_qty_ordered': self.rex_qty_ordered,
            'rex_qty_received': self.rex_qty_received,
            'neto_qty_available': self.neto_qty_available,
            'neto_cost_price': self.neto_cost_price,
            'rex_supplier_buy_ex': self.rex_supplier_buy_ex,
            'difference': self.difference,
            'disparity': self.disparity,
            'OrderID': self.order_id,
            'created_on': self.created_on.isoformat() if self.created_on else None,
            'modified_on': self.modified_on.isoformat() if self.modified_on else None,
            # Notes fields
            'latest_item_note': self.latest_item_note,
            'latest_item_note_user': self.latest_item_note_user,
            'latest_item_note_date': self.latest_item_note_date.isoformat() if self.latest_item_note_date else None
        }

class CachedPurchaseOrderComparison(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    po_id = db.Column(db.String(50), nullable=False)
    modified_on = db.Column(db.DateTime)
    sku = db.Column(db.String(100))
    name = db.Column(db.String(500))
    change_log = db.Column(db.String(100))
    rex_available_qty = db.Column(db.Float)  # REX available quantity
    neto_qty_available = db.Column(db.Float)  # Changed from Integer to Float
    original_rex_qty_ordered = db.Column(db.Float)  # Changed from Integer to Float
    neto_qty_shipped = db.Column(db.Float)  # Changed from Integer to Float
    final_rex_qty_ordered = db.Column(db.Float)  # Changed from Integer to Float
    rex_qty_received = db.Column(db.Float)  # Changed from Integer to Float
    order_id = db.Column(db.String(50))
    po_item_id = db.Column(db.String(50))  # Added po_item_id field
    latest_item_note = db.Column(db.Text)  # Added latest_item_note field
    latest_item_note_user = db.Column(db.String(100))  # Added latest_item_note_user field
    latest_item_note_date = db.Column(db.DateTime)  # Added latest_item_note_date field
    cached_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def to_dict(self):
        return {
            'po_id': self.po_id,
            'modified_on': self.modified_on.isoformat() if self.modified_on else None,
            'sku': self.sku,
            'name': self.name,
            'change_log': self.change_log,
            'rex_available_qty': self.rex_available_qty,
            'neto_qty_available': self.neto_qty_available,
            'original_rex_qty_ordered': self.original_rex_qty_ordered,
            'neto_qty_shipped': self.neto_qty_shipped,
            'final_rex_qty_ordered': self.final_rex_qty_ordered,
            'rex_qty_received': self.rex_qty_received,
            'OrderID': self.order_id,
            'po_item_id': self.po_item_id,
            'latest_item_note': self.latest_item_note,
            'latest_item_note_user': self.latest_item_note_user,
            'latest_item_note_date': self.latest_item_note_date.isoformat() if self.latest_item_note_date else None
        }

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Caching functions
def safe_parse_date(date_str):
    """Safely parse date string, return None if invalid"""
    if not date_str:
        return None
    
    # Handle None or empty values
    if date_str is None or date_str == '' or date_str == 'None':
        return None
    
    try:
        # Handle different date formats
        if isinstance(date_str, str):
            # Skip obviously invalid dates
            if date_str.lower() in ['null', 'none', 'invalid date', '', '0000-00-00', '0000-00-00 00:00:00']:
                return None
            
            # Handle BigQuery null dates
            if date_str.startswith('0000-00-00'):
                return None
            
            # Remove 'Z' and replace with timezone info
            if date_str.endswith('Z'):
                date_str = date_str.replace('Z', '+00:00')
            
            # Try to parse the date
            parsed_date = datetime.fromisoformat(date_str)
            
            # Check for obviously invalid dates (year 0, etc.)
            if parsed_date.year < 1900 or parsed_date.year > 2100:
                return None
                
            return parsed_date
        return None
    except (ValueError, TypeError):
        # Silently return None for invalid dates - no need to log every null date
        return None

def update_cache_with_latest_note(po_item_id, po_id):
    """Update the cache with the latest note for a specific item"""
    try:
        # Get the latest note for this item from BigQuery
        latest_note_data, error = purchase_orders_service.get_item_notes(po_item_id)
        
        if not error and latest_note_data and len(latest_note_data) > 0:
            # Get the latest note (first in the list, as they're sorted by date desc)
            latest_note = latest_note_data[0]
            note_text = latest_note.get('comment', '')
            note_user = latest_note.get('username', 'admin')
            note_date = latest_note.get('created_at')
        else:
            note_text = None
            note_user = None
            note_date = None
        
        # Update the comparison cache
        cached_comparison = CachedPurchaseOrderComparison.query.filter(
            CachedPurchaseOrderComparison.po_item_id == str(po_item_id)
        ).first()
        
        if cached_comparison:
            cached_comparison.latest_item_note = note_text
            cached_comparison.latest_item_note_user = note_user
            cached_comparison.latest_item_note_date = safe_parse_date(note_date) if note_date else None
            db.session.commit()
            print(f"Updated comparison cache for po_item_id {po_item_id} with latest note")
        
        # Update the items cache
        cached_item = CachedPurchaseOrderItem.query.filter(
            CachedPurchaseOrderItem.po_item_id == str(po_item_id)
        ).first()
        
        if cached_item:
            cached_item.latest_item_note = note_text
            cached_item.latest_item_note_user = note_user
            cached_item.latest_item_note_date = safe_parse_date(note_date) if note_date else None
            db.session.commit()
            print(f"Updated items cache for po_item_id {po_item_id} with latest note")
        
        return True
            
    except Exception as e:
        print(f"Error updating cache for po_item_id {po_item_id}: {str(e)}")
        return False

def refresh_po_cache(po_id):
    """Refresh cache for a specific PO to include latest notes"""
    try:
        # For notes functionality, we don't need to refresh the summary cache
        # The comparison table will be updated in real-time via JavaScript
        print(f"Cache refresh not needed for PO {po_id} - notes update handled in real-time")
        return True
            
    except Exception as e:
        print(f"Error refreshing cache for PO {po_id}: {str(e)}")
        return False

def cache_purchase_order_data():
    """Fetch all data from BigQuery and cache it locally"""
    with app.app_context():
        try:
            global sync_state
            
            # Check if already running
            if sync_state['is_running']:
                print("Sync already in progress, skipping...")
                return False, "Sync already in progress"
            
            # Mark as running
            sync_state['is_running'] = True
            sync_state['should_stop'] = False
            
            print("Starting to cache all purchase order data...")
            
            # Clear existing cached data
            print("Clearing existing cache...")
            CachedPurchaseOrderSummary.query.delete()
            CachedPurchaseOrderItem.query.delete()
            CachedPurchaseOrderComparison.query.delete()
            db.session.commit()
            print("Cache cleared successfully")
            
            # Check if we should stop
            if sync_state['should_stop']:
                print("Sync cancelled by user")
                sync_state['is_running'] = False
                return False, "Sync cancelled"
            
            # Get actual counts from BigQuery first
            print("Getting actual data counts from BigQuery...")
            summary_count, error = purchase_orders_service.get_summary_count()
            if error:
                print(f"Error getting summary count: {error}")
                sync_state['is_running'] = False
                return False, error
            
            items_count, error = purchase_orders_service.get_items_count()
            if error:
                print(f"Error getting items count: {error}")
                return False, error
            
            comparison_count, error = purchase_orders_service.get_comparison_count()
            if error:
                print(f"Error getting comparison count: {error}")
                return False, error
            
            print(f"Actual counts - Summary: {summary_count}, Items: {items_count}, Comparison: {comparison_count}")
            
            # Store the actual totals for progress tracking
            global actual_totals
            actual_totals['summary_total'] = summary_count
            actual_totals['items_total'] = items_count
            actual_totals['comparison_total'] = comparison_count
            
            # Fetch summary data from BigQuery (no limit to get all data)
            summary_data, error = purchase_orders_service.get_purchase_order_summary(limit=None, offset=0)
            if error:
                print(f"Error fetching summary data: {error}")
                return False, error
            
            print(f"Fetched {len(summary_data)} summary records from BigQuery")
        
            # Cache summary data in batches
            print("Caching summary data...")
            summary_cached = 0
            batch_size = 100
            for i in range(0, len(summary_data), batch_size):
                # Check if we should stop
                if sync_state['should_stop']:
                    print("Sync cancelled during summary caching")
                    sync_state['is_running'] = False
                    return False, "Sync cancelled"
                
                batch = summary_data[i:i + batch_size]
                for row in batch:
                    try:
                        cached_row = CachedPurchaseOrderSummary(
                            po_id=row.get('po_id'),
                            po_status=row.get('po_status'),
                            rex_po_created_by=row.get('rex_po_created_by'),
                            requested_date=safe_parse_date(row.get('requested_date')),
                            order_id=row.get('OrderID'),
                            order_link=row.get('order_link'),
                            entered_date=safe_parse_date(row.get('entered_date')),
                            received_date=safe_parse_date(row.get('received_date')),
                            neto_order_created_by=row.get('neto_order_created_by'),
                            completed_date=safe_parse_date(row.get('completed_date')),
                            completion_status=row.get('completion_status'),
                            order_status=row.get('order_status'),
                            difference=convert_decimal_to_float(row.get('difference')),
                            disparity=row.get('disparity'),
                            item_count=row.get('item_count'),
                            total_quantity_ordered=convert_decimal_to_float(row.get('total_quantity_ordered')),
                            total_quantity_received=convert_decimal_to_float(row.get('total_quantity_received')),
                            total_rex_cost=convert_decimal_to_float(row.get('total_rex_cost')),
                            total_neto_cost=convert_decimal_to_float(row.get('total_neto_cost')),
                            # Notes fields
                            latest_po_note=row.get('latest_po_note'),
                            latest_po_note_user=row.get('latest_po_note_user'),
                            latest_po_note_date=safe_parse_date(row.get('latest_po_note_date'))
                        )
                        db.session.add(cached_row)
                        summary_cached += 1
                    except Exception as e:
                        print(f"Error caching summary row: {str(e)}")
                        continue
                
                # Commit batch
                db.session.commit()
                print(f"Cached {summary_cached}/{len(summary_data)} summary records...")
            
            print(f"Successfully cached {summary_cached} summary records")
        
            # Fetch all items data from BigQuery
            print("Fetching all items data...")
            items_data, error = purchase_orders_service.get_all_purchase_order_items()
            if error:
                print(f"Error fetching items data: {error}")
                return False, f"Summary cached but items failed: {error}"
            
            print(f"Fetched {len(items_data)} items records from BigQuery")
            
            # Cache items data in batches
            print("Caching items data...")
            items_cached = 0
            batch_size = 100
            for i in range(0, len(items_data), batch_size):
                # Check if we should stop
                if sync_state['should_stop']:
                    print("Sync cancelled during items caching")
                    sync_state['is_running'] = False
                    return False, "Sync cancelled"
                
                batch = items_data[i:i + batch_size]
                for item in batch:
                    try:
                        cached_item = CachedPurchaseOrderItem(
                            po_id=item.get('po_id'),
                            po_item_id=item.get('po_item_id'),
                            sku=item.get('sku'),
                            supplier_sku=item.get('supplier_sku'),
                            manufacturer_sku=item.get('manufacturer_sku'),
                            short_description=item.get('short_description'),
                            neto_qty_ordered=convert_decimal_to_float(item.get('neto_qty_ordered')),
                            rex_qty_ordered=convert_decimal_to_float(item.get('rex_qty_ordered')),
                            rex_qty_received=convert_decimal_to_float(item.get('rex_qty_received')),
                            neto_qty_available=item.get('neto_qty_available'),
                            neto_cost_price=convert_decimal_to_float(item.get('neto_cost_price')),
                            rex_supplier_buy_ex=convert_decimal_to_float(item.get('rex_supplier_buy_ex')),
                            difference=convert_decimal_to_float(item.get('difference')),
                            disparity=item.get('disparity'),
                            order_id=item.get('OrderID'),
                            created_on=safe_parse_date(item.get('created_on')),
                            modified_on=safe_parse_date(item.get('modified_on')),
                            # Notes fields
                            latest_item_note=item.get('latest_item_note'),
                            latest_item_note_user=item.get('latest_item_note_user'),
                            latest_item_note_date=safe_parse_date(item.get('latest_item_note_date')),
                            cached_at=datetime.utcnow()
                        )
                        db.session.add(cached_item)
                        items_cached += 1
                    except Exception as e:
                        print(f"Error caching item: {str(e)}")
                        continue
                
                # Commit batch
                db.session.commit()
                print(f"Cached {items_cached}/{len(items_data)} items records...")
            
            print(f"Successfully cached {items_cached} items records")
        
            # Fetch all comparison data from BigQuery
            print("Fetching all comparison data...")
            comparison_data, error = purchase_orders_service.get_all_purchase_order_comparison()
            if error:
                print(f"Error fetching comparison data: {error}")
                return False, f"Summary and items cached but comparison failed: {error}"
            
            print(f"Fetched {len(comparison_data)} comparison records from BigQuery")
            
            # Cache comparison data in batches
            print("Caching comparison data...")
            comparison_cached = 0
            batch_size = 100
            for i in range(0, len(comparison_data), batch_size):
                # Check if we should stop
                if sync_state['should_stop']:
                    print("Sync cancelled during comparison caching")
                    sync_state['is_running'] = False
                    return False, "Sync cancelled"
                
                batch = comparison_data[i:i + batch_size]
                for comp in batch:
                    try:
                        cached_comp = CachedPurchaseOrderComparison(
                            po_id=convert_decimal_to_float(comp.get('po_id')),
                            modified_on=safe_parse_date(comp.get('modified_on')),
                            sku=comp.get('sku'),
                            name=comp.get('name'),
                            change_log=comp.get('change_log'),
                            rex_available_qty=convert_decimal_to_float(comp.get('rex_available_qty')),
                            neto_qty_available=convert_decimal_to_float(comp.get('neto_qty_available')),
                            original_rex_qty_ordered=convert_decimal_to_float(comp.get('original_rex_qty_ordered')),
                            neto_qty_shipped=convert_decimal_to_float(comp.get('neto_qty_shipped')),
                            final_rex_qty_ordered=convert_decimal_to_float(comp.get('final_rex_qty_ordered')),
                            rex_qty_received=convert_decimal_to_float(comp.get('rex_qty_received')),
                            order_id=comp.get('OrderID'),
                            cached_at=datetime.utcnow()
                        )
                        db.session.add(cached_comp)
                        comparison_cached += 1
                    except Exception as e:
                        print(f"Error caching comparison: {str(e)}")
                        continue
                
                # Commit batch
                db.session.commit()
                print(f"Cached {comparison_cached}/{len(comparison_data)} comparison records...")
            
            print(f"Successfully cached {comparison_cached} comparison records")
            
            # Mark sync as complete
            sync_state['is_running'] = False
            sync_state['should_stop'] = False
            
            return True, f"Cached {summary_cached} summary, {items_cached} items, and {comparison_cached} comparison records"
            
        except Exception as e:
            print(f"Error caching data: {str(e)}")
            db.session.rollback()
            sync_state['is_running'] = False
            sync_state['should_stop'] = False
            return False, str(e)

def get_cached_summary_data(search_term=None):
    """Get summary data from cache with optional search"""
    query = CachedPurchaseOrderSummary.query
    
    if search_term:
        # Search by both PO ID and Order ID
        query = query.filter(
            db.or_(
                CachedPurchaseOrderSummary.po_id.like(f'%{search_term}%'),
                CachedPurchaseOrderSummary.order_id.like(f'%{search_term}%')
            )
        )
    
    # Order by entered_date desc (most recent first)
    query = query.order_by(CachedPurchaseOrderSummary.entered_date.desc())
    
    cached_records = query.limit(200).all()
    return [record.to_dict() for record in cached_records]

def get_cached_items_data(po_id=None, order_id=None):
    """Get items data from cache for a specific PO or Order ID"""
    query = CachedPurchaseOrderItem.query
    
    if po_id:
        query = query.filter(CachedPurchaseOrderItem.po_id == po_id)
    elif order_id:
        query = query.filter(CachedPurchaseOrderItem.order_id == order_id)
    
    query = query.order_by(CachedPurchaseOrderItem.po_item_id)
    
    cached_records = query.all()
    return [record.to_dict() for record in cached_records]

def convert_decimal_to_float(value):
    """Convert decimal.Decimal to float for SQLite compatibility"""
    if value is None:
        return None
    if hasattr(value, '__class__') and 'Decimal' in str(value.__class__):
        return float(value)
    return value

def cache_items_data(po_id, order_id, items_data):
    """Cache items data for a specific PO or Order ID"""
    try:
        # Clear existing items for this PO/Order
        if po_id:
            CachedPurchaseOrderItem.query.filter(CachedPurchaseOrderItem.po_id == po_id).delete()
        if order_id:
            CachedPurchaseOrderItem.query.filter(CachedPurchaseOrderItem.order_id == order_id).delete()
        
        # Cache new items
        for item in items_data:
            cached_item = CachedPurchaseOrderItem(
                po_id=item.get('po_id'),
                po_item_id=item.get('po_item_id'),
                sku=item.get('sku'),
                supplier_sku=item.get('supplier_sku'),
                manufacturer_sku=item.get('manufacturer_sku'),
                short_description=item.get('short_description'),
                neto_qty_ordered=convert_decimal_to_float(item.get('neto_qty_ordered')),
                rex_qty_ordered=convert_decimal_to_float(item.get('rex_qty_ordered')),
                rex_qty_received=convert_decimal_to_float(item.get('rex_qty_received')),
                neto_qty_available=item.get('neto_qty_available'),  # This field is a string, no conversion needed
                neto_cost_price=convert_decimal_to_float(item.get('neto_cost_price')),
                rex_supplier_buy_ex=convert_decimal_to_float(item.get('rex_supplier_buy_ex')),
                difference=convert_decimal_to_float(item.get('difference')),
                disparity=item.get('disparity'),
                order_id=item.get('OrderID'),
                created_on=safe_parse_date(item.get('created_on')),
                modified_on=safe_parse_date(item.get('modified_on')),
                # Notes fields
                latest_item_note=item.get('latest_item_note'),
                latest_item_note_user=item.get('latest_item_note_user'),
                latest_item_note_date=safe_parse_date(item.get('latest_item_note_date'))
            )
            db.session.add(cached_item)
        
        db.session.commit()
        print(f"Cached {len(items_data)} items for PO {po_id}")
        return True
    except Exception as e:
        print(f"Error caching items data: {str(e)}")
        db.session.rollback()
        return False

def get_cached_comparison_data(po_id=None, order_id=None):
    """Get comparison data from cache for a specific PO or Order ID"""
    query = CachedPurchaseOrderComparison.query
    
    if po_id:
        query = query.filter(CachedPurchaseOrderComparison.po_id == po_id)
    elif order_id:
        query = query.filter(CachedPurchaseOrderComparison.order_id == order_id)
    
    query = query.order_by(CachedPurchaseOrderComparison.modified_on.desc())
    
    cached_records = query.all()
    return [record.to_dict() for record in cached_records]

def cache_comparison_data(po_id, order_id, comparison_data):
    """Cache comparison data for a specific PO or Order ID"""
    try:
        # Clear existing comparison data for this PO/Order
        if po_id:
            CachedPurchaseOrderComparison.query.filter(CachedPurchaseOrderComparison.po_id == po_id).delete()
        if order_id:
            CachedPurchaseOrderComparison.query.filter(CachedPurchaseOrderComparison.order_id == order_id).delete()
        
        # Cache new comparison data
        for item in comparison_data:
            cached_item = CachedPurchaseOrderComparison(
                po_id=convert_decimal_to_float(item.get('po_id')),
                modified_on=safe_parse_date(item.get('modified_on')) if item.get('modified_on') else None,
                sku=item.get('sku'),
                name=item.get('name'),
                change_log=item.get('change_log'),
                rex_available_qty=convert_decimal_to_float(item.get('rex_available_qty')),
                neto_qty_available=convert_decimal_to_float(item.get('neto_qty_available')),
                original_rex_qty_ordered=convert_decimal_to_float(item.get('original_rex_qty_ordered')),
                neto_qty_shipped=convert_decimal_to_float(item.get('neto_qty_shipped')),
                final_rex_qty_ordered=convert_decimal_to_float(item.get('final_rex_qty_ordered')),
                rex_qty_received=convert_decimal_to_float(item.get('rex_qty_received')),
                order_id=item.get('OrderID'),
                po_item_id=item.get('po_item_id'),
                latest_item_note=item.get('latest_item_note'),
                latest_item_note_user=item.get('latest_item_note_user'),
                latest_item_note_date=safe_parse_date(item.get('latest_item_note_date')) if item.get('latest_item_note_date') else None
            )
            db.session.add(cached_item)
        
        db.session.commit()
        print(f"Cached {len(comparison_data)} comparison records for PO {po_id}")
        return True
    except Exception as e:
        print(f"Error caching comparison data: {str(e)}")
        db.session.rollback()
        return False

# Routes
@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        user = User.query.filter_by(username=username).first()
        
        if user and user.check_password(password):
            remember_me = request.form.get('remember_me') == 'on'
            login_user(user, remember=remember_me)
            
            # Log the login
            login_log = LoginLog(
                user_id=user.id,
                ip_address=request.remote_addr,
                user_agent=request.headers.get('User-Agent')
            )
            db.session.add(login_log)
            db.session.commit()
            
            # Redirect immediately to dashboard
            flash('Login successful! Refreshing data in background...', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid username or password', 'error')
    
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out', 'info')
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    # Check if we have cached data
    summary_count = CachedPurchaseOrderSummary.query.count()
    items_count = CachedPurchaseOrderItem.query.count()
    comparison_count = CachedPurchaseOrderComparison.query.count()
    
    # If no data is cached, show progress page
    if summary_count == 0 and items_count == 0 and comparison_count == 0:
        return render_template('dashboard_progress.html')
    
    # Otherwise show normal dashboard
    recent_logins = LoginLog.query.filter_by(user_id=current_user.id).order_by(LoginLog.login_time.desc()).limit(5).all()
    return render_template('dashboard.html', recent_logins=recent_logins)

@app.route('/dashboard-progress')
@login_required
def dashboard_progress():
    """Progress page for cache refresh"""
    return render_template('dashboard_progress.html')

@app.route('/admin')
@login_required
def admin():
    if not current_user.is_admin:
        flash('Access denied. Admin privileges required.', 'error')
        return redirect(url_for('dashboard'))
    
    users = User.query.all()
    return render_template('admin.html', users=users)

@app.route('/admin/create_user', methods=['POST'])
@login_required
def create_user():
    if not current_user.is_admin:
        flash('Access denied. Admin privileges required.', 'error')
        return redirect(url_for('dashboard'))
    
    username = request.form['username']
    password = request.form['password']
    
    if User.query.filter_by(username=username).first():
        flash('Username already exists', 'error')
        return redirect(url_for('admin'))
    
    user = User(username=username, is_admin=False)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    
    flash(f'User {username} created successfully', 'success')
    return redirect(url_for('admin'))

@app.route('/admin/reset_password/<int:user_id>', methods=['POST'])
@login_required
def reset_password(user_id):
    if not current_user.is_admin:
        flash('Access denied. Admin privileges required.', 'error')
        return redirect(url_for('dashboard'))
    
    user = User.query.get_or_404(user_id)
    new_password = request.form['new_password']
    
    user.set_password(new_password)
    db.session.commit()
    
    flash(f'Password reset for {user.username}', 'success')
    return redirect(url_for('admin'))

# BigQuery Routes (now handled by sidebar navigation)

@app.route('/api/bigquery/test')
@login_required
def test_bigquery_connection():
    """Test BigQuery connection"""
    success, message = purchase_orders_service.test_connection()
    return jsonify({'success': success, 'message': message})

@app.route('/api/bigquery/schema')
@login_required
def get_bigquery_schema():
    """Get BigQuery table schema"""
    schema, error = bigquery_service.get_table_schema()
    if error:
        return jsonify({'success': False, 'error': error})
    return jsonify({'success': True, 'schema': schema})

@app.route('/api/bigquery/summary')
@login_required
def get_bigquery_summary():
    """Get cached summary data with search, fallback to BigQuery if no cache"""
    search_term = request.args.get('search', None)
    sku_search = request.args.get('sku_search', None)
    
    # If SKU search is requested, query BigQuery directly
    if sku_search:
        try:
            print(f"SKU search requested: {sku_search}")
            bigquery_data, error = purchase_orders_service.get_purchase_order_summary_by_sku(sku_search)
            if not error and bigquery_data:
                return jsonify({
                    'success': True, 
                    'data': bigquery_data, 
                    'count': len(bigquery_data),
                    'total_count': len(bigquery_data),
                    'sku_search': sku_search,
                    'timestamp': datetime.utcnow().isoformat()
                })
            else:
                print(f"SKU search failed: {error}")
                return jsonify({
                    'success': False, 
                    'data': [], 
                    'message': f"SKU search failed: {error}",
                    'timestamp': datetime.utcnow().isoformat()
                })
        except Exception as e:
            print(f"SKU search error: {str(e)}")
            return jsonify({
                'success': False, 
                'data': [], 
                'message': f"SKU search error: {str(e)}",
                'timestamp': datetime.utcnow().isoformat()
            })
    
    # Regular search logic
    data = get_cached_summary_data(search_term)
    
    # If no cached data, try to fetch from BigQuery as fallback
    if not data:
        try:
            print("No cached data found, fetching from BigQuery as fallback")
            bigquery_data, error = purchase_orders_service.get_purchase_order_summary(limit=None, offset=0, search_term=search_term)
            if not error and bigquery_data:
                data = bigquery_data
            else:
                print(f"BigQuery fallback failed: {error}")
        except Exception as e:
            print(f"BigQuery fallback error: {str(e)}")
    
    return jsonify({
        'success': True, 
        'data': data, 
        'count': len(data),
        'total_count': len(data),
        'search_term': search_term,
        'timestamp': datetime.utcnow().isoformat()
    })

@app.route('/api/bigquery/items')
@login_required
def get_bigquery_items():
    """Get cached items data for a specific PO or Order ID, cache from BigQuery if not cached"""
    import time
    start_time = time.time()
    
    po_id = request.args.get('po_id', None)
    order_id = request.args.get('order_id', None)
    sku_search = request.args.get('sku_search', None)
    
    # If SKU search is requested, query BigQuery directly
    if sku_search:
        try:
            print(f"SKU search for items requested: {sku_search}")
            bigquery_data, error = purchase_orders_service.get_all_purchase_order_items_by_sku(sku_search)
            if not error and bigquery_data:
                return jsonify({
                    'success': True, 
                    'data': bigquery_data, 
                    'count': len(bigquery_data),
                    'sku_search': sku_search,
                    'timestamp': datetime.utcnow().isoformat()
                })
            else:
                print(f"SKU search for items failed: {error}")
                return jsonify({
                    'success': False, 
                    'data': [], 
                    'message': f"SKU search for items failed: {error}",
                    'timestamp': datetime.utcnow().isoformat()
                })
        except Exception as e:
            print(f"SKU search for items error: {str(e)}")
            return jsonify({
                'success': False, 
                'data': [], 
                'message': f"SKU search for items error: {str(e)}",
                'timestamp': datetime.utcnow().isoformat()
            })
    
    # Regular items logic
    data = get_cached_items_data(po_id, order_id)
    from_cache = True
    
    # If no cached data, fetch from BigQuery and cache it
    if not data:
        from_cache = False
        try:
            print(f"No cached items found for PO {po_id}, fetching from BigQuery and caching")
            bigquery_data, error = purchase_orders_service.get_purchase_order_items(po_id, order_id, limit=None, offset=0)
            if not error and bigquery_data:
                # Cache the items data
                cache_items_data(po_id, order_id, bigquery_data)
                data = bigquery_data
            else:
                print(f"BigQuery items fetch failed: {error}")
        except Exception as e:
            print(f"BigQuery items fetch error: {str(e)}")
    
    # Calculate load time
    load_time = int((time.time() - start_time) * 1000)  # Convert to milliseconds
    
    return jsonify({
        'success': True, 
        'data': data, 
        'count': len(data),
        'total_count': len(data),  # Total items count
        'po_id': po_id,
        'order_id': order_id,
        'from_cache': from_cache,
        'load_time': load_time,
        'timestamp': datetime.utcnow().isoformat()
    })

def enrich_comparison_data_with_notes(data):
    """Enrich comparison data with latest notes from BigQuery"""
    try:
        enriched_data = []
        for item in data:
            po_item_id = item.get('po_item_id')
            if po_item_id:
                # Get latest note for this item
                notes, error = purchase_orders_service.get_item_notes(po_item_id)
                if not error and notes and len(notes) > 0:
                    latest_note = notes[0]  # Notes are sorted by date desc
                    item['latest_item_note'] = latest_note.get('comment')
                    item['latest_item_note_user'] = latest_note.get('username')
                    item['latest_item_note_date'] = latest_note.get('created_at')
                else:
                    item['latest_item_note'] = None
                    item['latest_item_note_user'] = None
                    item['latest_item_note_date'] = None
            else:
                item['latest_item_note'] = None
                item['latest_item_note_user'] = None
                item['latest_item_note_date'] = None
            
            enriched_data.append(item)
        
        return enriched_data
    except Exception as e:
        print(f"Error enriching comparison data with notes: {str(e)}")
        return data  # Return original data if enrichment fails

@app.route('/api/bigquery/comparison')
@login_required
def get_bigquery_comparison():
    """Get cached comparison data for a specific PO or Order ID, cache from BigQuery if not cached"""
    import time
    start_time = time.time()
    
    po_id = request.args.get('po_id', None)
    order_id = request.args.get('order_id', None)
    sku_search = request.args.get('sku_search', None)
    
    # If SKU search is requested, query BigQuery directly
    if sku_search:
        try:
            print(f"SKU search for comparison requested: {sku_search}")
            bigquery_data, error = purchase_orders_service.get_all_purchase_order_comparison_by_sku(sku_search)
            if not error and bigquery_data:
                load_time = int((time.time() - start_time) * 1000)
                return jsonify({
                    'success': True, 
                    'data': bigquery_data, 
                    'count': len(bigquery_data),
                    'sku_search': sku_search,
                    'from_cache': False,  # SKU search always goes to BigQuery
                    'load_time': load_time,
                    'timestamp': datetime.utcnow().isoformat()
                })
            else:
                print(f"SKU search for comparison failed: {error}")
                return jsonify({
                    'success': False, 
                    'data': [], 
                    'message': f"SKU search for comparison failed: {error}",
                    'timestamp': datetime.utcnow().isoformat()
                })
        except Exception as e:
            print(f"SKU search for comparison error: {str(e)}")
            return jsonify({
                'success': False, 
                'data': [], 
                'message': f"SKU search for comparison error: {str(e)}",
                'timestamp': datetime.utcnow().isoformat()
            })
    
    # Regular comparison logic
    data = get_cached_comparison_data(po_id, order_id)
    from_cache = True
    
    # Debug: Check what data is being returned
    if data and len(data) > 0:
        print(f"API: Serving cached comparison data for PO {po_id}, first record po_item_id: {data[0].get('po_item_id', 'MISSING')}")
    
    # Note enrichment is now handled by the cache update mechanism
    # The cache should already contain the latest notes when data is served
    
    # If no cached data OR cached data doesn't have po_item_id, fetch from BigQuery and cache it
    if not data or (data and len(data) > 0 and data[0].get('po_item_id') is None):
        from_cache = False
        try:
            print(f"No cached comparison data found for PO {po_id}, fetching from BigQuery and caching")
            bigquery_data, error = purchase_orders_service.get_purchase_order_comparison(po_id, order_id, limit=None, offset=0)
            if not error and bigquery_data:
                # Cache the comparison data
                cache_comparison_data(po_id, order_id, bigquery_data)
                data = bigquery_data
            else:
                print(f"BigQuery comparison fetch failed: {error}")
        except Exception as e:
            print(f"BigQuery comparison fetch error: {str(e)}")
    
    # Calculate load time
    load_time = int((time.time() - start_time) * 1000)  # Convert to milliseconds
    
    return jsonify({
        'success': True, 
        'data': data, 
        'count': len(data),
        'total_count': len(data),  # Total comparison count
        'po_id': po_id,
        'order_id': order_id,
        'from_cache': from_cache,
        'load_time': load_time,
        'timestamp': datetime.utcnow().isoformat()
    })

@app.route('/api/bigquery/refresh', methods=['POST'])
@login_required
def refresh_bigquery_data():
    """Refresh cached data from BigQuery"""
    try:
        success, message = cache_purchase_order_data()
        if success:
            return jsonify({'success': True, 'message': message})
        else:
            return jsonify({'success': False, 'error': message})
    except Exception as e:
        print(f"Refresh error: {str(e)}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/bigquery/debug', methods=['GET'])
@login_required
def debug_bigquery_data():
    """Debug endpoint to check BigQuery data format"""
    try:
        # Get a small sample from BigQuery
        data, error = purchase_orders_service.get_purchase_order_summary(limit=1, offset=0)
        if error:
            return jsonify({'success': False, 'error': error})
        
        if data and len(data) > 0:
            sample_row = data[0]
            return jsonify({
                'success': True, 
                'sample_row': sample_row,
                'date_fields': {
                    'requested_date': sample_row.get('requested_date'),
                    'entered_date': sample_row.get('entered_date'),
                    'received_date': sample_row.get('received_date'),
                    'completed_date': sample_row.get('completed_date')
                }
            })
        else:
            return jsonify({'success': False, 'error': 'No data returned from BigQuery'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/bigquery/cache-status', methods=['GET'])
@login_required
def cache_status():
    """Check cache status"""
    try:
        summary_count = CachedPurchaseOrderSummary.query.count()
        items_count = CachedPurchaseOrderItem.query.count()
        comparison_count = CachedPurchaseOrderComparison.query.count()
        
        # Get the most recent cached_at timestamp
        last_cached = None
        latest_record = CachedPurchaseOrderSummary.query.order_by(CachedPurchaseOrderSummary.cached_at.desc()).first()
        if latest_record and latest_record.cached_at:
            # Add 'Z' to indicate UTC time so JavaScript parses it correctly
            last_cached = latest_record.cached_at.isoformat() + 'Z'
        
        # Use actual totals from BigQuery count queries
        global actual_totals, sync_state
        totals = {
            'summary_total': actual_totals['summary_total'],
            'items_total': actual_totals['items_total'],
            'comparison_total': actual_totals['comparison_total']
        }
        
        return jsonify({
            'success': True,
            'summary_count': summary_count,
            'items_count': items_count,
            'comparison_count': comparison_count,
            'summary_total': totals['summary_total'],
            'items_total': totals['items_total'],
            'comparison_total': totals['comparison_total'],
            'has_cached_data': summary_count > 0,
            'last_cached': last_cached,
            'is_syncing': sync_state['is_running']
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/bigquery/clear-cache', methods=['POST'])
@login_required
def clear_cache():
    """Clear all cached data and stop any ongoing sync"""
    try:
        global sync_state
        
        # Stop any ongoing sync
        if sync_state['is_running']:
            print("Stopping ongoing sync due to cache clear request...")
            sync_state['should_stop'] = True
            
            # Wait a moment for sync to stop
            import time
            time.sleep(2)
        
        # Clear the cache
        CachedPurchaseOrderSummary.query.delete()
        CachedPurchaseOrderItem.query.delete()
        CachedPurchaseOrderComparison.query.delete()
        db.session.commit()
        
        # Reset sync state
        sync_state['is_running'] = False
        sync_state['should_stop'] = False
        
        print("Cache cleared successfully")
        
        return jsonify({
            'success': True,
            'message': 'Cache cleared successfully. Any ongoing sync has been stopped.'
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/bigquery/start-refresh', methods=['POST'])
@login_required
def start_background_refresh():
    """Start background cache refresh"""
    try:
        # Start the cache refresh in a separate thread
        import threading
        thread = threading.Thread(target=cache_purchase_order_data)
        thread.daemon = True
        thread.start()
        
        return jsonify({
            'success': True,
            'message': 'Background refresh started'
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Error starting refresh: {str(e)}'
        }), 500

@app.route('/api/notes/save', methods=['POST'])
@login_required
def save_note():
    """Save a note for a specific item"""
    try:
        data = request.get_json()
        po_item_id = data.get('po_item_id')
        po_id = data.get('po_id')
        sku = data.get('sku')
        comment = data.get('comment')
        
        if not all([po_item_id, po_id, comment]):
            return jsonify({'success': False, 'error': 'Missing required fields'}), 400
        
        success, message = purchase_orders_service.save_item_note(
            po_item_id=po_item_id,
            po_id=po_id,
            sku=sku,
            comment=comment,
            username=current_user.username
        )
        
        if success:
            # Update the cache with the latest note for this specific item
            try:
                update_cache_with_latest_note(po_item_id, po_id)
            except Exception as e:
                print(f"Warning: Failed to update cache for po_item_id {po_item_id}: {str(e)}")
            
            return jsonify({'success': True, 'message': message})
        else:
            return jsonify({'success': False, 'error': message}), 500
            
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/notes/<po_item_id>', methods=['GET'])
@login_required
def get_notes(po_item_id):
    """Get all notes for a specific item"""
    try:
        notes, error = purchase_orders_service.get_item_notes(po_item_id)
        
        if error:
            return jsonify({'success': False, 'error': error}), 500
        
        return jsonify({'success': True, 'notes': notes})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/notes/<note_id>/delete', methods=['POST'])
@login_required
def delete_note(note_id):
    """Soft delete a note"""
    try:
        # Get note details before deletion to update cache
        data = request.get_json()
        po_item_id = data.get('po_item_id')
        po_id = data.get('po_id')
        
        if not po_item_id or not po_id:
            return jsonify({'success': False, 'error': 'Missing po_item_id or po_id'}), 400
        
        success, message = purchase_orders_service.delete_item_note(
            note_id=note_id,
            username=current_user.username
        )
        
        if success:
            # Update the cache after deletion to reflect the latest notes
            try:
                update_cache_with_latest_note(po_item_id, po_id)
                print(f"Cache updated after deleting note {note_id} for po_item_id {po_item_id}")
            except Exception as e:
                print(f"Warning: Failed to update cache after deletion for po_item_id {po_item_id}: {str(e)}")
            
            return jsonify({'success': True, 'message': message})
        else:
            return jsonify({'success': False, 'error': message}), 500
            
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/po-notes/save', methods=['POST'])
@login_required
def save_po_note():
    """Save a PO-level note"""
    try:
        data = request.get_json()
        po_id = data.get('po_id')
        order_id = data.get('order_id')
        comment = data.get('comment')
        
        if not all([po_id, comment]):
            return jsonify({'success': False, 'error': 'Missing required fields'}), 400
        
        success, message = purchase_orders_service.save_po_note(
            po_id=po_id,
            order_id=order_id,
            comment=comment,
            username=current_user.username
        )
        
        if success:
            # Refresh the cache for this specific PO to include the new note
            try:
                refresh_po_cache(po_id)
            except Exception as e:
                print(f"Warning: Failed to refresh cache for PO {po_id}: {str(e)}")
            
            return jsonify({'success': True, 'message': message})
        else:
            return jsonify({'success': False, 'error': message}), 500
            
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/po-notes/<po_id>', methods=['GET'])
@login_required
def get_po_notes(po_id):
    """Get all notes for a specific PO"""
    try:
        notes, error = purchase_orders_service.get_po_notes(po_id)
        
        if error:
            return jsonify({'success': False, 'error': error}), 500
        
        return jsonify({'success': True, 'notes': notes})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/po-item-notes/<po_id>', methods=['GET'])
@login_required
def get_all_item_notes_for_po(po_id):
    """Get all item notes for all items in a specific PO"""
    try:
        notes, error = purchase_orders_service.get_all_item_notes_for_po(po_id)
        
        if error:
            return jsonify({'success': False, 'error': error}), 500
        
        return jsonify({'success': True, 'notes': notes})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

def create_admin_user():
    """Create the default admin user if it doesn't exist"""
    admin_user = User.query.filter_by(username='admin').first()
    if not admin_user:
        admin_user = User(username='admin', is_admin=True)
        admin_user.set_password('1234')
        db.session.add(admin_user)
        db.session.commit()
        print("Admin user created: username='admin', password='1234'")

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        create_admin_user()
    
    app.run(debug=True, host='0.0.0.0', port=5001)
