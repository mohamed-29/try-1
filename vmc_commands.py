import struct

"""
VMC Protocol V3.0 Command Library
---------------------------------
This module handles:
1. Generating HEX payloads for commands (to be stored in DB).
2. Parsing complex binary responses (like 0x11 Product Info).
"""

# --- Command Constants ---
CMD_CHECK_SELECTION = 0x01
CMD_DISPENSE        = 0x03  # "Buy"
CMD_DRIVE_DIRECT    = 0x06  # Drive Motor directly
CMD_REPORT_PRODUCT  = 0x11  # VMC reporting product info
CMD_SET_PRICE       = 0x12
CMD_SET_INVENTORY   = 0x13
CMD_SET_CAPACITY    = 0x14
CMD_SET_PRODUCT_ID  = 0x15
CMD_POLL_INTERVAL   = 0x16
CMD_INFO_SYNC       = 0x31  # Force VMC to report all products (0x11)
CMD_QUERY_STATUS    = 0x51  # Query Temp/Door/etc
CMD_DEDUCT_MONEY    = 0x64  # Card Deduction

class CommandBuilder:
    """
    Generates the RAW PAYLOAD (Cmd + Data) to be inserted into the database.
    Note: The SerialController will add the PackNO, Length, and Checksum.
    """

    @staticmethod
    def dispense(selection_id):
        """
        0x03: Select to buy (Dispense).
        Payload: 03 + Selection(2 bytes)
        """
        # Big Endian for selection number (e.g. 10 -> 0x00 0x0A)
        return struct.pack('>BH', CMD_DISPENSE, selection_id).hex().upper()

    @staticmethod
    def deduct_card(amount):
        """
        0x64: Deduct Money (or Cancel if amount=0).
        Payload: 64 + Amount(4 bytes)
        """
        # Amount in cents? Assuming int.
        return struct.pack('>BI', CMD_DEDUCT_MONEY, amount).hex().upper()

    @staticmethod
    def cancel_transaction():
        """
        0x64 with amount 0 = Cancel.
        """
        return CommandBuilder.deduct_card(0)

    @staticmethod
    def sync_info():
        """
        0x31: Info Synchronization.
        Triggers VMC to send 0x11 reports for all slots.
        """
        return struct.pack('B', CMD_INFO_SYNC).hex().upper()

    @staticmethod
    def query_machine_status():
        """0x51: Query Status"""
        return struct.pack('B', CMD_QUERY_STATUS).hex().upper()

    # --- Setting Commands (Admin) ---

    @staticmethod
    def set_price(selection_id, price):
        """0x12: Set Price (4 bytes)"""
        return struct.pack('>BHI', CMD_SET_PRICE, selection_id, price).hex().upper()

    @staticmethod
    def set_inventory(selection_id, inventory):
        """0x13: Set Inventory (1 byte)"""
        return struct.pack('>BHB', CMD_SET_INVENTORY, selection_id, inventory).hex().upper()

class ResponseParser:
    """
    Decodes the data body (excluding Header/PackNO) from VMC.
    """

    @staticmethod
    def parse_product_report(data_body):
        """
        Parses 0x11 (Page 7).
        Format: Selection(2) + Price(4) + Inv(1) + Cap(1) + PID(2) + Status(1)
        Total 11 bytes.
        """
        if len(data_body) < 11:
            return None
        
        # Unpack Big Endian
        sel, price, inv, cap, pid, status = struct.unpack('>HIBBHB', data_body[:11])
        
        return {
            "selection": sel,
            "price": price,
            "inventory": inv,
            "capacity": cap,
            "product_id": pid,
            "status": status # 0=Normal, 1=Pause
        }

    @staticmethod
    def parse_deduction_result(data_body):
        # 0x64 doesn't return data directly, it triggers 0x21 Money Notice.
        pass