from flask import Flask, jsonify, request
import time
from database import DatabaseManager
from vmc_commands import CommandBuilder

app = Flask(__name__)
db = DatabaseManager()

def wait_for_command_result(cmd_id, timeout=15.0):
    """
    Blocks the HTTP request until the command is COMPLETED or FAILED.
    This creates a 'Synchronous' feel for the API user.
    """
    start_time = time.time()
    # Create new connection for this thread to avoid sharing issues
    conn = db.get_connection() 
    
    while (time.time() - start_time) < timeout:
        cursor = conn.cursor()
        cursor.execute("SELECT status, completion_details FROM command_queue WHERE id = ?", (cmd_id,))
        row = cursor.fetchone()
        
        if row:
            status = row['status']
            # We wait for a final state.
            # 'ACKED' is not enough (it just means VMC heard us).
            # We want 'COMPLETED' (VMC finished the job) or 'FAILED'.
            if status == 'COMPLETED':
                return "COMPLETED", row['completion_details']
            elif status == 'FAILED':
                return "FAILED", row['completion_details']
        
        time.sleep(0.1) # Poll DB every 100ms
        
    return "TIMEOUT", None

def execute_blocking_command(hex_payload, action_name):
    """
    Helper to Add Command -> Wait -> Return JSON
    """
    # 1. Add to Queue
    cmd_id = db.add_command(hex_payload)
    
    # 2. Block and Wait
    status, details = wait_for_command_result(cmd_id)
    
    # 3. Construct Response
    if status == "TIMEOUT":
        return jsonify({
            "status": "timeout", 
            "error": "VMC did not respond in time",
            "command_id": cmd_id,
            "action": action_name
        }), 504
    
    response_data = {
        "status": status, # COMPLETED / FAILED
        "command_id": cmd_id,
        "action": action_name,
        "result": details # This contains the JSON data from the VMC (e.g. Sales Data)
    }
    
    return jsonify(response_data), 200

# ==============================================================================
#  CORE VENDING OPERATIONS
# ==============================================================================

@app.route('/api/buy', methods=['POST'])
def buy_product():
    """
    Standard Purchase.
    Waits for motor to finish turning (Success/Fail).
    """
    data = request.json
    selection = data.get('selection')
    if not selection: return jsonify({"error": "Missing selection"}), 400
    
    payload = CommandBuilder.dispense(int(selection))
    return execute_blocking_command(payload, "DISPENSE")

@app.route('/api/drive', methods=['POST'])
def drive_motor_direct():
    """
    Direct Drive.
    Waits for motor execution result.
    """
    data = request.json
    selection = data.get('selection')
    sel_hex = f"{int(selection):04X}"
    payload = f"060101{sel_hex}00" 
    return execute_blocking_command(payload, "DIRECT_DRIVE")

# ==============================================================================
#  PAYMENT & TRANSACTION
# ==============================================================================

@app.route('/api/deduct', methods=['POST'])
def deduct_money():
    data = request.json
    amount = data.get('amount')
    if not amount: return jsonify({"error": "Missing amount"}), 400
    
    payload = CommandBuilder.deduct_card(int(amount))
    return execute_blocking_command(payload, "DEDUCT_MONEY")

@app.route('/api/cancel', methods=['POST'])
def cancel_transaction():
    payload = CommandBuilder.cancel_transaction()
    return execute_blocking_command(payload, "CANCEL_TRANSACTION")

# ==============================================================================
#  PRODUCT CONFIGURATION (SETTERS)
# ==============================================================================

@app.route('/api/products/price', methods=['POST'])
def set_product_price():
    """Sets price and waits for confirmation."""
    data = request.json
    sel = data.get('selection')
    price = data.get('price')
    if sel is None or price is None: return jsonify({"error": "Missing Data"}), 400
    
    payload = CommandBuilder.set_price(int(sel), int(price))
    return execute_blocking_command(payload, "SET_PRICE")

@app.route('/api/products/inventory', methods=['POST'])
def set_product_inventory():
    """Sets inventory and waits for confirmation."""
    data = request.json
    sel = data.get('selection')
    inv = data.get('inventory')
    if sel is None or inv is None: return jsonify({"error": "Missing Data"}), 400
    
    payload = CommandBuilder.set_inventory(int(sel), int(inv))
    return execute_blocking_command(payload, "SET_INVENTORY")

# ==============================================================================
#  LIVE DATA QUERIES (GETTERS)
# ==============================================================================

@app.route('/api/config/selection/<int:selection_id>', methods=['GET'])
def query_selection_config(selection_id):
    """
    Live Query: Asks VMC for config of a specific slot.
    Returns the config data in the response 'result' field.
    """
    payload = CommandBuilder.query_selection_config(selection_id)
    return execute_blocking_command(payload, "QUERY_CONFIG")

@app.route('/api/sales/daily', methods=['GET'])
def query_daily_sales():
    """
    Live Query: Asks VMC for Sales Data.
    Returns the {total_sales, revenue} in the response.
    """
    today_str = time.strftime("%Y%m%d")
    payload = CommandBuilder.query_daily_sales(today_str)
    return execute_blocking_command(payload, "QUERY_SALES")

# ==============================================================================
#  CACHED DATA (Database Reads)
# ==============================================================================

@app.route('/api/products', methods=['GET'])
def get_products_cached():
    """Reads local DB cache (No VMC delay)."""
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM products ORDER BY selection_id ASC")
    products = [dict(row) for row in cursor.fetchall()]
    return jsonify({"count": len(products), "products": products})

@app.route('/api/status', methods=['GET'])
def get_machine_status():
    """Reads local DB cache (Temp/Door)."""
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM vmc_status")
    status = {row['key']: row['value'] for row in cursor.fetchall()}
    return jsonify(status)

if __name__ == '__main__':
    print("🚀 Middleware API Running (Synchronous Mode)...")
    app.run(host='0.0.0.0', port=5000, debug=True)