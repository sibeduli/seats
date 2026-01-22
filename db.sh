#!/bin/bash

# Database management script for seats app
# Usage: ./db.sh [backup|restore|clean|list]

DB_NAME="seats"
DB_USER="postgres"
DB_HOST="localhost"
DB_PORT="5432"
BACKUP_DIR="./backups"

# Connection string for pg commands
PG_CONN="-U $DB_USER -h $DB_HOST -p $DB_PORT"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Create backup directory if not exists
mkdir -p "$BACKUP_DIR"

backup() {
    TIMESTAMP=$(date +%Y%m%d_%H%M%S)
    BACKUP_FILE="$BACKUP_DIR/${DB_NAME}_${TIMESTAMP}.sql"
    
    echo -e "${YELLOW}Creating backup...${NC}"
    pg_dump $PG_CONN "$DB_NAME" > "$BACKUP_FILE"
    
    if [ $? -eq 0 ]; then
        echo -e "${GREEN}Backup created: $BACKUP_FILE${NC}"
        ls -lh "$BACKUP_FILE"
    else
        echo -e "${RED}Backup failed!${NC}"
        exit 1
    fi
}

restore() {
    if [ -z "$1" ]; then
        echo -e "${YELLOW}Available backups:${NC}"
        ls -lt "$BACKUP_DIR"/*.sql 2>/dev/null || echo "No backups found"
        echo ""
        read -p "Enter backup filename to restore: " BACKUP_FILE
    else
        BACKUP_FILE="$1"
    fi
    
    # Add backup dir prefix only if it's just a filename (no path)
    if [[ ! "$BACKUP_FILE" == */* ]]; then
        BACKUP_FILE="$BACKUP_DIR/$BACKUP_FILE"
    fi
    
    if [ ! -f "$BACKUP_FILE" ]; then
        echo -e "${RED}Backup file not found: $BACKUP_FILE${NC}"
        exit 1
    fi
    
    echo -e "${YELLOW}WARNING: This will overwrite the current database!${NC}"
    read -p "Are you sure you want to restore from $BACKUP_FILE? (y/n): " confirm
    
    if [ "$confirm" = "y" ] || [ "$confirm" = "Y" ]; then
        echo -e "${YELLOW}Dropping existing tables...${NC}"
        psql $PG_CONN "$DB_NAME" -c "DROP TABLE IF EXISTS seat CASCADE; DROP TABLE IF EXISTS transaction CASCADE;" 2>/dev/null
        
        echo -e "${YELLOW}Restoring database...${NC}"
        psql $PG_CONN "$DB_NAME" < "$BACKUP_FILE"
        
        if [ $? -eq 0 ]; then
            echo -e "${GREEN}Database restored successfully!${NC}"
        else
            echo -e "${RED}Restore failed!${NC}"
            exit 1
        fi
    else
        echo "Restore cancelled."
    fi
}

clean() {
    echo -e "${RED}WARNING: This will DELETE ALL DATA in the database!${NC}"
    read -p "Are you sure you want to clean the database? (y/n): " confirm
    
    if [ "$confirm" = "y" ] || [ "$confirm" = "Y" ]; then
        read -p "Type 'DELETE' to confirm: " confirm2
        
        if [ "$confirm2" = "DELETE" ]; then
            echo -e "${YELLOW}Cleaning database...${NC}"
            psql $PG_CONN "$DB_NAME" -c "TRUNCATE TABLE seat, transaction RESTART IDENTITY CASCADE;"
            
            if [ $? -eq 0 ]; then
                echo -e "${GREEN}Database cleaned successfully!${NC}"
            else
                echo -e "${RED}Clean failed!${NC}"
                exit 1
            fi
        else
            echo "Clean cancelled."
        fi
    else
        echo "Clean cancelled."
    fi
}

