from flask import Flask, jsonify, request
import time
from database import DatabaseManager
from vmc_commands import CommandBuilder

app = Flask(__name__)
db = DatabaseManager()

# ==============================================================================
#  CORE VENDING OPERATIONS
# ==============================================================================

@app.route('/api/buy', methods=['POST'])
def buy_product():
    """
    Standard Purchase (Command 0x03).
    Usage: POST /api/buy { "selection": 10 }
    """
    data = request.json
    selection = data.get('selection')
    
    if not selection:
        return jsonify({"error": "Missing selection ID"}), 400

    # 1. Generate HEX for "Select to Buy"
    hex_payload = CommandBuilder.dispense(int(selection))
    
    # 2. Add to Queue
    cmd_id = db.add_command(hex_payload)
    
    return jsonify({
        "status": "accepted",
        "command_id": cmd_id,
        "message": "Purchase request queued. Poll /api/command/<id> for result."
    }), 202

@app.route('/api/drive', methods=['POST'])
def drive_motor_direct():
    """
    Direct Motor Control (Command 0x06).
    Forces a motor to turn, bypassing some VMC logic.
    Usage: POST /api/drive { "selection": 10 }
    """
    data = request.json
    selection = data.get('selection')
    
    # Payload: 06 + DropSensor(1) + Elevator(1) + Selection(2) + Cart(1)
    # We construct this manually here or add helper in CommandBuilder
    # Simple hex construction for 0x06:
    # 06 01(Sensor On) 01(Elevator On) [Selection] 00(No Cart)
    sel_hex = f"{int(selection):04X}"
    hex_payload = f"060101{sel_hex}00" 
    
    cmd_id = db.add_command(hex_payload)
    
    return jsonify({
        "status": "accepted",
        "command_id": cmd_id,
        "type": "DIRECT_DRIVE"
    }), 202

# ==============================================================================
#  PAYMENT & TRANSACTION CONTROL
# ==============================================================================

@app.route('/api/deduct', methods=['POST'])
def deduct_money():
    """
    Deduct Balance (Command 0x64).
    Usage: POST /api/deduct { "amount": 500 } (5.00 Units)
    """
    data = request.json
    amount = data.get('amount')
    
    if amount is None or amount <= 0:
        return jsonify({"error": "Invalid amount"}), 400

    hex_payload = CommandBuilder.deduct_card(int(amount))
    cmd_id = db.add_command(hex_payload)
    
    return jsonify({"status": "processing", "command_id": cmd_id}), 202

@app.route('/api/cancel', methods=['POST'])
def cancel_transaction():
    """
    Cancel Transaction (Command 0x64 with Amount 0).
    Usage: POST /api/cancel
    """
    hex_payload = CommandBuilder.cancel_transaction()
    cmd_id = db.add_command(hex_payload)
    return jsonify({"status": "cancelling", "command_id": cmd_id}), 202

# ==============================================================================
#  DATA & SYNCHRONIZATION
# ==============================================================================

@app.route('/api/sync', methods=['POST'])
def force_sync():
    """
    Trigger VMC to report all products (Command 0x31).
    The Serial Controller will catch the 0x11 responses and populate the 'products' table.
    """
    hex_payload = CommandBuilder.sync_info()
    cmd_id = db.add_command(hex_payload)
    return jsonify({"status": "sync_started", "command_id": cmd_id}), 202

@app.route('/api/products', methods=['GET'])
def get_products():
    """
    Reads the 'products' table populated by the Serial Controller.
    Returns: JSON list of inventory/prices.
    """
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM products ORDER BY selection_id ASC")
    rows = cursor.fetchall()
    
    products = [dict(row) for row in rows]
    return jsonify({"count": len(products), "products": products})

@app.route('/api/status', methods=['GET'])
def get_machine_status():
    """Returns VMC status (Temp, Door, Balance) from DB."""
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM vmc_status")
    rows = cursor.fetchall()
    
    status = {row['key']: row['value'] for row in rows}
    return jsonify(status)

# ==============================================================================
#  COMMAND POLLING (The "Are we there yet?" endpoint)
# ==============================================================================

@app.route('/api/command/<int:cmd_id>', methods=['GET'])
def check_command_status(cmd_id):
    """
    Check the status of a specific command.
    """
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT status, response_payload, completion_details FROM command_queue WHERE id = ?", (cmd_id,))
    row = cursor.fetchone()
    
    if not row:
        return jsonify({"error": "Command not found"}), 404
    
    return jsonify({
        "id": cmd_id,
        "status": row['status'], # PENDING, SENDING, DISPENSING, COMPLETED, FAILED
        "details": row['completion_details'] # JSON string from Serial Controller
    })

# --- NEW SET & QUERY ENDPOINTS ---

@app.route('/api/products/price', methods=['POST'])
def set_product_price():
    """
    Sets the price for a selection.
    Payload: { "selection": 10, "price": 100 }
    """
    data = request.json
    sel = data.get('selection')
    price = data.get('price')
    
    if sel is None or price is None:
        return jsonify({"error": "Missing selection or price"}), 400
    
    cmd_id = db.add_command(CommandBuilder.set_price(int(sel), int(price)))
    return jsonify({"status": "queued", "command_id": cmd_id, "action": "SET_PRICE"}), 202

@app.route('/api/products/inventory', methods=['POST'])
def set_product_inventory():
    """
    Sets the inventory count.
    Payload: { "selection": 10, "inventory": 5 }
    """
    data = request.json
    sel = data.get('selection')
    inv = data.get('inventory')
    
    if sel is None or inv is None:
        return jsonify({"error": "Missing selection or inventory"}), 400
    
    cmd_id = db.add_command(CommandBuilder.set_inventory(int(sel), int(inv)))
    return jsonify({"status": "queued", "command_id": cmd_id, "action": "SET_INVENTORY"}), 202

@app.route('/api/config/selection/<int:selection_id>', methods=['GET'])
def query_selection_config(selection_id):
    """
    Triggers a live query (0x42) to get the config from the VMC.
    Returns the command ID to poll.
    """
    cmd_id = db.add_command(CommandBuilder.query_selection_config(selection_id))
    return jsonify({"status": "queued", "command_id": cmd_id, "action": "QUERY_CONFIG"}), 202

@app.route('/api/sales/daily', methods=['GET'])
def query_daily_sales():
    """
    Triggers a query for today's sales (0x43).
    """
    # Format YYYYMMDD
    today_str = time.strftime("%Y%m%d")
    cmd_id = db.add_command(CommandBuilder.query_daily_sales(today_str))
    return jsonify({"status": "queued", "command_id": cmd_id, "action": "QUERY_SALES", "date": today_str}), 202


# ==============================================================================
#  SERVER START
# ==============================================================================

if __name__ == '__main__':
    # Initialize DB if running standalone
    print("🚀 Middleware API Starting on Port 5000...")
    print("Ensure 'serial_controller.py' is running in a separate terminal!")
    app.run(host='0.0.0.0', port=5000, debug=True)
