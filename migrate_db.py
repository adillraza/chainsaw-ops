"""
Database migration script to add new columns to cached_purchase_order_summary table
This preserves the users table and all user data
"""
import sqlite3
import shutil
from datetime import datetime

# Backup the database first
backup_file = f'instance/users.db.backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}'
print(f"Creating backup: {backup_file}")
shutil.copy2('instance/users.db', backup_file)
print("Backup created successfully!")

# Connect to the database
conn = sqlite3.connect('instance/users.db')
cursor = conn.cursor()

try:
    # Check if columns already exist
    cursor.execute("PRAGMA table_info(cached_purchase_order_summary)")
    columns = [col[1] for col in cursor.fetchall()]
    
    if 'no_of_neto_orders' not in columns:
        print("Adding no_of_neto_orders column...")
        cursor.execute("""
            ALTER TABLE cached_purchase_order_summary 
            ADD COLUMN no_of_neto_orders INTEGER
        """)
        print("✓ Added no_of_neto_orders column")
    else:
        print("✓ no_of_neto_orders column already exists")
    
    if 'neto_order_ids' not in columns:
        print("Adding neto_order_ids column...")
        cursor.execute("""
            ALTER TABLE cached_purchase_order_summary 
            ADD COLUMN neto_order_ids TEXT
        """)
        print("✓ Added neto_order_ids column")
    else:
        print("✓ neto_order_ids column already exists")
    
    # Create indexes for the new columns
    print("Creating indexes...")
    try:
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_summary_no_of_neto_orders 
            ON cached_purchase_order_summary(no_of_neto_orders)
        """)
        print("✓ Created index on no_of_neto_orders")
    except Exception as e:
        print(f"Note: Index on no_of_neto_orders may already exist: {e}")
    
    # Commit the changes
    conn.commit()
    print("\n✅ Migration completed successfully!")
    print(f"Backup saved as: {backup_file}")
    print("\nYou can now restart the application.")
    
except Exception as e:
    conn.rollback()
    print(f"\n❌ Error during migration: {e}")
    print(f"Database has been rolled back. Backup is available at: {backup_file}")
    raise
finally:
    conn.close()