list() {
    echo -e "${YELLOW}Available backups:${NC}"
    echo "----------------------------------------"
    ls -lth "$BACKUP_DIR"/*.sql 2>/dev/null || echo "No backups found"
    echo "----------------------------------------"
    
    # Show count
    COUNT=$(ls -1 "$BACKUP_DIR"/*.sql 2>/dev/null | wc -l)
    echo -e "Total: ${GREEN}$COUNT${NC} backup(s)"
}

stats() {
    echo -e "${YELLOW}Database Statistics${NC}"
    echo "========================================"
    
    psql $PG_CONN "$DB_NAME" -t -c "
    SELECT 
        'Total Bookings' as metric, COUNT(*) as value FROM transaction
    UNION ALL
    SELECT 'Total Seats', COUNT(*) FROM seat WHERE transaction_id IS NOT NULL
    UNION ALL
    SELECT 'Active', COUNT(*) FROM transaction WHERE status = 'active'
    UNION ALL
    SELECT 'Pending', COUNT(*) FROM transaction WHERE status = 'pending'
    UNION ALL
    SELECT 'Expired', COUNT(*) FROM transaction WHERE status = 'expired'
    UNION ALL
    SELECT 'Revoked', COUNT(*) FROM transaction WHERE status = 'revoked'
    " | while read line; do
        if [ -n "$line" ]; then
            metric=$(echo "$line" | cut -d'|' -f1 | xargs)
            value=$(echo "$line" | cut -d'|' -f2 | xargs)
            printf "%-20s: ${GREEN}%s${NC}\n" "$metric" "$value"
        fi
    done
    
    echo "========================================"
}

export_csv() {
    TIMESTAMP=$(date +%Y%m%d_%H%M%S)
    EXPORT_DIR="$BACKUP_DIR/export_${TIMESTAMP}"
    mkdir -p "$EXPORT_DIR"
    
    echo -e "${YELLOW}Exporting current database to CSV...${NC}"
    echo -e "Source: ${GREEN}$DB_NAME${NC} @ $DB_HOST:$DB_PORT"
    
    # Export transactions
    psql $PG_CONN "$DB_NAME" -c "\COPY (SELECT id, ticket_hash, name, phone, status, timestamp, booked_by_admin FROM transaction ORDER BY id) TO '$EXPORT_DIR/transactions.csv' WITH CSV HEADER"
    
    # Export seats
    psql $PG_CONN "$DB_NAME" -c "\COPY (SELECT id, region, seat_number, transaction_id FROM seat ORDER BY id) TO '$EXPORT_DIR/seats.csv' WITH CSV HEADER"
    
    # Export combined view (transactions with seats)
    psql $PG_CONN "$DB_NAME" -c "\COPY (SELECT t.id, t.ticket_hash, t.name, t.phone, t.status, t.timestamp, t.booked_by_admin, s.region || '-' || s.seat_number as seat FROM transaction t LEFT JOIN seat s ON s.transaction_id = t.id ORDER BY t.id, s.region, s.seat_number) TO '$EXPORT_DIR/bookings_full.csv' WITH CSV HEADER"
    
    if [ $? -eq 0 ]; then
        echo -e "${GREEN}Export completed!${NC}"
        echo "Files created in: $EXPORT_DIR"
        ls -lh "$EXPORT_DIR"
    else
        echo -e "${RED}Export failed!${NC}"
        exit 1
    fi
}

usage() {
    echo "Database Management Script"
    echo ""
    echo "Usage: ./db.sh [command]"
    echo ""
    echo "Commands:"
    echo "  backup    Create a new database backup (.sql)"
    echo "  restore   Restore database from a backup file"
    echo "  clean     Delete all data from database (with confirmation)"
    echo "  list      List all available backups"
    echo "  export    Export data to CSV files (Excel compatible)"
    echo "  stats     Show database statistics"
    echo ""
    echo "Examples:"
    echo "  ./db.sh backup"
    echo "  ./db.sh restore seats_20260123_120000.sql"
    echo "  ./db.sh clean"
    echo "  ./db.sh list"
}

# Main
case "$1" in
    backup)
        backup
        ;;
    restore)
        restore "$2"
        ;;
    clean)
        clean
        ;;
    list)
        list
        ;;
    export)
        export_csv
        ;;
    stats)
        stats
        ;;
    *)
        usage
        ;;
esac
