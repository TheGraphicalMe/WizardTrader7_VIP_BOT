import os
import json
from dotenv import load_dotenv
from sqlalchemy import create_engine, MetaData
from sqlalchemy.orm import sessionmaker

def backup_database():
    # Load environment variables
    load_dotenv()

    # Get database URL (use production Supabase connection string)
    DATABASE_URL = os.getenv("DATABASE_URL")

    if not DATABASE_URL:
        print("❌ Error: DATABASE_URL not found in .env file.")
        return

    # Check if DATABASE_URL is SQLite or Supabase PostgreSQL
    is_sqlite = DATABASE_URL.startswith("sqlite")
    print(f"Connecting to {'Local SQLite' if is_sqlite else 'Supabase PostgreSQL'} Database...")

    try:
        engine = create_engine(DATABASE_URL)
        metadata = MetaData()
        # Reflect all tables in the database
        metadata.reflect(bind=engine)
    except Exception as e:
        print(f"❌ Failed to connect to database: {e}")
        return

    # Create backup directory
    backup_dir = "db_backups"
    os.makedirs(backup_dir, exist_ok=True)

    for table_name, table in metadata.tables.items():
        print(f"Exporting table: '{table_name}'...")
        try:
            with engine.connect() as conn:
                result = conn.execute(table.select())
                # Read all rows into dicts
                rows = [dict(row._mapping) for row in result]
                
                # Convert datetime fields to ISO strings so JSON can write them
                for row in rows:
                    for key, val in row.items():
                        if hasattr(val, "isoformat"):
                            row[key] = val.isoformat()
                
                backup_file = os.path.join(backup_dir, f"{table_name}.json")
                with open(backup_file, "w", encoding="utf-8") as f:
                    json.dump(rows, f, indent=4, default=str)
                print(f"  └─ Saved {len(rows)} records to {backup_file}")
        except Exception as e:
            print(f"❌ Failed to export table '{table_name}': {e}")

    print(f"\n✅ Backup complete! JSON files saved in the '{backup_dir}/' folder.")

if __name__ == "__main__":
    backup_database()
