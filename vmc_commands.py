import struct

"""
VMC Protocol V3.0 Command Library
---------------------------------
"""

# --- Command Constants ---
CMD_CHECK_SELECTION = 0x01
CMD_DISPENSE        = 0x03
CMD_DRIVE_DIRECT    = 0x06
CMD_REPORT_PRODUCT  = 0x11
CMD_SET_PRICE       = 0x12
CMD_SET_INVENTORY   = 0x13
CMD_SET_CAPACITY    = 0x14
CMD_SET_PRODUCT_ID  = 0x15
CMD_QUERY_CONFIG    = 0x42  # Query specific slot details
CMD_QUERY_SALES     = 0x43  # Daily Sales
CMD_INFO_SYNC       = 0x31
CMD_QUERY_STATUS    = 0x51
CMD_DEDUCT_MONEY    = 0x64

class CommandBuilder:
    @staticmethod
    def dispense(selection_id):
        return struct.pack('>BH', CMD_DISPENSE, selection_id).hex().upper()

    @staticmethod
    def deduct_card(amount):
        return struct.pack('>BI', CMD_DEDUCT_MONEY, amount).hex().upper()

    @staticmethod
    def cancel_transaction():
        return CommandBuilder.deduct_card(0)

    @staticmethod
    def sync_info():
        return struct.pack('B', CMD_INFO_SYNC).hex().upper()

    @staticmethod
    def query_machine_status():
        return struct.pack('B', CMD_QUERY_STATUS).hex().upper()

    # --- SET COMMANDS ---
    
    @staticmethod
    def set_price(selection_id, price):
        # 0x12 + Selection(2) + Price(4)
        return struct.pack('>BHI', CMD_SET_PRICE, selection_id, price).hex().upper()

    @staticmethod
    def set_inventory(selection_id, inventory):
        # 0x13 + Selection(2) + Inventory(1)
        return struct.pack('>BHB', CMD_SET_INVENTORY, selection_id, inventory).hex().upper()

    @staticmethod
    def set_capacity(selection_id, capacity):
        # 0x14 + Selection(2) + Capacity(1)
        return struct.pack('>BHB', CMD_SET_CAPACITY, selection_id, capacity).hex().upper()

    # --- QUERY COMMANDS ---

    @staticmethod
    def query_selection_config(selection_id):
        # 0x42 + Selection(2)
        return struct.pack('>BH', CMD_QUERY_CONFIG, selection_id).hex().upper()

    @staticmethod
    def query_daily_sales(date_str):
        # 0x43 + YYYYMMDD (4 bytes BCD or ASCII? PDF says 4 byte. Usually compressed BCD or Int)
        # Assuming Integer YYYYMMDD for now based on standard VMC protocols
        try:
            date_int = int(date_str) # Expects "20231027"
            return struct.pack('>BI', CMD_QUERY_SALES, date_int).hex().upper()
        except:
            return None

class ResponseParser:
    @staticmethod
    def parse_product_report(data_body):
        # Parses 0x11
        if len(data_body) < 11: return None
        sel, price, inv, cap, pid, status = struct.unpack('>HIBBHB', data_body[:11])
        return {
            "selection": sel, "price": price, "inventory": inv,
            "capacity": cap, "product_id": pid, "status": status
        }

    @staticmethod
    def parse_0x71_generic(data_body):
        """
        Parses the multi-purpose 0x71 return command.
        Structure: [SubCmd] [OpType] [Data...]
        """
        if len(data_body) < 3: return None
        
        sub_cmd = data_body[0]
        op_type = data_body[1] # 0x00=Read Success, 0x01=Set Success/Fail usually
        payload = data_body[2:]
        
        result = {"sub_command": sub_cmd, "op_type": op_type}

        # 1. SET CONFIRMATION (Price, Inv, etc.)
        # Usually OpType 0x01, Status 0x00=Success
        if sub_cmd in [0x12, 0x13, 0x14, 0x15]:
            status = payload[0] if len(payload) > 0 else 0xFF
            result["success"] = (status == 0x00)
            result["message"] = "Set Success" if status == 0x00 else "Set Failed"

        # 2. QUERY CONFIG (0x42 response)
        elif sub_cmd == 0x42 and op_type == 0x00:
            # Format: Price(4)+Inv(1)+Cap(1)+PID(2)+Mode(1)+Drop(1)+Jam(1)+Turn(1)
            if len(payload) >= 12:
                price, inv, cap, pid, mode, drop, jam, turn = struct.unpack('>IBBHBBBB', payload[:12])
                result["data"] = {
                    "price": price, "inventory": inv, "capacity": cap,
                    "product_id": pid, "motor_mode": mode
                }

        # 3. QUERY SALES (0x43 response)
        elif sub_cmd == 0x43 and op_type == 0x00:
            # Huge struct. Let's grab just Total Count(4) + Total Amt(4)
            if len(payload) >= 8:
                total_count, total_amt = struct.unpack('>II', payload[:8])
                result["data"] = {"total_sales_count": total_count, "total_revenue": total_amt}

        return result